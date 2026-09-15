# SQL Logic Test Templates

These aren't executable pytest fixtures — that would require a live PostGIS instance
with seeded data, which this skill doesn't assume you have wired up. Instead, this is
a checklist of assertions to verify by inspection (or by running manually against your
own PostGIS instance) whenever this skill generates or edits a spatial query. Use it
as a pre-flight check before running generated SQL against real data.

## Test 1: Proximity query correctness

**Setup**: table with a known point A, and a known point B exactly 500.0m away (in a
projected/meter-unit CRS), and a known point C exactly 500.1m away.

**Assertion**: `ST_DWithin(geom, A, 500)` must include B, must exclude C.
**Common failure mode to check for**: using `ST_Distance(...) < 500` instead of
`ST_DWithin` — functionally similar for small datasets but not index-accelerated;
flag if the query is going to run against a large table without `ST_DWithin`.
**Also check**: is the CRS of the geometry column actually meter-based? If it's
EPSG:4326, "500" means 500 degrees, not 500 meters — this is the single most common
mistake in generated proximity queries.

## Test 2: Overlay correctness

**Setup**: two polygons with a known, calculable overlap area (e.g. two unit squares
offset so they share exactly 0.25 of each other).

**Assertion**: `ST_Area(ST_Intersection(a.geom, b.geom))` should equal the known
overlap area within floating-point tolerance.
**Common failure mode to check for**: computing `ST_Intersection` for a full cross
join instead of pre-filtering with `ST_Intersects` in the JOIN clause — correct
result, but won't scale; flag if the table sizes suggest this matters.
**Also check**: `ST_IsEmpty()` filter present for JOIN cases where geometries might
not actually overlap (JOIN ON ST_Intersects should already prevent this, but if the
predicate was accidentally omitted, verify the empty-geometry case is handled).

## Test 3: Spatial join / containment correctness

**Setup**: a point known to be strictly inside polygon A, a point known to be
strictly outside all polygons, a point known to be exactly ON a polygon boundary.

**Assertion**: the inside point joins to A. The outside point joins to nothing.
The boundary point's behavior depends on predicate choice — `ST_Contains` excludes
boundary points, `ST_Covers`/`ST_Intersects` includes them. Verify the generated
query used the predicate that matches the user's actual intent (this is a frequent
silent bug: "contains" in casual English often actually means "covers" in PostGIS
terms when boundary-touching should count).

## Test 4: CRS transform correctness in a query

**Assertion checklist**:
- [ ] Source SRID confirmed against actual data (`SELECT DISTINCT ST_SRID(geom) FROM ...`), not assumed
- [ ] `ST_SetSRID` used only when metadata is wrong/missing (no reprojection happens); `ST_Transform` used when an actual coordinate change is needed — verify the query uses the right one for its stated intent
- [ ] If the target/source CRS is a Malaysian grid, the EPSG code was checked against `references/malaysian_crs.md` rather than assumed from memory
- [ ] Resulting geometry's unit (meters vs degrees) matches what any subsequent `ST_Area`/`ST_Distance`/`ST_DWithin` call in the same query assumes

## Test 5: Index usage sanity check

**Assertion**: for any query using `ST_Intersects`/`ST_DWithin`/`ST_Contains` on a
table plausibly larger than a few thousand rows, a GiST index should exist on the
geometry column, and the query plan (`EXPLAIN ANALYZE`) should show an index scan,
not a sequential scan. If you have access to run `EXPLAIN`, do so before trusting
that a generated query will perform acceptably at scale.
