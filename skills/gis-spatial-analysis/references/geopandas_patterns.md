# GeoPandas / Shapely Patterns

General rules before any pattern below:
- **Set/check CRS explicitly.** `gdf.crs` should never be `None` going into an operation. `gdf.set_crs(epsg, allow_override=True)` sets metadata without reprojecting (use when you know the data's true CRS but it wasn't read correctly). `gdf.to_crs(epsg)` actually reprojects.
- **Distance/area units follow the CRS.** Same rule as PostGIS — don't run `.buffer(500)` on data in EPSG:4326 expecting meters; reproject to a projected CRS first (e.g. a Malaysian grid from `references/malaysian_crs.md` for Malaysian data).
- For custom Malaysian CRSs not in a standard `pyproj` database lookup by EPSG code, verify `pyproj` resolves the code correctly before trusting it — cross-check the resolved proj4 string against `references/malaysian_crs.md`, since some older GDAL/EPSG exports have known bugs for these codes (documented false-easting/gamma errors for EPSG:3168/3375 in older distributions).

## Loading + reprojecting

```python
import geopandas as gpd

parcels = gpd.read_file("parcels.shp")
print(parcels.crs)  # confirm it's set and matches expectations before doing anything else

# Reproject to GDM2000 Peninsula RSO for meter-accurate area/distance calcs
parcels_rso = parcels.to_crs(epsg=3375)
```

## Proximity

```python
from shapely.geometry import Point

pt = gpd.GeoSeries([Point(804671, 0)], crs="EPSG:3375")

# Buffer-based proximity: all parcels within 500m
buffer = pt.buffer(500).iloc[0]
nearby = parcels_rso[parcels_rso.intersects(buffer)]

# Nearest-neighbor (requires geopandas >= 0.10 with sjoin_nearest, or shapely STRtree directly)
nearest = gpd.sjoin_nearest(pt.to_frame("geometry"), parcels_rso, distance_col="dist_m")
```

## Overlay (intersection, union, difference)

```python
# Intersection of two polygon layers, with resulting area
overlap = gpd.overlay(layer_a, layer_b, how="intersection")
overlap["area_sqm"] = overlap.geometry.area  # valid only if CRS is meter-based projected

# Union (dissolve) — combine all parcels by a grouping column
dissolved = parcels_rso.dissolve(by="district_id")

# Difference
diff = gpd.overlay(parcels_rso, flood_zones_rso, how="difference")
```

## Spatial joins

```python
# Which points fall within which polygons
joined = gpd.sjoin(stations_rso, districts_rso, how="inner", predicate="within")

# Adjacent-parcel check (touches)
touching_pairs = gpd.sjoin(parcels_rso, parcels_rso, predicate="touches")
touching_pairs = touching_pairs[touching_pairs.index < touching_pairs.index_right]  # drop self/dupes
```

## CRS transform sanity-check pattern

Always worth doing for Malaysian grids given the known transcription-error history — verify the resolved proj4 string matches the reference table before trusting output:

```python
from pyproj import CRS

crs = CRS.from_epsg(3375)
print(crs.to_proj4())
# Compare x_0 against references/malaysian_crs.md's 804671 — flag if it resolves to 0
# (this is the specific known bug: some older GDAL/EPSG CSV exports have false_easting=0
# instead of 804670.24/804671 for this code)
```

## Chaining operations end-to-end

Real workflows rarely stop at one operation — e.g. raw field points need transforming
into a working CRS *before* a KNN match makes sense, and the match result needs a
tolerance check before it's useful, and parcel context on top of that. See
`scripts/workflow_tanam_pastian.py` for a full worked example (NDCDB boundary mark
refixation): transform → KNN nearest-mark match → displacement/tolerance status →
parcel boundary context, with each step kept as a separate testable function rather
than one monolithic block, and a verifiable demo case at the bottom (a point placed
exactly at a known CRS projection origin, so its expected output is checkable by
construction rather than by trusting the code blindly).

Two habits from that script worth generalizing to any multi-step pipeline:
- **Assert CRS consistency between steps**, not just at the start — e.g. before a
  `sjoin_nearest`, assert both inputs share the working CRS. A silent CRS drift
  introduced by an earlier step (a forgotten `.to_crs()`, a copy that lost its CRS)
  is far harder to spot after several more operations have run on top of it.
- **Don't assume containment for boundary features.** A boundary mark sitting exactly
  on a parcel edge may fail a strict `.within()` check due to floating-point boundary
  ambiguity — check distance-to-boundary against a small snap tolerance instead of
  relying on point-in-polygon containment when the feature is conceptually *on* an
  edge rather than inside an area.

## Area/length calc gotcha

`.area` and `.length` on a GeoSeries return values **in the CRS's native units** — meaningless in EPSG:4326 (degrees²/degrees), correct in meters²/meters for a projected Malaysian grid. Always reproject to a projected CRS before computing area/length, and state which CRS the numbers are in when reporting results.
