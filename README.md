# Database Chat

Ask a PostgreSQL or PostGIS database questions in plain English, see the answer
as a table or on a map, and download it as CSV, JSON, GeoJSON or a shapefile.

Point it at your own server and it works out what you have: it lists your
databases, reads their tables and columns, indexes the actual values so typos
still match, and works out which coordinate system to offer from where your
data sits. Nothing is hardcoded to one dataset.

No API key, no language model, nothing sent anywhere. The matching is done in
Python against the real values in your tables, so "how many stations at kucing"
finds the Kuching rows even though it is spelled wrong.

```bash
pip install -r requirements.txt
python chatbox.py          # then open http://localhost:5000
```

The defaults point at a local PostgreSQL (`localhost:5432`, user `postgres`).
Copy `.env.example` to `.env` to change that. To put it online, see
[DEPLOY.md](DEPLOY.md).

## What it does

- **Answers questions** - counts, filters, groupings, top-N, "list all
  districts". Follow-ups work: ask a question, then say *"show them"* or
  *"what about Miri"*.
- **Answers spatial questions with the geometry**, not with a text filter that
  happens to look right - see below.
- **Draws the rows on a map** when they come from a table with geometry.
  Shapes are simplified for the screen; downloads keep full detail.
- **Downloads anything on screen** - the rows behind an answer or a whole
  table. Shapefiles arrive as a `.zip` that unzips straight into QGIS, in
  lon/lat or in a projected grid.
- **Shows its working** - every answer has a *View SQL* button.

## Spatial questions

When a database holds both a point layer and a polygon layer, questions about
*where* things are run as PostGIS joins:

| ask | what runs |
| --- | --- |
| how many stations in each subbasin | `ST_Contains` join, counting points per polygon |
| which subbasin is station X in | point-in-polygon lookup |
| stations within 10 km of Kapit | `ST_DWithin` on `::geography` |
| nearest station to Kapit | KNN through `CROSS JOIN LATERAL ... ORDER BY <->` |

Distances are metres on the ground. That needs saying because in EPSG:4326 the
units are *degrees* - `ST_DWithin(geom, geom, 10000)` there matches every row
in the table rather than everything within 10 km, without any error to warn
you. The `::geography` cast is what makes the number mean what it says.

Ordinary questions are untouched: "stations per division" is still a plain
column grouping, because `division` is an attribute, not a place.

## Coordinate systems for downloads

A shapefile carries its own `.prj`, so it can be delivered in a projected grid
instead of lon/lat - which is what you want before measuring anything, because
areas and distances in EPSG:4326 come out in degrees.

**What gets offered depends on where your data is.** The app measures the
layer's extent and suggests accordingly:

| your data | suggested |
| --- | --- |
| Sabah / Sarawak | EPSG:3376, the Borneo national grid |
| Peninsular Malaysia | EPSG:3375, the Peninsular grid |
| anywhere else | the UTM zone covering it - London gets 32630, Lima 32718, Sydney 32756 |

Malaysian data also gets the full list of national and state grids, with legacy
Kertau and Timbalai datums labelled as such. Data elsewhere doesn't - twenty
Malaysian grids would be noise to someone mapping Peru.

Those EPSG codes are read at startup from
`skills/gis-spatial-analysis/references/malaysian_crs.md` rather than written
into the code. That file is a verified table; the modern GDM2000 grids and the
legacy Kertau/Timbalai ones share almost identical projection parameters but
sit on different ellipsoids, so a code recalled from memory can be wrong by
hundreds of metres while still looking plausible. Choosing the Peninsular grid
instead of the Borneo one for Sarawak data changes the computed area by 1.9%.

GeoJSON deliberately has no choice: the format is specified as WGS84 lon/lat.

## Layout

| path | what it is |
| --- | --- |
| `chatbox.py` | the whole server: schema reading, the question engine, the API |
| `templates/index.html` | the page |
| `static/app.js`, `static/app.css` | the front end |
| `static/vendor/` | Leaflet, served from here so the page loads nothing from a CDN |
| `migrate_to_host.py` | copies your local databases to a managed Postgres |
| `skills/gis-spatial-analysis/` | the GIS reference the spatial SQL and the CRS list come from |
| `test_smoke.py` | a smoke test - see below |
| `legacy/` | two earlier viewers, kept for reference and not deployed |

## Tests

```bash
python test_smoke.py
```

Checks the page, the security headers, the question router and the export
guards. The parts that need a database are skipped automatically when there is
no database to reach, so it is safe to run anywhere.

## Settings

Everything is optional; the defaults are a local PostgreSQL. The full table is
in [DEPLOY.md](DEPLOY.md#settings), but the ones worth knowing:

| variable | default | what it does |
| --- | --- | --- |
| `DATABASE_URL` | - | managed Postgres connection string; replaces the `PG*` settings |
| `VISIBLE_DATABASES` | - | an allow-list. Unset, every database with tables is offered |
| `APP_TITLE` | `Database Chat` | what the page calls itself |
| `ALLOW_RAW_SQL` | `1` | whether visitors may type `SELECT ...` themselves. Switch off in public |
| `SECRET_KEY` | random | signs download links so they survive a restart |

## Using it with your own database

Nothing needs changing in the code:

```bash
PGHOST=your-server PGUSER=you PGPASSWORD=... python chatbox.py
```

Every database on that server with at least one table appears in the dropdown.
PostgreSQL's own (`template0`, `template1`) and the hosting providers' admin
databases are hidden, and so is any database with no tables in it. Set
`VISIBLE_DATABASES` if you would rather name the ones to show.

Spatial questions switch on by themselves when a database holds one point layer
and one polygon layer. With several of each, name the ones you mean in the
question - the app will not guess between them.
