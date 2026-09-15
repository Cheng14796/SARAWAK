"""
End-to-end NDCDB boundary mark refixation (tanam pastian) workflow.

Pipeline: raw GPS field points (EPSG:4326)
          -> CRS transform to local Cassini-Soldner grid
          -> KNN match against nearest NDCDB reference mark
          -> displacement/tolerance status check (urban vs rural threshold)
          -> parcel context (which parcel's boundary the mark sits on)

NOT executed by Claude — this environment has no network access and doesn't have
geopandas/pyproj/shapely installed. Written to run in a real GIS Python environment.

VERIFICATION NOTE — read before trusting the demo data at the bottom of this file:
The demo point GPS_ORIGIN is constructed to exactly equal the projection origin of
the target CRS (same trick used in scripts/test_crs_transforms.py), so its transformed
location and zero displacement are verifiable by definition of the projection, not by
running the code and hoping. The demo point GPS_FAR is placed ~1 degree of longitude
away (~111km) specifically so its "outside tolerance" classification is obviously true
without needing an exact distance computation by hand — it is NOT a near-miss/boundary
test case. If you need a genuine near-miss test (e.g. displacement of exactly 0.05m),
compute it with pyproj in your real environment and don't trust a hand-derived offset
for anything sub-meter — projected distance from a geodetic offset isn't exactly linear.

TOLERANCE VALUES: the tolerance_lookup dict below is a placeholder shape, not
verified regulatory data. Do not use these numbers for real refixation review
without checking them against the authoritative PUK 2009 source text.
"""

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, Polygon

WORKING_CRS = "EPSG:3380"  # Selangor Cassini-Soldner (GDM2000) — swap per state, see references/malaysian_crs.md
RAW_CRS = "EPSG:4326"


def load_raw_gps_points(records: list[dict]) -> gpd.GeoDataFrame:
    """records: [{'point_id': ..., 'lon': ..., 'lat': ...}, ...]"""
    gdf = gpd.GeoDataFrame(
        records,
        geometry=[Point(r["lon"], r["lat"]) for r in records],
        crs=RAW_CRS,
    )
    return gdf


def transform_to_working_crs(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Step 1: reproject raw field points into the projected working grid.
    Always to_crs, never set_crs here — this is a real coordinate change, the
    points genuinely arrive in EPSG:4326 from GPS capture."""
    assert gdf.crs is not None, "raw points must have CRS set before transforming"
    return gdf.to_crs(WORKING_CRS)


def match_nearest_mark(points: gpd.GeoDataFrame, marks: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Step 2: KNN match each resurvey point to its nearest NDCDB reference mark.
    Both inputs must already be in WORKING_CRS — sjoin_nearest doesn't reproject
    for you, and a CRS mismatch here will raise, not silently misjoin."""
    assert points.crs == marks.crs == WORKING_CRS, "CRS mismatch before KNN match"
    matched = gpd.sjoin_nearest(
        points, marks, distance_col="displacement_m", how="left"
    )
    return matched


def check_tolerance(matched: gpd.GeoDataFrame, tolerance_lookup: dict, area_class_col: str = "area_class") -> gpd.GeoDataFrame:
    """Step 3: classify displacement against the area-class-appropriate tolerance.
    tolerance_lookup is caller-supplied on purpose — see module docstring, do not
    hardcode regulatory values inside this function."""
    def status(row):
        limit = tolerance_lookup.get(row[area_class_col])
        if limit is None:
            return "unknown area_class — cannot evaluate"
        return "within tolerance" if row["displacement_m"] <= limit else "requires refixing review"

    matched = matched.copy()
    matched["max_allowed_m"] = matched[area_class_col].map(tolerance_lookup)
    matched["status"] = matched.apply(status, axis=1)
    return matched


def add_parcel_context(matched: gpd.GeoDataFrame, parcels: gpd.GeoDataFrame, boundary_snap_tolerance_m: float = 0.5) -> gpd.GeoDataFrame:
    """Step 4: identify which parcel boundary each mark sits on. Boundary marks sit
    ON a parcel edge, not necessarily strictly inside a polygon — so this checks
    distance-to-boundary rather than plain containment, which would miss marks
    sitting exactly on an edge due to floating-point boundary ambiguity."""
    assert parcels.crs == WORKING_CRS, "parcels must be in the working CRS"

    def nearest_parcel_info(point):
        distances = parcels.boundary.distance(point)
        idx = distances.idxmin()
        return pd.Series({
            "nearest_parcel": parcels.loc[idx, "lot_no"],
            "distance_to_boundary_m": distances.loc[idx],
            "on_boundary": distances.loc[idx] <= boundary_snap_tolerance_m,
        })

    context = matched.geometry.apply(nearest_parcel_info)
    return pd.concat([matched.reset_index(drop=True), context.reset_index(drop=True)], axis=1)


def run_tanam_pastian_workflow(
    raw_points: gpd.GeoDataFrame,
    marks: gpd.GeoDataFrame,
    parcels: gpd.GeoDataFrame,
    tolerance_lookup: dict,
) -> gpd.GeoDataFrame:
    """Full pipeline, steps kept separate above so each can be unit-tested and
    inspected independently rather than only trusting the end-to-end result."""
    working_points = transform_to_working_crs(raw_points)
    matched = match_nearest_mark(working_points, marks)
    with_status = check_tolerance(matched, tolerance_lookup)
    final = add_parcel_context(with_status, parcels)
    return final[[
        "point_id", "mark_id", "area_class", "displacement_m", "max_allowed_m",
        "status", "nearest_parcel", "distance_to_boundary_m", "on_boundary",
    ]]


if __name__ == "__main__":
    # --- Demo / self-check data ---
    # M1 sits exactly at the EPSG:3380 projection origin (lat_0=3.68464905, lon_0=101.389107913889).
    marks = gpd.GeoDataFrame(
        {"mark_id": ["M1", "M2"], "area_class": ["urban", "rural"]},
        geometry=[Point(-34836.161, 56464.049), Point(-30000, 60000)],
        crs=WORKING_CRS,
    )

    # Parcel with the origin ON its west boundary edge (midpoint), not centered —
    # boundary marks realistically sit on an edge, not in the interior.
    parcels = gpd.GeoDataFrame(
        {"lot_no": ["PT9001"]},
        geometry=[Polygon([
            (-34836.161, 56414.049), (-34736.161, 56414.049),
            (-34736.161, 56514.049), (-34836.161, 56514.049),
        ])],
        crs=WORKING_CRS,
    )

    raw_points = load_raw_gps_points([
        # Exactly the projection origin -> must transform to exactly M1's location,
        # displacement 0, and sits exactly on PT9001's west edge. Verifiable by
        # definition, not by running the code and eyeballing it.
        {"point_id": "GPS_ORIGIN", "lon": 101.389107913889, "lat": 3.68464905},
        # ~1 degree of longitude away (~111km) — unambiguously far, not a near-miss.
        {"point_id": "GPS_FAR", "lon": 102.389107913889, "lat": 3.68464905},
    ])

    tolerance_lookup_PLACEHOLDER = {"urban": 0.050, "rural": 0.100}  # verify before real use

    result = run_tanam_pastian_workflow(raw_points, marks, parcels, tolerance_lookup_PLACEHOLDER)
    print(result.to_string(index=False))

    # Expected, by construction:
    # GPS_ORIGIN -> M1, displacement_m == 0.0, status == 'within tolerance',
    #               nearest_parcel == 'PT9001', on_boundary == True
    # GPS_FAR    -> whichever mark is nearer (irrelevant which), displacement_m
    #               on the order of 1e5 m, status == 'requires refixing review'
    row0 = result.iloc[0]
    assert row0["mark_id"] == "M1" and abs(row0["displacement_m"]) < 1e-6, "GPS_ORIGIN did not match M1 exactly"
    assert row0["status"] == "within tolerance", "GPS_ORIGIN should be within tolerance"
    assert row0["on_boundary"], "GPS_ORIGIN should sit on PT9001's boundary"
    row1 = result.iloc[1]
    assert row1["status"] == "requires refixing review", "GPS_FAR should clearly fail tolerance"
    print("\nSelf-checks passed.")
