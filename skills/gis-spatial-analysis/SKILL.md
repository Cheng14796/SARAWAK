---
name: gis-spatial-analysis
description: Spatial reasoning, PostGIS SQL, and GeoPandas/Python for geoprocessing tasks — proximity analysis, spatial joins, overlays (intersection/union/difference), buffering, and coordinate reference system (CRS) handling, with specialized support for Malaysian CRSs (GDM2000 RSO/Cassini-Soldner, legacy Kertau/Timbalai grids, EPSG:3375/3376/3377-3385/29873/3167-3168). Use this skill whenever the user asks for spatial SQL, PostGIS queries, GeoPandas code, coordinate transforms, "which points are near X", overlay/intersection of geometries, spatial joins, or anything involving EPSG codes, projections, or Malaysian survey/cadastral coordinate systems — even if they don't say "GIS" explicitly. Always consult this skill before writing spatial SQL or CRS transform code from memory, since coordinate system parameters are easy to get subtly wrong and errors are silent (wrong-datum shifts of meters to hundreds of meters).
---

# GIS Spatial Analysis

Helps with three things: PostGIS SQL, GeoPandas/Shapely Python, and coordinate reference system (CRS) correctness — with deep support for Malaysian systems (relevant for cadastral/JUPEM-adjacent work).

## Core workflow

1. **Identify what's actually being asked**: a query (SQL) vs. a script (Python/GeoPandas) vs. a coordinate transform vs. a mix. Don't assume — ask if genuinely ambiguous, otherwise default to PostGIS SQL for anything phrased as "find/select/join" over data that sounds like it lives in a database, and GeoPandas for anything phrased as "load a file/process a dataset/build a script."

2. **Identify every CRS in play before writing any code.** This is the step most likely to cause silent, hard-to-detect errors. For any task involving Malaysian data:
   - Read `references/malaysian_crs.md` before assuming any EPSG code or proj4/WKT string from memory. Do not hardcode Malaysian CRS parameters from general training knowledge — use the verified table.
   - Confirm which grid: **modern (GDM2000, EPSG:3375-3385)** vs **legacy (Kertau/Timbalai, EPSG:3167-3168/29871-29873/4110s-series)**. These look superficially similar (same projection method, similar parameter values) but use different ellipsoids/datums — mixing them silently produces errors of meters to hundreds of meters, not an obvious crash.
   - If the source/target CRS isn't stated, ask rather than guess — a wrong-datum assumption is worse than a clarifying question.

3. **Write the code.**
   - PostGIS patterns (proximity, overlay, spatial joins, transforms, indexing): `references/postgis_patterns.md`
   - GeoPandas/Shapely patterns (same operations, Python-side): `references/geopandas_patterns.md`
   - Always specify SRID/CRS explicitly in code — never rely on an implicit/assumed default.

4. **Pair code with concise spatial reasoning.** Don't just hand back a query — briefly state what it does, any tolerance/edge-case assumptions (e.g. buffer distance units following the CRS's linear unit, not always meters), and anything the user should sanity-check (e.g. "verify against a known point since I can't run this in your DB").

5. **For anything precision-critical (cadastral, legal boundaries, survey tolerances)**: flag explicitly if the task is approaching the domain of the JUPEM SOPs / Pekeliling KPUP tolerances (e.g. area-difference tolerances, boundary mark displacement limits) rather than silently applying generic GIS assumptions — those tolerances are regulatory, not just technical.

## Test suite

`scripts/test_crs_transforms.py` — validates the Malaysian CRS reference table: checks that each grid's defined origin maps to its false easting/northing (true by definition of the projection, so this catches transcription errors), and round-trip (forward→inverse) consistency. Run this after any edit to `references/malaysian_crs.md`, and before trusting a coordinate transform for real cadastral work. Requires `pyproj` — not available in this environment, written to run in a real GIS Python environment (e.g. the user's).

`scripts/test_sql_logic.md` — a set of PostGIS query templates paired with assertions about expected row-level behavior (not full pytest fixtures, since that needs a live PostGIS instance) — use these to sanity-check that a generated query's spatial logic (predicate choice, SRID handling, join direction) is correct before running it against real data.

`scripts/eval_checklist.md` — trigger-test prompts, steering-test assertions, and a baseline-comparison method for evaluating whether this skill is actually working (fires when it should, changes the output when it fires, stays quiet when irrelevant). Run this periodically, especially after any edit to SKILL.md's description or the reference files.

`scripts/workflow_tanam_pastian.py` — end-to-end worked example chaining CRS transform → KNN nearest-mark match → tolerance status check → parcel boundary context (NDCDB boundary mark refixation). Use this as the reference pattern for any multi-step pipeline task, not just this specific cadastral use case — each stage is a separate function so pieces can be reused independently. Verified passing in the user's real environment (both `test_crs_transforms.py` and this script) as of 2026-07-23.

## Quick reference: which Malaysian CRS am I dealing with?

| Situation | Use |
|---|---|
| Peninsular Malaysia, modern GDM2000 data | EPSG:3375 (RSO) or state Cassini-Soldner (3377-3385) |
| Sabah/Sarawak, modern GDM2000 data | EPSG:3376 |
| Peninsular Malaysia, pre-2003 legacy data | EPSG:3168/3167 (Kertau RSO) |
| Sabah/Sarawak, legacy/oil-and-gas-era data | EPSG:29873 (Timbalai 1948 RSO Borneo) |
| Data source unclear which datum | **Ask** — don't guess; see `references/malaysian_crs.md` for how to tell them apart from a .prj/WKT string |

See `references/malaysian_crs.md` for full parameters and the datum-confusion pitfalls (this is the single most common source of silent error in Malaysian GIS work).
