# PostGIS SQL Patterns

General rules before any pattern below:
- **Always cast/specify SRID explicitly.** Never assume a geometry column's SRID matches what you need — `ST_SRID(geom)` to check, `ST_SetSRID(geom, srid)` if it's unset (metadata-only, no reprojection), `ST_Transform(geom, srid)` to actually reproject.
- **Always use a GiST spatial index** for any table involved in a spatial join/proximity query at nontrivial scale: `CREATE INDEX idx_<table>_geom ON <table> USING GIST (geom);`
- **Distance units follow the CRS's linear unit.** In a projected CRS like GDM2000 RSO/Cassini (EPSG:3375-3385/3376), units are meters — `ST_DWithin(geom, pt, 500)` means 500 meters. In a geographic CRS (EPSG:4326), units are degrees — don't pass meters directly; either transform to a projected CRS first, or use the geography type (`::geography`) which handles meters natively over the sphere but is slower and less precise for cadastral-grade work than a proper local projection.

## Proximity (find features within a distance)

```sql
-- Find all parcels within 500m of a given point, in a projected (meter-unit) CRS
SELECT p.id, p.name, ST_Distance(p.geom, pt.geom) AS distance_m
FROM parcels p, (SELECT ST_SetSRID(ST_MakePoint(804671, 0), 3375) AS geom) pt
WHERE ST_DWithin(p.geom, pt.geom, 500)
ORDER BY distance_m;
```

```sql
-- Nearest-neighbor: closest N features to a point (uses the index-accelerated <-> operator)
SELECT id, name, geom <-> ST_SetSRID(ST_MakePoint(804671, 0), 3375) AS dist
FROM parcels
ORDER BY geom <-> ST_SetSRID(ST_MakePoint(804671, 0), 3375)
LIMIT 10;
```

## Overlay (intersection, union, difference)

```sql
-- Intersection: area where two polygon layers overlap, with area computed
SELECT
    a.id AS layer_a_id,
    b.id AS layer_b_id,
    ST_Intersection(a.geom, b.geom) AS overlap_geom,
    ST_Area(ST_Intersection(a.geom, b.geom)) AS overlap_area_sqm
FROM layer_a a
JOIN layer_b b ON ST_Intersects(a.geom, b.geom)
WHERE NOT ST_IsEmpty(ST_Intersection(a.geom, b.geom));
```

Note: filter with `ST_Intersects` in the JOIN before computing `ST_Intersection` — computing the intersection geometry for every possible pair is far more expensive than the boolean predicate, and the index accelerates `ST_Intersects`.

```sql
-- Union: dissolve all parcels within a district into one geometry
SELECT district_id, ST_Union(geom) AS dissolved_geom
FROM parcels
GROUP BY district_id;
```

```sql
-- Difference: area of parcel not covered by a flood-zone layer
SELECT p.id, ST_Difference(p.geom, fz.geom) AS unaffected_geom
FROM parcels p
JOIN flood_zones fz ON ST_Intersects(p.geom, fz.geom);
```

## Spatial joins (containment)

```sql
-- Which points fall within which polygons (e.g. survey stations within a district)
SELECT s.id AS station_id, d.name AS district
FROM stations s
JOIN districts d ON ST_Contains(d.geom, s.geom);
-- ST_Contains(A, B) = B is fully inside A. Use ST_Within(B, A) for the mirror-image predicate — pick whichever reads more naturally for the join direction.
```

```sql
-- Boundary-touching check (e.g. adjacent parcels for an amalgamation/penyatuan tanah candidate check)
SELECT a.id, b.id
FROM parcels a
JOIN parcels b ON ST_Touches(a.geom, b.geom) AND a.id < b.id;
```

## Nearest-neighbor + tolerance check (common cadastral/survey pattern)

Often the real question isn't "what's within N meters" (fixed radius) but "what's the
*nearest* reference feature, and is the displacement within an allowed tolerance" —
e.g. checking a resurveyed point against the nearest original control mark. This
combines KNN with a threshold check rather than a plain `ST_DWithin`:

```sql
SELECT rp.id, nearest.ref_id, nearest.ref_class,
       ST_Distance(rp.geom, nearest.geom) AS displacement,
       CASE WHEN ST_Distance(rp.geom, nearest.geom) <= <tolerance_for_ref_class>
            THEN 'within tolerance' ELSE 'requires review' END AS status
FROM resurvey_points rp
CROSS JOIN LATERAL (
    SELECT ref_id, ref_class, geom
    FROM reference_marks
    ORDER BY geom <-> rp.geom
    LIMIT 1
) nearest;
```

Key points:
- `CROSS JOIN LATERAL (... ORDER BY geom <-> rp.geom LIMIT 1)` is what makes this
  index-accelerated per row — a plain `ORDER BY ST_Distance(...) LIMIT 1` without the
  LATERAL join forces a full scan for every outer row instead of using the GiST
  index's KNN support (the `<->` operator).
- Don't hardcode the tolerance value in the query — different feature/area classes
  routinely carry different allowed tolerances (e.g. urban vs rural in a cadastral
  context), so look the threshold up per row (a CASE expression or a join to a
  tolerance-lookup table) rather than a single constant.
- **Confirm the boundary-inclusive reading before writing the comparison.** "Must not
  exceed X" reads as `<=` (displacement exactly at the limit still passes) — but
  don't assume this without checking the actual regulatory/spec wording for your
  domain, since `<` vs `<=` at an exact-tolerance boundary is a real pass/fail
  difference, not a rounding nuance.
- Never hardcode the actual tolerance *values* into this skill's reference files
  without independently verifying them against the authoritative source — regulatory
  thresholds sourced secondhand (a summary, a prior conversation, a scraped page) can
  be wrong in ways that don't announce themselves.

## Cross-SRID spatial join (transform-then-join)

Common when one layer arrives in a native/raw CRS (e.g. GPS capture in EPSG:4326)
and needs joining against data stored in a projected working CRS (e.g. a Malaysian
grid). PostGIS refuses to compare geometries with mismatched SRIDs outright — this
is one of the few CRS mistakes that fails loudly (a hard error) rather than quietly
returning a wrong answer, but you still need to transform explicitly:

```sql
SELECT a.id, b.id
FROM raw_points a          -- geom stored in its native/raw SRID, e.g. 4326
JOIN target_layer b        -- geom stored in the working projected SRID
  ON ST_Contains(b.geom, ST_Transform(a.geom, ST_SRID(b.geom)));
```

Key points:
- **Transform the smaller/less-indexed side**, not the side with the spatial index —
  here that's the point layer, not the polygon layer. The GiST index on the indexed
  side still accelerates the bounding-box filter even though the other argument is
  computed on the fly; transforming the indexed side instead would silently defeat
  its index.
- Set the source SRID correctly at ingestion (`ST_SetSRID`) rather than relying on
  a default — a raw GPS/collector feed won't always arrive with SRID metadata already
  attached.
- At scale (repeated joins against a large raw-CRS points table), consider
  materializing a pre-transformed geometry column with its own index instead of
  transforming per-query every time.
- The hard-error behavior on SRID mismatch is a safety net worth relying on
  deliberately: if a query using two geometry columns unexpectedly throws an SRID
  error, that's PostGIS catching a real bug, not friction to route around by
  force-casting.

## CRS transforms in SQL

```sql
-- Reproject a table's geometry from GDM2000 Peninsula RSO to WGS84 for web display
SELECT id, ST_Transform(geom, 4326) AS geom_wgs84
FROM parcels;  -- assumes geom column SRID is already correctly set to 3375
```

```sql
-- If SRID metadata is wrong/missing (common with legacy imports) — set it first, don't transform blind
UPDATE parcels SET geom = ST_SetSRID(geom, 3375) WHERE ST_SRID(geom) = 0;
```

If the CRS involved is one of the Malaysian legacy/modern grids, confirm the exact EPSG code against `references/malaysian_crs.md` before writing the transform — see that file's datum-confusion pitfalls section.

## Indexing checklist for any nontrivial spatial query

```sql
CREATE INDEX IF NOT EXISTS idx_parcels_geom ON parcels USING GIST (geom);
ANALYZE parcels;
```

Without this, `ST_Intersects`/`ST_DWithin`/`ST_Contains` fall back to full sequential scans with per-row geometry comparison — fine for a few hundred rows, prohibitive beyond that.
