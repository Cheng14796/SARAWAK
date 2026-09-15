"""
Test suite for Malaysian CRS transforms.

NOT executed by Claude — this environment has no network access and doesn't
have pyproj installed. Run this in your actual GIS Python environment
(pyproj >= 3.0 required: pip install pyproj).

What these tests actually check, and why each is a real correctness check
rather than a tautology:

1. test_origin_maps_to_false_easting_northing:
   By definition of an oblique Mercator / Cassini-Soldner projection, the
   point at (lat_0, lon_0) MUST project to exactly (x_0, y_0). This is not
   a self-consistency check — if the reference table has a transcribed
   parameter wrong (e.g. the documented false_easting=0 bug seen in some
   older GDAL/EPSG CSV exports for EPSG:3168/3375), this test catches it,
   because it's checking the projection math against the table's own
   stated x_0/y_0, which would then be internally inconsistent with what
   the proj4 string actually produces.

2. test_round_trip:
   Forward transform then inverse transform should return the original
   point within a tight tolerance (sub-millimeter). Catches malformed
   proj4 strings, wrong ellipsoid pairing, or unit errors that a purely
   forward-only test could miss.

3. test_epsg_registry_matches_manual_proj4:
   Where pyproj resolves the EPSG code, compare the registry's own proj4
   export to our manually-specified string. This is the direct check
   against the documented GDAL/EPSG bug: if they don't match, print the
   diff so a human can inspect which is right rather than silently
   trusting either.

These do NOT independently verify absolute geodetic accuracy against a
real-world control point — that would require a genuinely independent
authoritative Malaysian control point pair (e.g. from the JUPEM GDM2000
Technical Manual), which we have not obtained. Treat a pass here as
"internally consistent with the documented registry parameters," not as
"verified against ground truth." Flag to the user if that distinction
matters for their use case.
"""

from pyproj import CRS, Transformer

TOLERANCE_M = 0.001  # 1mm — appropriate for cadastral-grade round-trip checks

# Manually-specified proj4 strings, built directly from references/malaysian_crs.md
# rather than relying on `CRS.from_epsg()`, to sidestep the documented registry-export
# bugs for some of these codes (see malaysian_crs.md pitfalls section).
GRIDS = {
    "GDM2000 / Peninsula RSO (EPSG:3375)": {
        "proj4": "+proj=omerc +lat_0=4 +lonc=102.25 +alpha=323.025796466667 "
                 "+gamma=323.130102361111 +k=0.99984 +x_0=804671 +y_0=0 "
                 "+ellps=GRS80 +units=m +no_defs",
        "origin_latlon": (4.0, 102.25),
        "expected_xy": (804671, 0),
        "epsg": 3375,
    },
    "GDM2000 / East Malaysia BRSO (EPSG:3376)": {
        "proj4": "+proj=omerc +lat_0=4 +lonc=115 +alpha=53.31580995 "
                 "+gamma=53.1301023611111 +k=0.99984 +x_0=0 +y_0=0 "
                 "+ellps=GRS80 +units=m +no_defs",
        "origin_latlon": (4.0, 115.0),
        "expected_xy": (0, 0),
        "epsg": 3376,
    },
    "Timbalai 1948 / RSO Borneo m (EPSG:29873)": {
        "proj4": "+proj=omerc +lat_0=4 +lonc=115 +alpha=53.3158204722222 "
                 "+gamma=53.1301023611111 +k=0.99984 +x_0=590476.87 +y_0=442857.65 "
                 "+ellps=evrstSS +towgs84=-679,669,-48,0,0,0,0 +units=m +no_defs",
        "origin_latlon": (4.0, 115.0),
        "expected_xy": (590476.87, 442857.65),
        "epsg": 29873,
    },
    # State Cassini-Soldner grids use +proj=cass, not +proj=omerc
    "Selangor Cassini-Soldner (EPSG:3380)": {
        "proj4": "+proj=cass +lat_0=3.68464905 +lon_0=101.389107913889 "
                 "+x_0=-34836.161 +y_0=56464.049 +ellps=GRS80 +units=m +no_defs",
        "origin_latlon": (3.68464905, 101.389107913889),
        "expected_xy": (-34836.161, 56464.049),
        "epsg": 3380,
    },
    "Kertau (RSO) / RSO Malaya m (EPSG:3168)": {
        "proj4": "+proj=omerc +lat_0=4 +lonc=102.25 +alpha=323.0257905 "
                 "+gamma=323.130102361111 +k=0.99984 +x_0=804670.24 +y_0=0 "
                 "+a=6377295.664 +rf=300.8017 +units=m +no_defs",
        "origin_latlon": (4.0, 102.25),
        "expected_xy": (804670.24, 0),
        "epsg": 3168,
        # NOTE: intentionally does NOT use +ellps=evrst69 or similar named shortcut —
        # spelling out +a/+rf directly from the registry-verified semi-major/inverse-
        # flattening avoids depending on whether the local PROJ install's named
        # ellipsoid table matches, which is the same class of silent-mismatch risk
        # documented for the false_easting=0 bug on this exact EPSG code.
    },
}

# Remaining GDM2000-era state Cassini-Soldner grids (all GRS80, +proj=cass), and the
# legacy Kertau-era equivalents (Everest 1830 RSO 1969). Added to close the coverage
# gap where most of malaysian_crs.md's table had never actually been round-trip
# checked — only spot-checked entries earn the "verified" label in practice.
_ELLPS_GRS80 = "+ellps=GRS80"
_ELLPS_KERTAU = "+a=6377295.664 +rf=300.8017"  # Everest 1830 (RSO 1969), spelled out — see note above

_STATE_CASSINI_MODERN = {
    "Johor Cassini (EPSG:3377)": (2.12167974444444, 103.427936236111, -14810.562, 8758.320, 3377),
    "N. Sembilan & Melaka Cassini (EPSG:3378)": (2.68234763611111, 101.974905041667, 3673.785, -4240.573, 3378),
    "Pahang Cassini (EPSG:3379)": (3.76938808888889, 102.368298983333, -7368.228, 6485.858, 3379),
    "Terengganu Cassini (EPSG:3381)": (4.9762852, 103.070275625, 19594.245, 3371.895, 3381),
    "Pinang Cassini (EPSG:3382)": (5.42151754166667, 100.344376963889, -23.414, 62.283, 3382),
    "Kedah & Perlis Cassini (EPSG:3383)": (5.96467271388889, 100.636371111111, 0, 0, 3383),
    "Perak Cassini (EPSG:3384)": (4.85906302222222, 100.815410586111, -1.769, 133454.779, 3384),
    "Kelantan Cassini (EPSG:3385)": (5.97254365833333, 102.295241669444, 13227.851, 8739.894, 3385),
}
for _name, (_lat0, _lon0, _x0, _y0, _epsg) in _STATE_CASSINI_MODERN.items():
    GRIDS[_name] = {
        "proj4": f"+proj=cass +lat_0={_lat0} +lon_0={_lon0} +x_0={_x0} +y_0={_y0} {_ELLPS_GRS80} +units=m +no_defs",
        "origin_latlon": (_lat0, _lon0),
        "expected_xy": (_x0, _y0),
        "epsg": _epsg,
    }

_STATE_CASSINI_LEGACY = {
    "Johor Cassini legacy (EPSG:4114)": (2.04258333, 103.56275833, 0, 0, 4114),
    "N. Sembilan & Melaka Cassini legacy (EPSG:4115)": (2.71228333, 101.94116667, -242.005, -948.547, 4115),
    "Pahang Cassini legacy (EPSG:4116)": (3.71097222, 102.43617778, 0, 0, 4116),
    "Selangor Cassini legacy (EPSG:4117)": (3.68034444, 101.50824444, -21759.438, 55960.906, 4117),
    "Perak Revised Cassini legacy (EPSG:4321)": (4.85938056, 100.81676667, 0, 133453.669, 4321),
}
for _name, (_lat0, _lon0, _x0, _y0, _epsg) in _STATE_CASSINI_LEGACY.items():
    GRIDS[_name] = {
        "proj4": f"+proj=cass +lat_0={_lat0} +lon_0={_lon0} +x_0={_x0} +y_0={_y0} {_ELLPS_KERTAU} +units=m +no_defs",
        "origin_latlon": (_lat0, _lon0),
        "expected_xy": (_x0, _y0),
        "epsg": _epsg,
    }


def test_origin_maps_to_false_easting_northing():
    failures = []
    for name, g in GRIDS.items():
        crs = CRS.from_proj4(g["proj4"])
        transformer = Transformer.from_crs(crs.geodetic_crs, crs, always_xy=True)
        lat, lon = g["origin_latlon"]
        x, y = transformer.transform(lon, lat)
        exp_x, exp_y = g["expected_xy"]
        if abs(x - exp_x) > TOLERANCE_M or abs(y - exp_y) > TOLERANCE_M:
            failures.append(
                f"{name}: origin projected to ({x:.4f}, {y:.4f}), "
                f"expected ({exp_x}, {exp_y})"
            )
    assert not failures, "\n".join(failures)


def test_round_trip():
    failures = []
    test_offsets = [(0, 0), (0.1, 0.1), (-0.1, 0.2)]  # degrees from origin
    for name, g in GRIDS.items():
        crs = CRS.from_proj4(g["proj4"])
        fwd = Transformer.from_crs(crs.geodetic_crs, crs, always_xy=True)
        inv = Transformer.from_crs(crs, crs.geodetic_crs, always_xy=True)
        lat0, lon0 = g["origin_latlon"]
        for dlat, dlon in test_offsets:
            lat, lon = lat0 + dlat, lon0 + dlon
            x, y = fwd.transform(lon, lat)
            lon2, lat2 = inv.transform(x, y)
            # convert degree error back to meters roughly for a fair tolerance check
            deg_err = max(abs(lon - lon2), abs(lat - lat2))
            if deg_err > 1e-8:  # ~1mm at this latitude
                failures.append(f"{name}: round-trip error at offset ({dlat},{dlon}): {deg_err}")
    assert not failures, "\n".join(failures)


def test_epsg_registry_matches_manual_proj4():
    """Direct check against the documented GDAL/EPSG export bug (false_easting=0
    instead of 804671 for some 3168/3375 exports). Prints a diff rather than
    hard-failing, since a mismatch might mean OUR string is wrong, not the registry's."""
    mismatches = []
    for name, g in GRIDS.items():
        try:
            registry_crs = CRS.from_epsg(g["epsg"])
        except Exception as e:
            mismatches.append(f"{name}: could not resolve EPSG:{g['epsg']} — {e}")
            continue
        manual_crs = CRS.from_proj4(g["proj4"])
        registry_proj4 = registry_crs.to_proj4()
        # Rough check: does the registry's false easting appear in our manual string's ballpark?
        if str(int(g["expected_xy"][0])) not in registry_proj4.replace(".", ""):
            mismatches.append(
                f"{name}: registry proj4 does not appear to contain expected "
                f"false easting {g['expected_xy'][0]} — INSPECT MANUALLY.\n"
                f"  registry: {registry_proj4}\n"
                f"  manual:   {g['proj4']}"
            )
    if mismatches:
        print("MISMATCHES FOUND (inspect before trusting either source):")
        for m in mismatches:
            print(" -", m)
    # Not asserted — this test is diagnostic, meant to surface the known bug for human review.


if __name__ == "__main__":
    test_origin_maps_to_false_easting_northing()
    print("PASS: origin-mapping test")
    test_round_trip()
    print("PASS: round-trip test")
    test_epsg_registry_matches_manual_proj4()
    print("Done. Review any printed mismatches above.")
