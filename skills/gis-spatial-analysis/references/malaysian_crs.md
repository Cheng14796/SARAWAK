# Malaysian Coordinate Reference Systems — Verified Reference

All parameters below are verified against the EPSG registry (not taken from general training memory — Malaysian CRS data has known transcription errors floating around online, see pitfalls section). Always prefer this table over recalling parameters from memory.

## Modern national datum: GDM2000 (current)

Ellipsoid: GRS80. This is the datum in current production use for JUPEM/eKadaster-era work.

### National RSO (Rectified Skew Orthomorphic) grids

| Name | EPSG | lat_0 | lonc | alpha | gamma | k | x_0 | y_0 |
|---|---|---|---|---|---|---|---|---|
| GDM2000 / Peninsula RSO | **3375** | 4° | 102.25° | 323.025796466667° | 323.130102361111° | 0.99984 | 804671 | 0 |
| GDM2000 / East Malaysia BRSO | **3376** | 4° | 115° | 53.31580995° | 53.1301023611111° | 0.99984 | 0 | 0 |

Ellipsoid GRS80 for both. Method: Hotine Oblique Mercator.

### State Cassini-Soldner grids (GDM2000/GRS80, EPSG method 9806)

| State | EPSG | lat_0 (°) | lon_0 (°) | x_0 (m) | y_0 (m) |
|---|---|---|---|---|---|
| Johor | 3377 | 2.12167974444444 | 103.427936236111 | -14810.562 | 8758.320 |
| N. Sembilan & Melaka | 3378 | 2.68234763611111 | 101.974905041667 | 3673.785 | -4240.573 |
| Pahang | 3379 | 3.76938808888889 | 102.368298983333 | -7368.228 | 6485.858 |
| Selangor | 3380 | 3.68464905 | 101.389107913889 | -34836.161 | 56464.049 |
| Terengganu | 3381 | 4.9762852 | 103.070275625 | 19594.245 | 3371.895 |
| Pinang | 3382 | 5.42151754166667 | 100.344376963889 | -23.414 | 62.283 |
| Kedah & Perlis | 3383 | 5.96467271388889 | 100.636371111111 | 0 | 0 |
| Perak | 3384 | 4.85906302222222 | 100.815410586111 | -1.769 | 133454.779 |
| Kelantan | 3385 | 5.97254365833333 | 102.295241669444 | 13227.851 | 8739.894 |

## Legacy datums (pre-GDM2000) — DO NOT use for current production work unless explicitly converting historical data

Two unrelated legacy datums exist depending on region — do not treat them as interchangeable:

### Peninsular Malaysia: Kertau (RSO)
Ellipsoid: **Everest 1830 (RSO 1969)**, semi-major 6377295.664, inverse flattening 300.8017.

| Name | EPSG | Notes |
|---|---|---|
| Kertau (RSO) / RSO Malaya (m) | 3168 | lat_0=4°, lonc=102.25°, azimuth(alpha)=323.0257905°, rectified_grid_angle(gamma)=323.130102361111°, k=0.99984, **x_0=804670.24**, y_0=0. Method: Hotine Oblique Mercator variant A. Replaced by EPSG:3375. **Note x_0 is 804670.24 here, not 804671 (the GDM2000/EPSG:3375 value) — close enough to typo past, confirm which one you actually need.** |
| Kertau (RSO) / RSO Malaya (ch) | 3167 | Imperial (chains): x_0=40000 (British chains), same angles/ellipsoid as 3168. |

Legacy state Cassini grids (Kertau-era), verified — note these use **different EPSG codes** than the GDM2000-era codes with similar-looking state names:

| State grid | EPSG | lat_0 (°) | lon_0 (°) | x_0 (m) | y_0 (m) |
|---|---|---|---|---|---|
| Johor Cassini Grid | 4114 | 2.04258333 | 103.56275833 | 0 | 0 |
| N. Sembilan & Melaka Cassini Grid | 4115 | 2.71228333 | 101.94116667 | -242.005 | -948.547 |
| Pahang Cassini Grid | 4116 | 3.71097222 | 102.43617778 | 0 | 0 |
| Selangor Cassini Grid (legacy) | 4117 | 3.68034444 | 101.50824444 | -21759.438 | 55960.906 |
| Perak Revised Cassini Grid (legacy) | 4321 | 4.85938056 | 100.81676667 | 0 | 133453.669 |

### Sabah/Sarawak: Timbalai 1948
Ellipsoid: **Everest 1830 (1967 Definition)**, semi-major 6377298.556, inverse flattening 300.8017. TOWGS84: -679, 669, -48, 0, 0, 0, 0.

| Name | EPSG | Params |
|---|---|---|
| Timbalai 1948 / RSO Borneo (m) | **29873** | lat_0=4°, lonc=115°, alpha=53.3158204722222°, gamma=53.1301023611111°, k=0.99984, x_0=590476.87, y_0=442857.65 |
| Timbalai 1948 / RSO Borneo (ft) | 29872 | Same angles, feet-based false easting/northing |
| Timbalai 1948 / RSO Borneo (ch) | 29871 | Same angles, chains-based false easting/northing |

Method: Hotine Oblique Mercator **variant B** (azimuth-center) — note this differs from the variant A used for Peninsular Kertau RSO; the formula isn't identical even though the angle parameters look similar.

## ⚠️ Datum-confusion pitfalls (read this before touching Malaysian data)

1. **GDM2000 vs Kertau/Timbalai look deceptively similar.** The RSO angle parameters (lat_0=4°, alpha≈53.3°/323.0°, k=0.99984) are nearly identical between modern GDM2000 grids and their legacy predecessors, because the projection definition carried over — but the underlying **ellipsoid and datum are different** (GRS80 vs Everest 1830 variants). Applying a legacy false easting/northing to a GDM2000-labeled point (or vice versa) is a wrong-datum error of **meters to hundreds of meters**, not a rounding error, and it will not throw an error — it will silently produce a plausible-looking but wrong coordinate.

2. **State Cassini grid EPSG codes are NOT sequential between eras.** GDM2000-era state grids are 3377-3385. Legacy Kertau-era state grids are scattered in the 4110s range (4114-4117, 4321) and do **not** map 1:1 by state name to a similar-sounding code — e.g. legacy Perak is EPSG:4321, not "4384". Always look up the code in this table; don't infer it from the GDM2000 code by pattern-matching.

3. **ArcGIS naming trap**: ArcGIS has both "Kertau RSO Malaya (m)" and "Kertau (RSO) RSO Malaya (m)" as distinct, similarly-named CRS entries with different datums/ellipsoids/false eastings. Only the one with "(RSO) RSO" in the name conforms to EPSG:3168. If a user gives you a `.prj`/WKT string, check the ellipsoid semi-major axis and datum name directly rather than trusting a CRS's display name.

3b. **Confirmed real-world instance of the false-easting=0 bug**: a documented QGIS/GDAL user report shows OSGeo4W's `pcs.csv` EPSG export encoding EPSG:3168 with `false_easting=0` instead of the correct 804670.24 — not a hypothetical risk, an actual shipped bug in a widely-used GIS toolchain's EPSG export. If a transform through EPSG:3168 produces coordinates that look shifted by ~804.6km in easting, this is almost certainly why — check the resolved false easting before assuming anything else is wrong.

4. **How to tell datums apart from a `.prj`/WKT file**: check the ellipsoid semi-major axis.
   - `6378137` (approx) → GRS80 → **modern GDM2000**
   - `6377295.664` → Everest 1830 (RSO 1969) → **legacy Kertau (Peninsular)**
   - `6377298.556` → Everest 1830 (1967 Definition) → **legacy Timbalai 1948 (Sabah/Sarawak)**

5. **When the source CRS is genuinely unknown** (e.g. old field data, unlabeled shapefile): don't guess based on region alone. Ask the user, or if you have a known reference point, sanity-check by projecting a candidate point in each candidate CRS and confirming which lands in the plausible geographic area — but state this reasoning out loud rather than silently picking one.

## Confirmed-but-unverified data (from prior conversation, flagged — do not treat as fact without independent confirmation)
- Kertau 1948 ↔ GDM2000 seven-parameter Helmert/Bursa-Wolf shift (ΔX≈-11m, ΔY≈+851m, ΔZ≈+5m) — plausible, not independently verified against the registry transformation record.
- Claims of an exact zero-shift GDM2000↔WGS84/ITRF transformation — the ~1m-accuracy approximation is confirmed, but treat "exact zero" as a stated approximation, not a geodetic identity.
