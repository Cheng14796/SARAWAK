"""
Database Chat - Clean version, no bugs
Run: python chatbox.py
Open: http://localhost:5000
"""

from flask import Flask, jsonify, Response, request
import psycopg2
import json
import re
import csv
import io
import os
import shutil
import tempfile
import zipfile
import datetime
import difflib
import decimal
import uuid
import hmac
import hashlib
from urllib.parse import urlparse, parse_qs, unquote

app = Flask(__name__)

# How much of the data we index up front so questions can be matched
# against real values (see get_schema_info).
MAX_ROWS_TO_INDEX = 500000     # skip DISTINCT scans on tables bigger than this
MAX_DISTINCT_PER_COL = 2000    # over this a column is free text, not a category

TEXT_TYPES = ("text", "varchar", "character varying", "char",
              "character", "bpchar", "name", "citext")

NUMERIC_TYPES = ("integer", "bigint", "smallint", "numeric", "decimal",
                 "real", "double precision", "money")

# PostGIS creates these in every spatial database. They are plumbing, not data -
# spatial_ref_sys alone is 8500 rows of projection text - so they must not show
# up as tables the user can ask about, or be indexed for value matching.
SYSTEM_TABLES = ("spatial_ref_sys", "geometry_columns", "geography_columns",
                 "raster_columns", "raster_overviews", "layer", "topology")

SYSTEM_SCHEMAS = ("topology", "tiger", "tiger_data")


def is_geom_col(name):
    n = str(name).lower()
    return "geom" in n or "geometry" in n or n in ("shape", "the_geom")


def is_text_type(dtype):
    return str(dtype).lower() in TEXT_TYPES


def is_numeric_type(dtype):
    return str(dtype).lower() in NUMERIC_TYPES

def _load_dotenv(name=".env"):
    """Read a .env sitting next to this file, if there is one.

    This is what keeps the database password out of the source, and so out of
    the repo, without adding a dependency. Real environment variables win, so
    a host's settings always override the file.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()


def _db_config():
    """Connection settings, from the environment when it is set.

    Nothing has to be configured to run this on your own machine - the
    defaults are the local PostgreSQL install. A host like Render or Railway
    hands over a DATABASE_URL instead, so no password is ever written down
    in this file.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        p = urlparse(url)
        cfg = {"host": p.hostname or "localhost",
               "port": p.port or 5432,
               "user": unquote(p.username or "postgres"),
               "password": unquote(p.password or "")}
        # Managed Postgres almost always requires TLS.
        q = parse_qs(p.query or "")
        cfg["sslmode"] = (q.get("sslmode") or ["require"])[0]
        # The database in the URL is where we look for the list of databases.
        cfg["_default_db"] = (p.path or "/postgres").lstrip("/") or "postgres"
        return cfg
    cfg = {"host": os.environ.get("PGHOST", "localhost"),
           "port": int(os.environ.get("PGPORT", "5432")),
           "user": os.environ.get("PGUSER", "postgres"),
           "password": os.environ.get("PGPASSWORD", ""),
           "_default_db": os.environ.get("PGDATABASE", "postgres")}
    if os.environ.get("PGSSLMODE"):
        cfg["sslmode"] = os.environ["PGSSLMODE"]
    return cfg


DB_CONFIG = _db_config()

# Where to look up the list of databases - "postgres" locally, or whatever
# database the host's DATABASE_URL points at.
ADMIN_DB = DB_CONFIG.pop("_default_db", "postgres")

# Only these databases are offered in the dropdown, in this order.
# Set to an empty list to show every database on the server again.
VISIBLE_DATABASES = [d.strip() for d in os.environ.get(
    "VISIBLE_DATABASES", "sarawak basin,subbasin_gis").split(",") if d.strip()]

# Cache schema per database so we don't reload every question
_schema_cache = {}


def get_connection(dbname):
    cfg = dict(DB_CONFIG)
    cfg["database"] = dbname
    return psycopg2.connect(**cfg)


def get_databases():
    conn = get_connection(ADMIN_DB)
    try:
        cur = conn.cursor()
        cur.execute("SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname")
        dbs = [r[0] for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    if VISIBLE_DATABASES:
        # Keep the configured order, and skip any that aren't on this server
        return [d for d in VISIBLE_DATABASES if d in dbs]
    return dbs


def db_allowed(dbname):
    return (not VISIBLE_DATABASES) or dbname in VISIBLE_DATABASES


def get_schema_info(dbname):
    # Use cache
    if dbname in _schema_cache:
        return _schema_cache[dbname]

    conn = get_connection(dbname)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
              AND table_type = 'BASE TABLE'
            ORDER BY table_schema, table_name
        """)
        tables = cur.fetchall()
        result = []

        for schema, table in tables:
            if table.lower() in SYSTEM_TABLES or schema.lower() in SYSTEM_SCHEMAS:
                continue
            cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
            """, (schema, table))
            cols = [{"name": c[0], "type": c[1]} for c in cur.fetchall()]

            try:
                cur.execute('SELECT COUNT(*) FROM {0}.{1}'.format(
                    quote_ident(schema), quote_ident(table)))
                count = cur.fetchone()[0]
            except Exception:
                conn.rollback()
                count = -1

            # Index the DISTINCT values of every text-like column so a question
            # can be matched against real data, not just against table names.
            # Big tables are skipped - DISTINCT over millions of rows is slow.
            values = {}
            sample = {}
            index_ok = 0 <= count <= MAX_ROWS_TO_INDEX
            for c in cols:
                if is_geom_col(c["name"]) or not is_text_type(c["type"]):
                    continue
                if not index_ok:
                    continue
                try:
                    cur.execute(
                        'SELECT DISTINCT {0} FROM {1}.{2} '
                        'WHERE {0} IS NOT NULL LIMIT {3}'.format(
                            quote_ident(c["name"]), quote_ident(schema),
                            quote_ident(table), MAX_DISTINCT_PER_COL + 1
                        )
                    )
                    vals = []
                    for r in cur.fetchall():
                        try:
                            v = str(r[0]).strip()
                        except Exception:
                            continue
                        if v and len(v) <= 200:
                            vals.append(v)
                    # Over the cap means it is free text (a description, an id) -
                    # not worth matching a question against.
                    if vals and len(vals) <= MAX_DISTINCT_PER_COL:
                        values[c["name"]] = vals
                    if vals:
                        sample[c["name"]] = vals[:5]
                except Exception:
                    # A failed statement aborts the transaction - reset it,
                    # otherwise every later query on this connection fails too.
                    conn.rollback()

            result.append({
                "schema": schema,
                "table": table,
                "columns": cols,
                "rows": count,
                "sample": sample,
                "values": values
            })

        cur.close()
    finally:
        conn.close()
    _schema_cache[dbname] = result
    return result


READ_ONLY_PREFIXES = ("SELECT", "WITH", "EXPLAIN")

# Typing raw SELECT statements into the chat box is handy on your own machine
# and a liability on a public site. Set ALLOW_RAW_SQL=0 there; downloads keep
# working either way, because the export endpoint accepts a query only when it
# carries the signature this server gave out with the answer.
ALLOW_RAW_SQL = os.environ.get("ALLOW_RAW_SQL", "1").lower() not in ("0", "false", "no")

_EXPORT_SECRET = (os.environ.get("SECRET_KEY") or uuid.uuid4().hex).encode("utf-8")


def sign_sql(sql):
    return hmac.new(_EXPORT_SECRET, (sql or "").encode("utf-8"),
                    hashlib.sha256).hexdigest()


def signature_ok(sql, sig):
    return bool(sig) and hmac.compare_digest(sign_sql(sql), str(sig))


def execute_sql(dbname, sql):
    clean = sql.strip().upper()
    if not clean.startswith(READ_ONLY_PREFIXES):
        return None, "Only SELECT / WITH / EXPLAIN queries are allowed."
    conn = None
    try:
        conn = get_connection(dbname)
        cur = conn.cursor()
        cur.execute(sql)
        if cur.description is None:
            return {"columns": [], "rows": [], "count": 0}, None
        col_names = [desc[0] for desc in cur.description]
        rows = []
        for row in cur.fetchall():
            rd = {}
            for i, val in enumerate(row):
                try:
                    json.dumps(val)
                    rd[col_names[i]] = val
                except (TypeError, ValueError):
                    rd[col_names[i]] = str(val)
            rows.append(rd)
        return {"columns": col_names, "rows": rows, "count": len(rows)}, None
    except Exception as e:
        return None, str(e)
    finally:
        if conn is not None:
            conn.close()


def safe_sql_string(val):
    """Escape single quotes for a SQL string literal"""
    return str(val).replace("'", "''")


def quote_ident(name):
    """Quote an identifier (table/column) safely"""
    return '"{}"'.format(str(name).replace('"', '""'))


# ============ EXPORT / DOWNLOAD ============
# Everything the page can show can also be downloaded: the rows behind an
# answer, or a whole table. CSV and JSON always; GeoJSON when the rows come
# from a table that has a geometry column, so the export reopens in QGIS.

EXPORT_FORMATS = ("csv", "json", "geojson", "shp")

# Formats that carry the geometry, so they only work on rows from a table
# that actually has a geometry column.
GEO_FORMATS = ("geojson", "shp")

GEOJSON_COL = "__geojson"

# GeoJSON and shapefiles are both written in WGS84 lon/lat. The data here is
# already 4326, but reprojecting anything else keeps a layer in another CRS
# from being shipped out mislabelled - a silent error worth a cheap CASE.
GEOM_AS_GEOJSON = ("ST_AsGeoJSON(CASE WHEN ST_SRID({g}) IN (0, 4326) "
                   "THEN {g} ELSE ST_Transform({g}, 4326) END) AS {a}")

# smart_ask caps its display query at 100 rows; an explicit "top 5" sets its own
# limit instead. Only the display cap is dropped on export - "top 5" must stay 5.
DISPLAY_LIMIT = 100

RE_TRAILING_LIMIT = re.compile(r'\s+LIMIT\s+(\d+)\s*;?\s*$', re.I)

RE_SELECT_FROM = re.compile(r'^\s*SELECT\s+(?P<cols>.+?)\s+FROM\s+(?P<rest>.+)$',
                            re.I | re.S)

RE_FIRST_TABLE = re.compile(r'^"(?P<schema>[^"]+)"\."(?P<table>[^"]+)"')


def drop_display_limit(sql):
    """Remove the automatic 'LIMIT 100' so an export gives every matching row."""
    m = RE_TRAILING_LIMIT.search(sql)
    if m and int(m.group(1)) == DISPLAY_LIMIT:
        return sql[:m.start()]
    return sql


def find_table(tables, name):
    """Look up a table by 'name' or 'schema.name'."""
    want = str(name).strip().lower()
    for t in tables:
        full = "{}.{}".format(t["schema"], t["table"]).lower()
        if want == full or want == t["table"].lower():
            return t
    return None


def geom_column(columns):
    for c in columns:
        if is_geom_col(c["name"]):
            return c["name"]
    return None


def geom_select(geom):
    return GEOM_AS_GEOJSON.format(g=quote_ident(geom), a=quote_ident(GEOJSON_COL))


def no_geometry(name, fmt):
    return "**{}** has no geometry column, so it cannot be exported as {}.".format(
        name, "a shapefile" if fmt == "shp" else "GeoJSON")


def table_export_sql(table, fmt):
    """SELECT for a whole table - geometry as GeoJSON text, or dropped."""
    geom = geom_column(table["columns"])
    sel = get_display_cols(table["columns"])
    if fmt in GEO_FORMATS:
        if not geom:
            return None, no_geometry(table["table"], fmt)
        sel = sel + [geom_select(geom)]
    return "SELECT {} FROM {}.{}".format(
        ", ".join(sel) or "*",
        quote_ident(table["schema"]), quote_ident(table["table"])), None


def answer_export_sql(sql, tables, fmt):
    """Re-point an answer's SQL at an export.

    For GeoJSON the geometry has to be added back - smart_ask leaves it out of
    the SELECT because it is unreadable in a table - so the column list is
    rebuilt while the FROM/WHERE/ORDER BY tail is kept exactly as it was.
    """
    sql = drop_display_limit(sql)
    if fmt not in GEO_FORMATS:
        return sql, None

    label = "a shapefile" if fmt == "shp" else "GeoJSON"
    nope = "That answer cannot be exported as {} - try CSV or JSON.".format(label)

    # A summary or a list of distinct values is not a set of map features, and
    # attaching geometry to it would silently change the row count.
    if re.search(r'\bGROUP\s+BY\b', sql, re.I) or re.match(r'^\s*SELECT\s+DISTINCT\b', sql, re.I):
        return None, ("That answer is a summary, not map features - "
                      "export it as CSV or JSON instead.")

    m = RE_SELECT_FROM.match(sql)
    if not m:
        return None, nope
    tm = RE_FIRST_TABLE.match(m.group("rest").strip())
    if not tm:
        return None, nope

    table = find_table(tables, "{}.{}".format(tm.group("schema"), tm.group("table")))
    if not table:
        return None, nope
    geom = geom_column(table["columns"])
    if not geom:
        return None, no_geometry(table["table"], fmt)

    # Keep the answer's own column list, so the file holds exactly the columns
    # the table on screen showed - just with the geometry added back.
    sel = "{}, {}".format(m.group("cols").strip(), geom_select(geom))
    return "SELECT {} FROM {}".format(sel, m.group("rest")), None


def export_value(v):
    """Anything psycopg2 hands back, as something CSV and JSON can carry."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.datetime, datetime.time)):
        return v.isoformat()
    return str(v)


def stream_rows(dbname, sql, chunk=2000):
    """Yield rows, pulling them from the server a chunk at a time so a big
    table never has to fit in memory. Column names come from probe_columns -
    a server-side cursor leaves .description empty until rows arrive."""
    conn = get_connection(dbname)
    try:
        cur = conn.cursor(name="chatbox_export")
        cur.itersize = chunk
        cur.execute(sql)
        for row in cur:
            yield row
        cur.close()
    finally:
        conn.close()


def probe_columns(dbname, sql):
    """Run the query with no rows: proves it works and names the columns,
    before the browser has been told a download is on its way."""
    res, err = execute_sql(dbname, "SELECT * FROM ({}) _probe LIMIT 0".format(sql))
    if err:
        return None, err
    return res["columns"], None


def csv_body(dbname, sql, cols):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    def take():
        out = buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        return out

    yield u"﻿"     # byte-order mark, so Excel opens UTF-8 names correctly
    writer.writerow(cols)
    yield take()
    for row in stream_rows(dbname, sql):
        writer.writerow([export_value(v) for v in row])
        yield take()


def json_body(dbname, sql, cols):
    yield "[\n"
    first = True
    for row in stream_rows(dbname, sql):
        rec = {c: export_value(v) for c, v in zip(cols, row)}
        yield ("" if first else ",\n") + json.dumps(rec, ensure_ascii=False)
        first = False
    yield "\n]\n"


def geojson_body(dbname, sql, cols):
    yield '{"type":"FeatureCollection","features":[\n'
    first = True
    for row in stream_rows(dbname, sql):
        props = {}
        geom = None
        for c, v in zip(cols, row):
            if c == GEOJSON_COL:
                geom = v
            else:
                props[c] = export_value(v)
        feature = '{"type":"Feature","geometry":' + (geom or "null") + \
                  ',"properties":' + json.dumps(props, ensure_ascii=False) + '}'
        yield ("" if first else ",\n") + feature
        first = False
    yield "\n]}\n"


def shapefile_zip(dbname, sql, cols, stem):
    """A real shapefile set - .shp/.shx/.dbf/.prj/.cpg - bundled into one zip,
    because a shapefile is never a single file.

    Returns (bytes, error). Unlike the other formats this one is built whole in
    memory: a shapefile has to know its full extent before it can be written.
    """
    try:
        import geopandas as gpd
        from shapely.geometry import shape as to_shape
    except ImportError:
        return None, ("Shapefile export needs geopandas and shapely on the "
                      "server - install them with: pip install geopandas")

    records = []
    geoms = []
    for row in stream_rows(dbname, sql):
        props = {}
        geom = None
        for c, v in zip(cols, row):
            if c == GEOJSON_COL:
                geom = v
            else:
                props[c] = export_value(v)
        records.append(props)
        geoms.append(to_shape(json.loads(geom)) if geom else None)

    if not records:
        return None, "Nothing to export - that query matched no rows."
    if not any(g is not None for g in geoms):
        return None, "Those rows have no geometry, so there is no shape to write."

    gdf = gpd.GeoDataFrame(records, geometry=geoms, crs="EPSG:4326")

    tmp = tempfile.mkdtemp(prefix="chatbox_shp_")
    try:
        base = safe_filename(stem)
        gdf.to_file(os.path.join(tmp, base + ".shp"),
                    driver="ESRI Shapefile", encoding="utf-8")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for part in sorted(os.listdir(tmp)):
                z.write(os.path.join(tmp, part), part)
        return buf.getvalue(), None
    except Exception as e:
        return None, "Could not write the shapefile: " + str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def safe_filename(name):
    base = re.sub(r'[^A-Za-z0-9._-]+', '_', str(name)).strip('_')
    return base or "export"


# ============ NATURAL LANGUAGE ENGINE ============
# The question is matched against three things at once: table names, column
# names, and the actual values in the data. Matching is fuzzy, so "kucing"
# still finds "Kuching" and "stations" still finds "rainfall_stations".
# The last answer is remembered so follow-ups like "what were they" work.

STOPWORDS = set("""
a an the of in at on for from to by with and or is are was were be been being am
show me list get see display give tell find search look up query
i we you my our your want need know please thanks thank
which what where who whose when why how there here it its this that these those
them they their he she him her do does did can could would should will shall
have has had many much number numbers amount count counts total totals
data info information detail details record records row rows entry entries
item items result results table tables database db
about near around like named called equal equals value values
""".split())

RE_COUNT = re.compile(r'\b(how many|how much|count|number of|total)\b')
RE_GROUP = re.compile(r'\b(per|each|every|by group|group by|grouped by|breakdown|broken down)\b')
RE_UNIQUE = re.compile(r'\b(unique|distinct|different|kinds? of|types? of|what|which|list|all)\b')
RE_COLUMNS = re.compile(r'\b(column|columns|field|fields|structure|describe|schema|headers?)\b')
RE_TOPN = re.compile(r'\b(top|first|bottom|last)\b')
RE_MOST = re.compile(r'\b(most|highest|largest|biggest|greatest|top|maximum|max)\b')
RE_LEAST = re.compile(r'\b(least|fewest|lowest|smallest|minimum|min)\b')
RE_WHICH = re.compile(r'\b(which|what|who)\b')
RE_ORDER = re.compile(r'\b(?:sort|sorted|order|ordered|arrange|arranged|rank|ranked)\s+(?:by|on)\s+([a-z0-9_ ]{2,40})')
RE_DESC = re.compile(r'\b(desc|descending|highest first|biggest first|largest first|reverse)\b')
RE_NULL = re.compile(r'\b(?:without|missing|empty|blank|null|no)\s+([a-z0-9_ ]{2,30})')
RE_NOT = re.compile(r'\b(?:not|except|excluding|other than|apart from|besides)\b')
RE_BETWEEN = re.compile(r'between\s+(-?\d+(?:\.\d+)?)\s+and\s+(-?\d+(?:\.\d+)?)')
RE_OVERVIEW = re.compile(
    r'(list tables|show tables|what tables|which tables|how many tables'
    r'|what data do you have|what can i ask|what can you do|what do you have'
    r'|whats in (?:this|the) database|overview|^help$)')
# "more" is deliberately absent - it belongs to "more than 500", not to a
# follow-up. Only the explicit "show more" counts.
RE_FOLLOWUP = re.compile(
    r'\b(them|they|those|these|it|ones|same|again|the rest|others'
    r'|what about|how about|and for|and in|show more)\b')

# Only these words mean "put the actual rows on screen". Everything else gets
# a one-line answer, and the user can follow up with "show them".
RE_SHOW = re.compile(
    r'\b(show|list|display|view|see|print|give me|name them|table|rows'
    r'|details?|which ones|what are|are there|all of them|everything)\b')

SHOW_HINT = '  \n*Say "show them" to see the rows.*'

AGG_WORDS = [
    (re.compile(r'\b(average|avg|mean)\b'), 'AVG'),
    (re.compile(r'\b(maximum|max|highest|largest|biggest|greatest|furthest|farthest)\b'), 'MAX'),
    (re.compile(r'\b(minimum|min|lowest|smallest|least|nearest|closest)\b'), 'MIN'),
    (re.compile(r'\b(sum|sum of|added up|add up)\b'), 'SUM'),
]

CMP_OPS = [
    (r'(?:>=|at least|not less than|minimum of|no less than)', '>='),
    (r'(?:<=|at most|not more than|maximum of|no more than)', '<='),
    (r'(?:>|more than|greater than|above|over|bigger than|larger than|higher than|exceeds|exceeding)', '>'),
    (r'(?:<|less than|fewer than|below|under|smaller than|lower than)', '<'),
]

TABLE_T = 0.80   # match thresholds
COL_T = 0.82
VALUE_T = 0.84
MAX_IN_VALUES = 50

# Remembers the last answer so follow-up questions ("show them", "what about
# Miri") make sense. Keyed by visitor and database, so two people using the
# deployed site at once cannot answer each other's follow-ups.
_context = {}

# Nothing here is worth keeping forever - drop the lot once it grows large
# rather than leaking a slot per visitor.
MAX_CONTEXTS = 2000


def norm_text(s):
    """Lowercase, strip punctuation/underscores so 'rainfall_stations' == 'rainfall stations'"""
    return re.sub(r'[^a-z0-9]+', ' ', str(s).lower()).strip()


def sim(a, b):
    """Similarity 0..1 that tolerates typos and partial words."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    # Cheap reject: wildly different lengths and no containment
    if a not in b and b not in a and min(la, lb) * 2 < max(la, lb):
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if la >= 4 and lb >= 4 and (a in b or b in a):
        return max(ratio, 0.88 + 0.10 * (min(la, lb) / float(max(la, lb))))
    return ratio


def _variants(p):
    """Try the phrase as-is, singular, and plural."""
    v = [p]
    if p.endswith('s') and len(p) > 3:
        v.append(p[:-1])
    else:
        v.append(p + 's')
    return v


def best_sim(phrase, target):
    tn = norm_text(target)
    return max(sim(pv, tn) for pv in _variants(phrase))


def gen_phrases(question, max_words=4):
    """Every 1..4-word phrase with its token span, longest first."""
    toks = norm_text(question).split()
    out = []
    for n in range(min(max_words, len(toks)), 0, -1):
        for i in range(len(toks) - n + 1):
            part = toks[i:i + n]
            # A useful phrase never starts or ends on a filler word
            if part[0] in STOPWORDS or part[-1] in STOPWORDS:
                continue
            out.append((" ".join(part), (i, i + n)))
    return toks, out


def _norm_values(t):
    """Normalised copy of the value index, cached on the table dict."""
    nv = t.get("_nvalues")
    if nv is None:
        nv = {}
        for cname, vals in t.get("values", {}).items():
            nv[cname] = [(norm_text(v), v) for v in vals]
        t["_nvalues"] = nv
    return nv


def collect_hits(question, tables, forced=None):
    """Match every phrase in the question against tables, columns and values."""
    toks, phrases = gen_phrases(question)
    for f in (forced or []):
        phrases.append((norm_text(f), None))

    hits = []
    for phrase, span in phrases:
        if len(phrase) < 2:
            continue

        for t in tables:
            s = best_sim(phrase, t["table"])
            if s >= TABLE_T:
                hits.append({"kind": "table", "score": s, "span": span,
                             "phrase": phrase, "table": t})

            for c in t["columns"]:
                if is_geom_col(c["name"]):
                    continue
                s = best_sim(phrase, c["name"])
                if s >= COL_T:
                    # Shapefiles leave behind numeric twins like district_f
                    # next to the real district column - prefer an exact name,
                    # and prefer a column that actually holds readable values.
                    spec = (1.0 if norm_text(c["name"]) in _variants(phrase) else 0.0)
                    if c["name"] in t.get("values", {}):
                        spec += 0.5
                    hits.append({"kind": "column", "score": s, "span": span,
                                 "phrase": phrase, "table": t, "column": c,
                                 "spec": spec})

            if len(phrase) < 3:
                continue
            for cname, pairs in _norm_values(t).items():
                best, matched = 0.0, []
                for nv, orig in pairs:
                    s = max(sim(pv, nv) for pv in _variants(phrase))
                    if s >= VALUE_T:
                        matched.append(orig)
                        best = max(best, s)
                if matched:
                    exact = [m for m in matched if norm_text(m) == phrase]
                    hits.append({"kind": "value", "score": best, "span": span,
                                 "phrase": phrase, "table": t, "column_name": cname,
                                 "values": exact or matched,
                                 # A value found in a short list of categories
                                 # (division, basin) is what people filter on,
                                 # more so than one buried among 345 names.
                                 "spec": 1 if len(pairs) <= 25 else 0,
                                 "display": (exact or matched)[0]})

    # Longer phrases and better scores win; a value hit outranks a bare
    # column/table hit on the same words because it is far more specific.
    # Scores are rounded so that a near-tie is settled by kind, not by noise.
    kind_rank = {"value": 2, "table": 1, "column": 0}
    hits.sort(key=lambda h: (round(h["score"], 1),
                             (h["span"][1] - h["span"][0]) if h["span"] else 9,
                             kind_rank[h["kind"]],
                             h.get("spec", 0),
                             h["score"]), reverse=True)

    accepted, used = [], set()
    for h in hits:
        if h["span"] is not None:
            cells = set(range(h["span"][0], h["span"][1]))
            if cells & used:
                continue
            used |= cells
        accepted.append(h)
    return toks, accepted, used


HIT_WEIGHT = {"table": 3.0, "column": 2.5, "value": 1.5}


def pick_table(accepted, tables):
    """Vote: the table the question refers to most strongly, across all hits."""
    if not accepted:
        return tables[0] if len(tables) == 1 else None
    scores = {}
    for h in accepted:
        w = HIT_WEIGHT[h["kind"]] + (0.5 if h.get("spec") else 0.0)
        key = id(h["table"])
        scores[key] = scores.get(key, [0.0, h["table"]])
        scores[key][0] += w * h["score"]
    return max(scores.values(), key=lambda x: x[0])[1]


def find_column(phrase, table, numeric_only=False):
    """Best column of one table for a loose phrase like 'shape area'."""
    phrase = norm_text(phrase).strip()
    best, score = None, COL_T
    for c in table["columns"]:
        if is_geom_col(c["name"]):
            continue
        if numeric_only and not is_numeric_type(c["type"]):
            continue
        s = best_sim(phrase, c["name"])
        if s >= score:
            best, score = c, s
    return best


# A shapefile attribute table usually carries the same thing twice: a code
# (subbasin = "001") and the readable name beside it (subbasin_n = "Rajang 1").
# When someone says "subbasin" they mean the name, so that is what to answer
# with - unless they asked for the code, or named another column outright.
NAME_SUFFIXES = ("_n", "_nm", "_name")

RE_CODE_WORDS = re.compile(r'\b(code|codes|id|ids|number|numbers|no)\b', re.I)


def name_twin(column, table):
    """The readable twin of a code column: subbasin -> subbasin_n.

    Matched on the raw column names - norm_text turns '_' into a space, which
    would stop 'subbasin' + '_n' from ever lining up with 'subbasin_n'.
    """
    base = str(column["name"]).lower()
    for suf in NAME_SUFFIXES:
        for c in table["columns"]:
            if str(c["name"]).lower() == base + suf and is_text_type(c["type"]):
                return c
    return None


def spelled_column(column, table, raw):
    """A sibling column the question named outright: 'list all subbasin_a'.

    The tokeniser cannot see these - it splits on '_' and then throws the
    phrase away because it ends on the stop word "a" - so the raw question
    text is checked directly.
    """
    base = str(column["name"]).lower() + "_"
    for c in table["columns"]:
        cn = str(c["name"]).lower()
        if cn.startswith(base) and re.search(r'\b{}\b'.format(re.escape(cn)), raw):
            return c
    return None


def prefer_names(free_hits, table, question, raw=""):
    """Swap each matched code column for its name twin.

    Naming a column outright still wins, and so does asking for the code.
    """
    if RE_CODE_WORDS.search(question):
        return free_hits
    raw = str(raw).lower()
    out, seen = [], set()
    for h in free_hits:
        col = spelled_column(h["column"], table, raw) or name_twin(h["column"], table) \
            or h["column"]
        if col["name"] in seen:
            continue
        seen.add(col["name"])
        out.append(h if col is h["column"] else dict(h, column=col))
    return out


def parse_comparisons(q, table):
    """'shape area more than 100', 'lat between 2 and 3' -> SQL conditions."""
    conds, notes, nums, cols = [], [], set(), set()
    for c in table["columns"]:
        if not is_numeric_type(c["type"]) or is_geom_col(c["name"]):
            continue
        cn = re.escape(norm_text(c["name"]))
        m = re.search(cn + r'\s*(?:is\s+)?' + RE_BETWEEN.pattern, q)
        if m:
            conds.append("{} BETWEEN {} AND {}".format(
                quote_ident(c["name"]), m.group(1), m.group(2)))
            notes.append("**{}** between {} and {}".format(c["name"], m.group(1), m.group(2)))
            nums.update([m.group(1), m.group(2)])
            cols.add(c["name"])
            continue
        for op_src, op in CMP_OPS:
            m = re.search(cn + r'\s*(?:is\s+)?' + op_src + r'\s*(-?\d+(?:\.\d+)?)', q)
            if m:
                conds.append("{} {} {}".format(quote_ident(c["name"]), op, m.group(1)))
                notes.append("**{}** {} {}".format(c["name"], op, m.group(1)))
                nums.add(m.group(1))
                cols.add(c["name"])
                break
    return conds, notes, nums, cols


def parse_nulls(q, table):
    """'stations without river basin' -> river_basin IS NULL"""
    conds, notes, cols = [], [], set()
    for m in RE_NULL.finditer(q):
        c = find_column(m.group(1), table)
        if c:
            conds.append("{} IS NULL".format(quote_ident(c["name"])))
            notes.append("**{}** is empty".format(c["name"]))
            cols.add(c["name"])
    return conds, notes, cols


def parse_order(q, table):
    """'sorted by lat' -> ORDER BY lat"""
    m = RE_ORDER.search(q)
    if not m:
        return None, None
    c = find_column(m.group(1), table)
    if not c:
        return None, None
    return c, ("DESC" if RE_DESC.search(q) or RE_MOST.search(q) else "ASC")


def build_where(filters, extra=None, negate=False):
    parts = []
    for f in filters:
        col = quote_ident(f["column"])
        vals = f["values"][:MAX_IN_VALUES]
        op_eq, op_in = ("<>", "NOT IN") if negate else ("=", "IN")
        if len(vals) == 1:
            parts.append("{} {} '{}'".format(col, op_eq, safe_sql_string(vals[0])))
        else:
            joined = ", ".join("'{}'".format(safe_sql_string(v)) for v in vals)
            parts.append("{} {} ({})".format(col, op_in, joined))
    parts.extend(extra or [])
    return (" WHERE " + " AND ".join(parts)) if parts else ""


def describe_filters(filters, extra_notes=None, negate=False):
    bits = []
    word = "is not" if negate else "is"
    for f in filters:
        if len(f["values"]) == 1:
            bits.append('**{}** {} "{}"'.format(f["column"], word, f["values"][0]))
        else:
            bits.append('**{}** {} one of {} values ("{}", ...)'.format(
                f["column"], word, len(f["values"]), f["values"][0]))
    bits.extend(extra_notes or [])
    return " and ".join(bits)


def spelling_note(filters):
    """Tell the user when we corrected their spelling."""
    notes = []
    for f in filters:
        if len(f["values"]) == 1 and norm_text(f["values"][0]) != f.get("phrase"):
            notes.append('understood "{}" as "{}"'.format(f["phrase"], f["values"][0]))
    return "  \n*({})*".format(", ".join(notes)) if notes else ""


def help_text(tables, lead=None):
    """Show what can actually be asked about, built from the real data."""
    lines = [lead or "I couldn't work out what you're asking. Here's what this database holds:", ""]
    for t in tables[:6]:
        cats = []
        for cname, vals in list(t.get("values", {}).items()):
            if 1 < len(vals) <= 25:
                cats.append("**{}** ({})".format(cname, ", ".join(vals[:4]) +
                                                 (", ..." if len(vals) > 4 else "")))
        lines.append("**{}** - {} rows".format(t["table"], t["rows"]))
        if cats:
            lines.append("  can be filtered by: " + "; ".join(cats[:3]))
    ex_table = tables[0]["table"]
    ex_col, ex_val = None, None
    for t in tables:
        for cname, vals in t.get("values", {}).items():
            if 1 < len(vals) <= 25:
                ex_table, ex_col, ex_val = t["table"], cname, vals[0]
                break
        if ex_col:
            break
    lines.append("")
    lines.append("Try asking:")
    lines.append('- "how many {}"'.format(ex_table))
    if ex_col:
        lines.append('- "{} in {}"'.format(ex_table, ex_val))
        lines.append('- "how many {} per {}"'.format(ex_table, ex_col))
        lines.append('- "which {} has the most {}"'.format(ex_col, ex_table))
        lines.append('- "list all {}"'.format(ex_col))
    lines.append('- "columns of {}"'.format(ex_table))
    return "\n".join(lines)


def get_display_cols(columns):
    """Get column references for SELECT, excluding geometry"""
    return [quote_ident(c["name"]) for c in columns if not is_geom_col(c["name"])]


def group_target(toks, free_hits):
    """In 'stations per division' the column after 'per' is the one to group on."""
    anchors = [i + 1 for i, t in enumerate(toks)
               if t in ("per", "by", "each", "every")]
    for a in anchors:
        for h in free_hits:
            if h["span"] and h["span"][0] >= a:
                return h["column"]
    return free_hits[0]["column"]


def fmt_val(v):
    """Trim 959094495.422184905078377 down to something a person can read.
    Numerics arrive as strings here - execute_sql stringifies Decimal because
    it is not JSON-serialisable - so parse those back before formatting."""
    num = None
    if isinstance(v, bool) or v is None:
        return str(v)
    if isinstance(v, float):
        num = v
    elif isinstance(v, decimal.Decimal):
        num = float(v)
    else:
        s = str(v)
        if re.match(r'^-?\d+\.\d{3,}$', s):
            try:
                num = float(s)
            except ValueError:
                pass
        if num is None:
            return s
    if abs(num) >= 1000:
        return '{:,.0f}'.format(num)
    return '{:g}'.format(float('{:.6g}'.format(num)))


def inline_list(vals, limit=15):
    """Values as a sentence: 'Kuching, Miri, Sibu, ...'"""
    out = ", ".join(fmt_val(v) for v in vals[:limit])
    return out + (", ..." if len(vals) > limit else "")


def inline_pairs(rows, keycol, valcol, limit=12):
    """Grouped counts as a sentence: 'Kuching (53), Miri (52), ...'"""
    out = ", ".join("{} ({})".format(fmt_val(r[keycol]), fmt_val(r[valcol]))
                    for r in rows[:limit])
    return out + (", ..." if len(rows) > limit else "")


def true_total(dbname, full, where, shown, limit):
    """When a LIMIT truncated the result, find out how many rows really match,
    so the reply never claims 100 when there are 292."""
    if shown < limit:
        return shown
    res, err = execute_sql(dbname, "SELECT COUNT(*) AS total FROM {}{}".format(full, where))
    if err or not res["rows"]:
        return shown
    return res["rows"][0]["total"]


def rows_phrase(total, shown, tname, wtxt):
    if total > shown:
        return 'Found **{}** record(s) in **{}**{} - showing the first **{}**:'.format(
            total, tname, wtxt, shown)
    return 'Found **{}** record(s) in **{}**{}:'.format(total, tname, wtxt)


def remember(key, table, intent, column=None, filters=None, extra=None,
             notes=None, negate=False):
    if len(_context) > MAX_CONTEXTS:
        _context.clear()
    _context[key] = {"table": table, "intent": intent, "column": column,
                     "filters": filters or [], "extra": extra or [],
                     "notes": notes or [], "negate": negate}


def smart_ask(question, dbname, tables, session=""):
    ctx_key = (session, dbname)
    q_orig = question.strip()
    q = norm_text(q_orig)

    if not tables:
        return "No tables found in this database.", None, None, None

    # ---- OVERVIEW / HELP ----
    if RE_OVERVIEW.search(q):
        return help_text(tables, "This database has {} table(s):".format(len(tables))), None, None, None

    # Anything in quotes is taken literally as a value to look for
    forced = re.findall(r'["\']([^"\']{2,60})["\']', q_orig)

    toks, accepted, used = collect_hits(q_orig, tables, forced)
    table = pick_table(accepted, tables)
    ctx = _context.get(ctx_key)
    following = bool(RE_FOLLOWUP.search(q)) and ctx is not None

    # ---- COLUMNS / STRUCTURE ----
    if RE_COLUMNS.search(q):
        t = table or (ctx["table"] if ctx else tables[0])
        sql = ("SELECT column_name, data_type FROM information_schema.columns "
               "WHERE table_schema = '{}' AND table_name = '{}' "
               "ORDER BY ordinal_position".format(
                   safe_sql_string(t["schema"]), safe_sql_string(t["table"])))
        res, err = execute_sql(dbname, sql)
        if err:
            return "Error: " + err, sql, None, None
        remember(ctx_key, t, "columns")
        return "Here are the columns in **{}**:".format(t["table"]), sql, res, None

    # A follow-up with no table of its own carries on with the last one
    if table is None and following:
        table = ctx["table"]
    if table is None:
        return help_text(tables), None, None, None

    # Re-match against the chosen table alone. A word like "sibu" can appear in
    # several tables; now that we know which one the question is about, it must
    # resolve inside that one.
    toks, mine, used = collect_hits(q_orig, [table], forced)
    filters, free_hits = [], []
    seen_cols = set()
    for h in mine:
        if h["kind"] == "value" and h["column_name"] not in seen_cols:
            seen_cols.add(h["column_name"])
            filters.append({"column": h["column_name"], "values": h["values"],
                            "phrase": h["phrase"]})
    for h in mine:
        if h["kind"] == "column" and h["column"]["name"] not in seen_cols:
            seen_cols.add(h["column"]["name"])
            free_hits.append(h)
    free_cols = [h["column"] for h in free_hits]

    cmp_conds, cmp_notes, cmp_nums, cmp_cols = parse_comparisons(q, table)
    null_conds, null_notes, null_cols = parse_nulls(q, table)
    extra = cmp_conds + null_conds
    extra_notes = cmp_notes + null_notes
    negate = bool(RE_NOT.search(q)) and bool(filters)

    # A column already spoken for by "lat above 4" or "sorted by name" was not
    # the user asking to see that column on its own.
    order_col, order_dir = parse_order(q, table)
    consumed = cmp_cols | null_cols | ({order_col["name"]} if order_col else set())
    free_hits = [h for h in free_hits if h["column"]["name"] not in consumed]
    free_hits = prefer_names(free_hits, table, q, q_orig)
    free_cols = [h["column"] for h in free_hits]

    # ---- FOLLOW-UP with nothing new to say: expand the previous answer ----
    if following and not filters and not free_cols and not extra and table is ctx["table"]:
        prev, pcol = ctx["intent"], ctx.get("column")
        pfilters, pextra = ctx["filters"], ctx["extra"]
        pneg = ctx.get("negate", False)
        full = "{}.{}".format(quote_ident(table["schema"]), quote_ident(table["table"]))
        pwhere = build_where(pfilters, pextra, pneg)
        if prev in ("count_distinct", "distinct") and pcol:
            sql = ("SELECT DISTINCT {c} FROM {t} WHERE {c} IS NOT NULL "
                   "ORDER BY 1 LIMIT 500".format(c=quote_ident(pcol), t=full))
            res, err = execute_sql(dbname, sql)
            if err:
                return "Error: " + err, sql, None, None
            remember(ctx_key, table, "distinct", pcol, pfilters, pextra, ctx["notes"], pneg)
            return 'Those **{}** **{}** values are:'.format(
                res["count"], pcol), sql, res, None

        # "show them" after a breakdown means show the breakdown, not raw rows
        if prev == "group" and pcol:
            sql = ("SELECT {c}, COUNT(*) AS total FROM {t}{w} "
                   "GROUP BY 1 ORDER BY 2 DESC LIMIT 200".format(
                       c=quote_ident(pcol), t=full, w=pwhere))
            res, err = execute_sql(dbname, sql)
            if err:
                return "Error: " + err, sql, None, None
            remember(ctx_key, table, "group", pcol, pfilters, pextra, ctx["notes"], pneg)
            return '**{}** broken down by **{}**:'.format(
                table["table"], pcol), sql, res, None
        # Everything else: show the rows behind the previous answer
        where = build_where(pfilters, pextra, pneg)
        sel = ", ".join(get_display_cols(table["columns"])) or "*"
        sql = "SELECT {} FROM {}{} LIMIT 100".format(sel, full, where)
        res, err = execute_sql(dbname, sql)
        if err:
            return "Error: " + err, sql, None, None
        fd = describe_filters(pfilters, ctx["notes"], pneg)
        pw = (" where " + fd) if fd else ""
        total = true_total(dbname, full, where, res["count"], 100)
        remember(ctx_key, table, "rows", pcol, pfilters, pextra, ctx["notes"], pneg)
        return rows_phrase(total, res["count"], table["table"], pw), sql, res, None

    # A follow-up like "what about miri" keeps the previous kind of question
    inherit = None
    if following and ctx and table is ctx["table"] and (filters or extra):
        inherit = ctx["intent"]

    full = "{}.{}".format(quote_ident(table["schema"]), quote_ident(table["table"]))
    where = build_where(filters, extra, negate)
    fdesc = describe_filters(filters, extra_notes, negate)
    note = spelling_note(filters)
    tname = table["table"]
    wtxt = (" where " + fdesc) if fdesc else ""

    # A number in the question is a row limit only if it wasn't matched as data
    limit = None
    for i, tk in enumerate(toks):
        if tk.isdigit() and i not in used and tk not in cmp_nums:
            limit = max(1, min(int(tk), 500))
            break

    is_count = bool(RE_COUNT.search(q)) or inherit in ("count", "count_distinct")
    is_group = bool(RE_GROUP.search(q)) or inherit == "group"
    # "top 5 ..." and "sorted by ..." are themselves requests for a list
    show_rows = (bool(RE_SHOW.search(q)) or bool(RE_TOPN.search(q))
                 or order_col is not None)
    agg = None
    for rx, fn in AGG_WORDS:
        if rx.search(q):
            agg = fn
            break
    num_cols = [c for c in free_cols if is_numeric_type(c["type"])]

    # ---- "which division has the most stations" ----
    superlative = RE_MOST.search(q) or RE_LEAST.search(q)
    if RE_WHICH.search(q) and superlative and free_cols and not num_cols and not is_group:
        cn = free_cols[0]["name"]
        least = bool(RE_LEAST.search(q))
        sql = ("SELECT {c}, COUNT(*) AS total FROM {t}{w} "
               "GROUP BY 1 ORDER BY 2 {d} LIMIT 200".format(
                   c=quote_ident(cn), t=full, w=where, d="ASC" if least else "DESC"))
        res, err = execute_sql(dbname, sql)
        if err:
            return "Error: " + err, sql, None, None
        remember(ctx_key, table, "group", cn, filters, extra, extra_notes, negate)
        if not res["rows"]:
            return "No rows to compare.", sql, None, None
        top = res["rows"][0]
        head = '**{}** has the {} rows in **{}**, **{}** of them{}.'.format(
            top[cn], "fewest" if least else "most", tname, top["total"], wtxt)
        if not show_rows:
            return (head + note + SHOW_HINT), sql, None, None
        return (head + " Full ranking:" + note), sql, res, str(top[cn])

    # ---- TOP N / superlative row: "top 5 stations by lat", "highest lat" ----
    if agg in ("MAX", "MIN") and num_cols and (limit or RE_TOPN.search(q) or RE_WHICH.search(q)) \
            and not is_count and not is_group:
        col = quote_ident(num_cols[0]["name"])
        order = "DESC" if agg == "MAX" else "ASC"
        sel = ", ".join(get_display_cols(table["columns"])) or "*"
        n = limit or (1 if RE_WHICH.search(q) and not RE_TOPN.search(q) else 10)
        sql = "SELECT {} FROM {}{} ORDER BY {} {} LIMIT {}".format(sel, full, where, col, order, n)
        res, err = execute_sql(dbname, sql)
        if err:
            return "Error: " + err, sql, None, None
        remember(ctx_key, table, "topn", num_cols[0]["name"], filters, extra, extra_notes, negate)
        return ("Top **{}** from **{}** by **{}** ({} first){}:".format(
            res["count"], tname, num_cols[0]["name"],
            "highest" if agg == "MAX" else "lowest", wtxt) + note), sql, res, None

    # ---- GROUPED AGGREGATE: "average shape area per district" ----
    if is_group and agg and num_cols:
        gcands = [h for h in free_hits if not is_numeric_type(h["column"]["type"])]
        if gcands:
            gcol = group_target(toks, gcands)
            cn = num_cols[0]["name"]
            alias = agg.lower() + "_" + cn
            sql = ("SELECT {g}, {a}({c}) AS {al} FROM {t}{w} "
                   "GROUP BY 1 ORDER BY 2 DESC LIMIT 200".format(
                       g=quote_ident(gcol["name"]), a=agg, c=quote_ident(cn),
                       al=quote_ident(alias), t=full, w=where))
            res, err = execute_sql(dbname, sql)
            if err:
                return "Error: " + err, sql, None, None
            remember(ctx_key, table, "group", gcol["name"], filters, extra, extra_notes, negate)
            head = "**{}** of **{}** for each **{}** in **{}**{}".format(
                agg.lower(), cn, gcol["name"], tname, wtxt)
            if not show_rows:
                return (head + ": " + inline_pairs(res["rows"], gcol["name"], alias)
                        + note + SHOW_HINT), sql, None, None
            return (head + ":" + note), sql, res, None

    # ---- AGGREGATE: "average lat", "total shape area" ----
    if agg and num_cols:
        cn = num_cols[0]["name"]
        sql = "SELECT {}({}) AS {} FROM {}{}".format(
            agg, quote_ident(cn), quote_ident(agg.lower() + "_" + cn), full, where)
        res, err = execute_sql(dbname, sql)
        if err:
            return "Error: " + err, sql, None, None
        val = list(res["rows"][0].values())[0] if res["rows"] else None
        remember(ctx_key, table, "agg", cn, filters, extra, extra_notes, negate)
        return ("The **{}** of **{}** in **{}**{} is **{}**.".format(
            agg.lower(), cn, tname, wtxt, fmt_val(val)) + note), sql, None, None

    # ---- GROUP BY: "how many stations per division" ----
    if is_group and free_cols:
        gcol = group_target(toks, free_hits)
        col = quote_ident(gcol["name"])
        sql = ("SELECT {c}, COUNT(*) AS total FROM {t}{w} "
               "GROUP BY 1 ORDER BY 2 DESC LIMIT 200".format(c=col, t=full, w=where))
        res, err = execute_sql(dbname, sql)
        if err:
            return "Error: " + err, sql, None, None
        remember(ctx_key, table, "group", gcol["name"], filters, extra, extra_notes, negate)
        head = "**{}** broken down by **{}**{}".format(tname, gcol["name"], wtxt)
        if not show_rows:
            return (head + ": " + inline_pairs(res["rows"], gcol["name"], "total")
                    + note + SHOW_HINT), sql, None, None
        return (head + ":" + note), sql, res, None

    # ---- COUNT ----
    if is_count:
        if free_cols and not filters and not extra and inherit != "count":
            # "how many divisions" means how many different ones
            cn = free_cols[0]["name"]
            sql = "SELECT COUNT(DISTINCT {}) AS total FROM {}".format(quote_ident(cn), full)
            res, err = execute_sql(dbname, sql)
            if err:
                return "Error: " + err, sql, None, None
            n = res["rows"][0]["total"] if res["rows"] else 0
            remember(ctx_key, table, "count_distinct", cn)
            return 'There are **{}** different **{}** values in **{}**.'.format(
                n, cn, tname), sql, None, None
        sql = "SELECT COUNT(*) AS total FROM {}{}".format(full, where)
        res, err = execute_sql(dbname, sql)
        if err:
            return "Error: " + err, sql, None, None
        n = res["rows"][0]["total"] if res["rows"] else 0
        remember(ctx_key, table, "count", None, filters, extra, extra_notes, negate)
        if fdesc:
            return ('Found **{}** record(s) in **{}** where {}.'.format(n, tname, fdesc)
                    + note), sql, None, None
        return '**{}** has **{}** records.'.format(tname, n), sql, None, None

    # ---- DISTINCT VALUES: "list all divisions" ----
    if free_cols and not filters and not extra and RE_UNIQUE.search(q):
        cn = free_cols[0]["name"]
        sql = ("SELECT DISTINCT {c} FROM {t} WHERE {c} IS NOT NULL "
               "ORDER BY 1 LIMIT 500".format(c=quote_ident(cn), t=full))
        res, err = execute_sql(dbname, sql)
        if err:
            return "Error: " + err, sql, None, None
        remember(ctx_key, table, "distinct", cn)
        head = 'There are **{}** different **{}** values in **{}**'.format(
            res["count"], cn, tname)
        if not show_rows:
            vals = [list(r.values())[0] for r in res["rows"]]
            return (head + ": " + inline_list(vals)), sql, None, None
        return head + ":", sql, res, None

    # ---- ROWS (optionally filtered, narrowed, sorted) ----
    # Only trim the columns shown when the user actually listed several of
    # them ("station name and division") - one passing mention is not a request
    # to hide everything else.
    sel = ""
    if len(free_cols) >= 2:
        sel = ", ".join(quote_ident(c["name"]) for c in free_cols)
    if not sel:
        sel = ", ".join(get_display_cols(table["columns"])) or "*"
    order = ""
    if order_col:
        order = " ORDER BY {} {}".format(quote_ident(order_col["name"]), order_dir)
    sql = "SELECT {} FROM {}{}{} LIMIT {}".format(sel, full, where, order, limit or 100)
    res, err = execute_sql(dbname, sql)
    if err:
        return "Error: " + err, sql, None, None
    remember(ctx_key, table, "rows", None, filters, extra, extra_notes, negate)
    if not res["rows"]:
        return ('No records in **{}**{}.'.format(tname, wtxt) + note), sql, None, None
    hl = filters[0]["values"][0] if len(filters) == 1 and len(filters[0]["values"]) == 1 else None
    total = true_total(dbname, full, where, res["count"], limit or 100)

    # Default to a one-line answer - the rows only appear when asked for
    if not show_rows:
        if fdesc:
            return ('Found **{}** record(s) in **{}** where {}.'.format(total, tname, fdesc)
                    + note + SHOW_HINT), sql, None, None
        return ('**{}** has **{}** records.'.format(tname, total)
                + note + SHOW_HINT), sql, None, None

    if fdesc:
        return rows_phrase(total, res["count"], tname, wtxt) + note, sql, res, hl
    if total > res["count"]:
        return 'Showing the first **{}** of **{}** records in **{}**:'.format(
            res["count"], total, tname), sql, res, hl
    return 'Here are **{}** records from **{}**:'.format(
        res["count"], tname), sql, res, hl


# ============ HTML ============
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Database Chat</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=DM+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
<style>
:root{--bg:#0a0d12;--bgc:#12161e;--bg2:#181d28;--fg:#e4e8ef;--mt:#5e6d82;--ac:#00d68f;--ad:rgba(0,214,143,.1);--ag:rgba(0,214,143,.2);--ubg:#1a3a2e;--ubr:rgba(0,214,143,.25);--abg:#161b25;--abr:#1e2636;--dn:#ff4757;--wn:#ffaa00;--inf:#4da6ff;--bd:#1c2333;--rd:12px;--ui:'DM Sans',sans-serif;--mn:'JetBrains Mono',monospace}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:var(--ui);background:var(--bg);color:var(--fg);height:100vh;overflow:hidden;display:flex;flex-direction:column}
.hd{padding:12px 20px;border-bottom:1px solid var(--bd);display:flex;align-items:center;justify-content:space-between;flex-shrink:0;background:var(--bgc);gap:10px;flex-wrap:wrap}
.hd-l{display:flex;align-items:center;gap:10px}
.hd-l h1{font-size:1.05rem;font-weight:700;display:flex;align-items:center;gap:9px}
.hd-l .ic{width:30px;height:30px;background:var(--ad);border:1px solid var(--bd);border-radius:7px;display:flex;align-items:center;justify-content:center;color:var(--ac);font-size:.8rem}
.hd-r{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.dsel{padding:6px 28px 6px 10px;background:var(--bg);border:1px solid var(--bd);border-radius:6px;color:var(--fg);font-family:var(--mn);font-size:.78rem;outline:none;cursor:pointer;appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%235e6d82' stroke-width='2'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 8px center}
.dsel:focus{border-color:var(--ac)}.dsel option{background:var(--bgc);color:var(--fg)}
.pl{font-size:.68rem;padding:4px 9px;border-radius:5px;font-family:var(--mn);display:flex;align-items:center;gap:4px;background:rgba(0,214,143,.08);color:var(--ac);border:1px solid rgba(0,214,143,.2)}
.pl .dt{width:6px;height:6px;border-radius:50%;background:currentColor}
.ms{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:16px}
.ms::-webkit-scrollbar{width:5px}.ms::-webkit-scrollbar-thumb{background:var(--bd);border-radius:3px}
.mg{display:flex;gap:10px;max-width:88%;animation:fi .25s ease}
.mg.ur{align-self:flex-end;flex-direction:row-reverse}
.mg.sy{align-self:flex-start}
@keyframes fi{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.av{width:32px;height:32px;border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:.75rem;flex-shrink:0}
.mg.ur .av{background:var(--ad);color:var(--ac);border:1px solid var(--ubr)}
.mg.sy .av{background:var(--bg2);color:var(--inf);border:1px solid var(--bd)}
.bb{padding:13px 16px;border-radius:var(--rd);line-height:1.65;font-size:.88rem}
.mg.ur .bb{background:var(--ubg);border:1px solid var(--ubr);border-top-right-radius:4px}
.mg.sy .bb{background:var(--abg);border:1px solid var(--abr);border-top-left-radius:4px}
.bb p{margin-bottom:6px}.bb p:last-child{margin-bottom:0}
.bb strong{color:var(--fg);font-weight:600}
.bb code{background:rgba(0,214,143,.08);color:var(--ac);padding:2px 6px;border-radius:4px;font-family:var(--mn);font-size:.8rem}
.stg{margin-top:8px}
.stg button{background:none;border:1px solid var(--bd);color:var(--mt);font-size:.7rem;font-family:var(--mn);padding:3px 10px;border-radius:4px;cursor:pointer}
.stg button:hover{border-color:var(--mt);color:var(--fg)}
.stg-hid{display:none;margin-top:8px;background:#0c0f14;border:1px solid var(--bd);border-radius:6px;padding:10px;font-family:var(--mn);font-size:.76rem;color:var(--ac);white-space:pre-wrap;word-break:break-all}
.tw{max-height:350px;overflow:auto;margin:10px 0;border:1px solid var(--bd);border-radius:8px}
.tw::-webkit-scrollbar{width:4px;height:4px}.tw::-webkit-scrollbar-thumb{background:var(--bd);border-radius:2px}
.tb{width:100%;border-collapse:collapse;font-size:.76rem;font-family:var(--mn)}
.tb th{background:#0c0f14;color:var(--mt);padding:7px 11px;text-align:left;position:sticky;top:0;font-weight:600;text-transform:uppercase;letter-spacing:.3px;font-size:.67rem;white-space:nowrap}
.tb td{padding:6px 11px;border-top:1px solid var(--bd);white-space:nowrap;max-width:240px;overflow:hidden;text-overflow:ellipsis;color:var(--fg)}
.tb tr:hover td{background:rgba(0,214,143,.03)}
.tb .nl{color:var(--mt);font-style:italic}
.tb .hl{background:rgba(0,214,143,.15);color:var(--ac);font-weight:600}
.rc{font-size:.72rem;color:var(--mt);margin-top:5px;font-style:italic}
.xp{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:9px}
.xp-l{font-size:.66rem;color:var(--mt);font-family:var(--mn);text-transform:uppercase;letter-spacing:.5px;margin-right:2px}
.xb{padding:4px 10px;background:var(--bgc);border:1px solid var(--bd);border-radius:5px;color:var(--fg);font-family:var(--mn);font-size:.7rem;cursor:pointer;display:inline-flex;align-items:center;gap:5px;transition:all .2s}
.xb:hover{border-color:var(--ac);color:var(--ac);background:var(--ad)}
.xb:disabled{opacity:.5;cursor:progress}
.xb i{font-size:.66rem;color:var(--ac)}
.xrow{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:9px}
.xrow .xn{font-family:var(--mn);font-size:.76rem;color:var(--fg);min-width:170px}
.btn-exp{padding:6px 11px;background:var(--bg);border:1px solid var(--bd);border-radius:6px;color:var(--fg);font-family:var(--mn);font-size:.78rem;cursor:pointer;display:flex;align-items:center;gap:6px;transition:all .2s}
.btn-exp:hover:not(:disabled){border-color:var(--ac);color:var(--ac)}
.btn-exp:disabled{opacity:.35;cursor:not-allowed}
.qa{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.qb{padding:7px 13px;background:var(--bgc);border:1px solid var(--bd);border-radius:7px;color:var(--fg);font-family:var(--ui);font-size:.78rem;cursor:pointer;transition:all .2s;display:flex;align-items:center;gap:6px}
.qb:hover{border-color:var(--ac);background:var(--ad);color:var(--ac)}
.qb i{font-size:.7rem;color:var(--ac)}
.scb{background:#0c0f14;border:1px solid var(--bd);border-radius:8px;margin:8px 0;overflow:hidden}
.scb-h{padding:7px 12px;background:rgba(77,166,255,.05);border-bottom:1px solid var(--bd);font-size:.68rem;font-family:var(--mn);color:var(--inf);text-transform:uppercase;letter-spacing:.5px}
.scb-b{padding:10px 12px;font-family:var(--mn);font-size:.76rem;line-height:1.7;color:var(--mt)}
.scb-b .tn{color:var(--fg);font-weight:600}.scb-b .cn{color:var(--ac)}.scb-b .ct{opacity:.5}.scb-b .rn{color:var(--wn)}
.tp-ind{display:flex;gap:4px;padding:6px 0}
.tp-ind span{width:6px;height:6px;background:var(--mt);border-radius:50%;animation:bn 1.4s infinite ease-in-out}
.tp-ind span:nth-child(2){animation-delay:.2s}.tp-ind span:nth-child(3){animation-delay:.4s}
@keyframes bn{0%,80%,100%{transform:scale(.6);opacity:.4}40%{transform:scale(1);opacity:1}}
.ip{padding:12px 20px;border-top:1px solid var(--bd);background:var(--bgc);flex-shrink:0}
.ip-r{display:flex;gap:8px;align-items:flex-end}
.ip-r textarea{flex:1;padding:11px 14px;background:var(--bg);border:1px solid var(--bd);border-radius:var(--rd);color:var(--fg);font-family:var(--ui);font-size:.9rem;resize:none;outline:none;transition:border-color .2s;min-height:44px;max-height:120px;line-height:1.5}
.ip-r textarea:focus{border-color:var(--ac)}.ip-r textarea::placeholder{color:var(--mt)}
.btn-send{width:44px;height:44px;border-radius:var(--rd);border:1px solid var(--ac);background:var(--ac);color:#0a0d12;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:.95rem;transition:all .2s;flex-shrink:0}
.btn-send:hover{background:#00c080;border-color:#00c080;transform:scale(1.04)}
.btn-send:disabled{opacity:.3;cursor:not-allowed;transform:none}
.ip-h{text-align:center;font-size:.68rem;color:var(--mt);margin-top:5px;opacity:.5}
.wc{text-align:center;padding:30px 20px;max-width:520px;margin:auto}
.wc .wi{font-size:2.5rem;color:var(--bd);margin-bottom:14px}
.wc h2{font-size:1.15rem;font-weight:700;margin-bottom:8px}
.wc p{color:var(--mt);line-height:1.6;margin-bottom:16px;font-size:.86rem}
.wc .arw{font-size:1.1rem;color:var(--ac);margin-bottom:6px;animation:ab 1.5s infinite}
@keyframes ab{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}
.er{color:var(--dn);font-size:.82rem;display:flex;align-items:flex-start;gap:7px;margin-top:6px}
@media(max-width:640px){.hd{padding:10px 12px}.hd-l h1{font-size:.9rem}.ms{padding:12px}.mg{max-width:96%}.ip{padding:10px 12px}}
</style>
</head>
<body>

<div class="hd">
  <div class="hd-l"><h1><span class="ic"><i class="fas fa-database"></i></span> Database Chat</h1></div>
  <div class="hd-r">
    <select class="dsel" id="dbSel" onchange="onDbChange()"><option value="">Loading...</option></select>
    <button class="btn-exp" id="expBtn" onclick="showExport()" disabled title="Download data from this database"><i class="fas fa-download"></i> Export</button>
    <span class="pl" id="connSt"><span class="dt"></span>Connected</span>
    <span class="pl"><i class="fas fa-lock-open" style="font-size:.5rem"></i> Free</span>
  </div>
</div>

<div class="ms" id="msgBox">
  <div class="wc" id="welcome">
    <div class="arw"><i class="fas fa-arrow-up"></i></div>
    <p style="color:var(--ac);font-weight:600;margin-bottom:10px">First: select a database from the dropdown above</p>
    <div class="wi"><i class="fas fa-comments"></i></div>
    <h2>Ask about your data in plain English</h2>
    <p>Just type normally - no SQL, and spelling doesn't have to be perfect.<br>
    Examples: "how many stations at kucing", "stations per division", "list all districts"</p>
  </div>
</div>

<div class="ip">
  <div class="ip-r">
    <textarea id="inp" placeholder="Select a database first..." rows="1" onkeydown="onKey(event)"></textarea>
    <button class="btn-send" id="sendBtn" onclick="doSend()" disabled><i class="fas fa-paper-plane"></i></button>
  </div>
  <div class="ip-h">Enter to send</div>
</div>

<script>
var curDb = null;
var busy = false;
var curTables = [];   // schema of the selected database, for the export buttons
var expJobs = [];     // the SQL behind each answer, referenced by index
var inp = document.getElementById('inp');

inp.addEventListener('input', function() {
  inp.style.height = 'auto';
  inp.style.height = Math.min(inp.scrollHeight, 120) + 'px';
});

document.addEventListener('DOMContentLoaded', function() {
  loadDbs();
  inp.focus();
});

function loadDbs() {
  var sel = document.getElementById('dbSel');
  fetch('/api/databases').then(function(r) { return r.json(); }).then(function(d) {
    if (d.error) throw new Error(d.error);
    sel.innerHTML = '';
    var ph = document.createElement('option');
    ph.value = ''; ph.textContent = '-- Select database --';
    sel.appendChild(ph);
    (d.databases || []).forEach(function(db) {
      var o = document.createElement('option');
      o.value = db; o.textContent = db;
      sel.appendChild(o);
    });
  }).catch(function(e) {
    sel.innerHTML = '<option value="">Error</option>';
  });
}

function onDbChange() {
  var db = document.getElementById('dbSel').value;
  curDb = db;
  curTables = [];
  expJobs = [];
  document.getElementById('sendBtn').disabled = !db;
  document.getElementById('expBtn').disabled = true;
  document.getElementById('connSt').innerHTML = db
    ? '<span class="dt"></span>' + esc(db)
    : '<span class="dt"></span>Connected';
  if (!db) { inp.placeholder = 'Select a database first...'; return; }
  inp.placeholder = 'Ask anything about your data...';
  document.getElementById('msgBox').innerHTML = '';
  showTyping();
  fetch('/api/schema?db=' + encodeURIComponent(db)).then(function(r) { return r.json(); }).then(function(d) {
    if (d.error) throw new Error(d.error);
    hideTyping();
    curTables = d.tables || [];
    document.getElementById('expBtn').disabled = !curTables.length;
    showSchema(d.tables);
  }).catch(function(e) {
    hideTyping();
    addMsg('sy', '', '<div class="er"><i class="fas fa-exclamation-circle"></i> ' + esc(e.message) + '</div>');
  });
}

function showSchema(tables) {
  if (!tables || !tables.length) {
    addMsg('sy', '', '<p>Database <strong>' + esc(curDb) + '</strong> is empty - it has no tables yet, '
      + 'so there is nothing to ask about.</p><p style="color:var(--mt)">Import your data into it '
      + '(QGIS <em>Export &rarr; PostGIS</em>, <code>shp2pgsql</code>, or <code>ogr2ogr</code>), '
      + 'then pick the database again to reload.</p>');
    return;
  }
  var h = '<p>Database <strong>' + esc(curDb) + '</strong> is ready. Ask in plain English - no SQL, and typos are fine.<br>'
        + 'You get a short answer; say <strong>"show them"</strong> when you want the full table, '
        + 'or <strong>"what about Miri"</strong> to carry on.</p>';
  h += '<div class="scb"><div class="scb-h">What\'s in here</div><div class="scb-b">';
  tables.forEach(function(t) {
    h += '<div style="margin-bottom:8px"><span class="tn">' + esc(t.table) + '</span> <span class="rn">(' + t.rows + ' rows)</span><br>';
    t.columns.forEach(function(c) {
      h += '&nbsp;&nbsp;<span class="cn">' + esc(c.name) + '</span> <span class="ct">' + esc(c.type) + '</span><br>';
    });
    var f = t.filterable || {};
    Object.keys(f).forEach(function(k) {
      var v = f[k];
      h += '&nbsp;&nbsp;<span class="ct">you can ask by</span> <span class="cn">' + esc(k) + '</span><span class="ct">: '
         + esc(v.slice(0, 6).join(', ')) + (v.length > 6 ? ', ...' : '') + '</span><br>';
    });
    h += '</div>';
  });
  h += '</div></div><p>Try asking:</p><div class="qa">';

  // Build examples from the real data so they always work
  var qs = [];
  tables.forEach(function(t) {
    qs.push(['fa-table', 'Show ' + t.table, 'show all ' + t.table]);
    var f = t.filterable || {};
    var keys = Object.keys(f);
    if (keys.length) {
      var k = keys[0], v = f[k][0];
      qs.push(['fa-filter', t.table + ' in ' + v, t.table + ' in ' + v]);
      qs.push(['fa-chart-simple', 'Count per ' + k, 'how many ' + t.table + ' per ' + k]);
      qs.push(['fa-trophy', 'Which ' + k + ' has most', 'which ' + k + ' has the most ' + t.table]);
      qs.push(['fa-list-ul', 'List all ' + k, 'list all ' + k]);
    } else {
      qs.push(['fa-calculator', 'Count ' + t.table, 'how many ' + t.table]);
    }
  });
  qs.slice(0, 12).forEach(function(x) {
    h += '<button class="qb" onclick="quickQ(this.dataset.q)" data-q="' + esc(x[2]) + '">'
       + '<i class="fas ' + x[0] + '"></i> ' + esc(x[1]) + '</button>';
  });
  h += '</div>';
  addMsg('sy', '', h);
}

function onKey(e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSend(); } }
function quickQ(q) { inp.value = q; doSend(); }

function doSend() {
  var txt = inp.value.trim();
  if (!txt || busy || !curDb) return;
  var w = document.getElementById('welcome');
  if (w) w.remove();
  addMsg('ur', txt);
  inp.value = ''; inp.style.height = 'auto';
  busy = true;
  document.getElementById('sendBtn').disabled = true;
  showTyping();
  fetch('/api/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ database: curDb, question: txt })
  }).then(function(r) { return r.json(); }).then(function(d) {
    hideTyping();
    if (d.error) {
      addMsg('sy', '', fmt(d.error));
    } else {
      var h = '';
      if (d.reply) h += fmt(d.reply);
      if (d.data && d.data.rows && d.data.rows.length > 0) {
        h += buildTbl(d.data, d.highlight);
        h += exportBar(d.sql, d.sig);
      }
      if (d.sql) {
        h += '<div class="stg"><button onclick="var el=this.nextElementSibling;el.style.display=el.style.display===\'none\'?\'block\':\'none\';this.textContent=el.style.display===\'none\'?\'View SQL\':\'Hide SQL\'">View SQL</button><div class="stg-hid">' + esc(d.sql) + '</div></div>';
      }
      addMsg('sy', '', h);
    }
  }).catch(function(e) {
    hideTyping();
    addMsg('sy', '', '<div class="er"><i class="fas fa-exclamation-circle"></i> ' + esc(e.message) + '</div>');
  }).then(function() {
    busy = false;
    document.getElementById('sendBtn').disabled = !curDb;
    inp.focus();
  });
}

function fmt(t) {
  return esc(t)
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*\n]+)\*/g, '<em>$1</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/^\s*-\s+/gm, '&bull; ')   // only a dash starting a line is a bullet
    .replace(/\n/g, '<br>');
}

function buildTbl(data, hl) {
  var cols = data.columns || [];
  var rows = data.rows || [];
  var show = rows.slice(0, 100);
  var hv = hl ? hl.toLowerCase() : null;
  var h = '<div class="tw"><table class="tb"><thead><tr>';
  cols.forEach(function(c) { h += '<th>' + esc(c) + '</th>'; });
  h += '</tr></thead><tbody>';
  show.forEach(function(row) {
    h += '<tr>';
    cols.forEach(function(c) {
      var v = row[c];
      if (v === null || v === undefined) {
        h += '<td class="nl">NULL</td>';
      } else {
        var s = String(v);
        var isH = hv && s.toLowerCase().indexOf(hv) !== -1;
        h += '<td title="' + esc(s) + '"' + (isH ? ' class="hl"' : '') + '>' + esc(s.length > 120 ? s.substring(0, 120) + '...' : s) + '</td>';
      }
    });
    h += '</tr>';
  });
  h += '</tbody></table></div>';
  h += '<div class="rc">Showing ' + show.length + ' of ' + rows.length + ' rows';
  if (hv) h += ' &middot; matched: <span style="color:var(--ac)">"' + esc(hl) + '"</span>';
  h += '</div>';
  return h;
}

// ---- Download / export ----

function xbtn(icon, label, call) {
  return '<button class="xb" onclick="' + call + '"><i class="fas ' + icon + '"></i>' + label + '</button>';
}

function tableHasGeom(name) {
  var want = String(name).toLowerCase();
  for (var i = 0; i < curTables.length; i++) {
    var t = curTables[i];
    if (t.table.toLowerCase() !== want) continue;
    for (var j = 0; j < t.columns.length; j++) {
      var n = t.columns[j].name.toLowerCase();
      if (n.indexOf('geom') !== -1 || n === 'shape' || n === 'the_geom') return true;
    }
  }
  return false;
}

// GeoJSON only makes sense for plain rows straight out of a table with a
// geometry column - a "count per district" summary has nothing to draw.
function canGeoJson(sql) {
  if (!sql || /\bGROUP\s+BY\b/i.test(sql) || /^\s*SELECT\s+DISTINCT\b/i.test(sql)) return false;
  var m = /\bFROM\s+"([^"]+)"\."([^"]+)"/i.exec(sql);
  return m ? tableHasGeom(m[2]) : false;
}

function exportBar(sql, sig) {
  if (!sql) return '';
  var i = expJobs.push({sql: sql, sig: sig || ''}) - 1;
  var h = '<div class="xp"><span class="xp-l">Download</span>'
        + xbtn('fa-file-csv', 'CSV', 'expAnswer(this,' + i + ",'csv')")
        + xbtn('fa-file-code', 'JSON', 'expAnswer(this,' + i + ",'json')");
  if (canGeoJson(sql)) {
    h += xbtn('fa-map-location-dot', 'GeoJSON', 'expAnswer(this,' + i + ",'geojson')")
       + xbtn('fa-layer-group', 'Shapefile', 'expAnswer(this,' + i + ",'shp')");
  }
  return h + '</div>';
}

function expAnswer(btn, i, fmtName) {
  var job = expJobs[i];
  runExport(btn, { db: curDb, sql: job.sql, sig: job.sig, fmt: fmtName });
}

function expTable(btn, i, fmtName) {
  runExport(btn, { db: curDb, table: curTables[i].table, fmt: fmtName });
}

function showExport() {
  if (!curDb || !curTables.length) return;
  var w = document.getElementById('welcome');
  if (w) w.remove();
  var h = '<p>Download from <strong>' + esc(curDb) + '</strong> - the whole table, in the format you want.</p>';
  curTables.forEach(function(t, i) {
    h += '<div class="xrow"><span class="xn">' + esc(t.table)
       + ' <span style="color:var(--mt)">(' + t.rows + ' rows)</span></span>'
       + xbtn('fa-file-csv', 'CSV', 'expTable(this,' + i + ",'csv')")
       + xbtn('fa-file-code', 'JSON', 'expTable(this,' + i + ",'json')");
    if (tableHasGeom(t.table)) {
      h += xbtn('fa-map-location-dot', 'GeoJSON', 'expTable(this,' + i + ",'geojson')")
         + xbtn('fa-layer-group', 'Shapefile', 'expTable(this,' + i + ",'shp')");
    }
    h += '</div>';
  });
  h += '<p style="color:var(--mt);font-size:.8rem">Shapefile arrives as a .zip - '
     + '.shp, .shx, .dbf and .prj together, ready to unzip straight into QGIS.<br>'
     + 'Only want part of it? Ask a question first - every answer comes with its own '
     + 'download buttons.</p>';
  addMsg('sy', '', h);
}

function runExport(btn, params) {
  var qs = Object.keys(params).map(function(k) {
    return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]);
  }).join('&');
  var label = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; }
  fetch('/api/export?' + qs).then(function(r) {
    // Only the status says whether this is a file or a refusal - a successful
    // JSON export is application/json too, so the content type cannot decide.
    if (!r.ok) {
      return r.text().then(function(t) {
        var msg = 'Export failed';
        try { msg = JSON.parse(t).error || msg; } catch (err) {}
        throw new Error(msg);
      });
    }
    var name = 'export';
    var m = /filename="([^"]+)"/.exec(r.headers.get('Content-Disposition') || '');
    if (m) name = m[1];
    return r.blob().then(function(b) { saveBlob(b, name); });
  }).catch(function(e) {
    addMsg('sy', '', '<div class="er"><i class="fas fa-exclamation-circle"></i> ' + esc(e.message) + '</div>');
  }).then(function() {
    if (btn) { btn.disabled = false; btn.innerHTML = label; }
  });
}

function saveBlob(blob, name) {
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(function() { URL.revokeObjectURL(url); }, 1000);
}

function addMsg(role, text, html) {
  var box = document.getElementById('msgBox');
  var div = document.createElement('div');
  div.className = 'mg ' + role;
  var ic = role === 'ur' ? 'fa-user' : 'fa-robot';
  div.innerHTML = '<div class="av"><i class="fas ' + ic + '"></i></div><div class="bb">' + (html || '<p>' + esc(text).replace(/\n/g, '<br>') + '</p>') + '</div>';
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}

function showTyping() {
  var box = document.getElementById('msgBox');
  var div = document.createElement('div');
  div.className = 'mg sy';
  div.id = 'typEl';
  div.innerHTML = '<div class="av"><i class="fas fa-robot"></i></div><div class="bb"><div class="tp-ind"><span></span><span></span><span></span></div></div>';
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function hideTyping() {
  var el = document.getElementById('typEl');
  if (el) el.remove();
}

function esc(s) {
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
</script>
</body>
</html>"""


@app.route('/')
def index():
    return Response(HTML_PAGE, mimetype='text/html; charset=utf-8')


@app.route('/api/databases')
def list_databases():
    try:
        return jsonify({"databases": get_databases()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/schema')
def get_schema():
    db = request.args.get('db', '')
    if not db:
        return jsonify({"error": "No database selected"}), 400
    if not db_allowed(db):
        return jsonify({"error": "That database is not available here."}), 403
    try:
        # Send only what the page draws - the value index stays server-side
        payload = [{
            "schema": t["schema"],
            "table": t["table"],
            "rows": t["rows"],
            "columns": t["columns"],
            "filterable": {k: v for k, v in t.get("values", {}).items() if 1 < len(v) <= 25}
        } for t in get_schema_info(db)]
        return jsonify({"tables": payload})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


SESSION_COOKIE = "chatsid"


def with_session(payload, sid, status=200):
    """Answer, and make sure this visitor keeps their own follow-up thread."""
    resp = jsonify(payload)
    resp.status_code = status
    resp.set_cookie(SESSION_COOKIE, sid, max_age=60 * 60 * 12,
                    samesite="Lax", httponly=True,
                    secure=request.is_secure)
    return resp


@app.route('/api/ask', methods=['POST'])
def ask():
    data = request.get_json(silent=True) or {}
    db = data.get("database", "")
    question = (data.get("question") or "").strip()
    sid = request.cookies.get(SESSION_COOKIE) or uuid.uuid4().hex

    if not db:
        return with_session({"error": "No database selected"}, sid, 400)
    if not db_allowed(db):
        return with_session({"error": "That database is not available here."}, sid, 403)
    if not question:
        return with_session({"error": "Empty question"}, sid, 400)

    # Raw SQL
    if question.upper().startswith(READ_ONLY_PREFIXES):
        if not ALLOW_RAW_SQL:
            return with_session({"reply": "Typing SQL is switched off here - ask in plain English instead.",
                                 "sql": None, "data": None, "highlight": None}, sid)
        res, err = execute_sql(db, question)
        if err:
            return with_session({"reply": "SQL Error: " + err, "sql": question, "data": None, "highlight": None}, sid)
        if not res or not res["rows"]:
            return with_session({"reply": "Query returned 0 rows.", "sql": question, "data": None, "highlight": None}, sid)
        return with_session({"reply": 'Query returned **{}** rows:'.format(res["count"]),
                             "sql": question, "sig": sign_sql(question),
                             "data": res, "highlight": None}, sid)

    try:
        tables = get_schema_info(db)
        reply, sql, result, highlight = smart_ask(question, db, tables, sid)
    except Exception as e:
        return with_session({"error": str(e)}, sid, 500)
    return with_session({"reply": reply, "sql": sql, "sig": sign_sql(sql) if sql else None,
                         "data": result, "highlight": highlight}, sid)


@app.route('/api/export')
def export():
    """Download rows as CSV, JSON or GeoJSON.

    Either ?table=<name> for a whole table, or ?sql=<answer sql> for the rows
    behind an answer. Read-only queries only, same as /api/ask.
    """
    db = request.args.get('db', '')
    fmt = (request.args.get('fmt') or 'csv').lower()
    table_name = (request.args.get('table') or '').strip()
    sql = (request.args.get('sql') or '').strip()

    if not db:
        return jsonify({"error": "No database selected"}), 400
    if not db_allowed(db):
        return jsonify({"error": "That database is not available here."}), 403
    if fmt not in EXPORT_FORMATS:
        return jsonify({"error": "Unknown format: " + fmt}), 400
    if not table_name and not sql:
        return jsonify({"error": "Nothing to export"}), 400

    try:
        tables = get_schema_info(db)

        if table_name:
            table = find_table(tables, table_name)
            if not table:
                return jsonify({"error": "No table called " + table_name}), 404
            out_sql, err = table_export_sql(table, fmt)
            stem = table["table"]
        else:
            # A query is exportable if this server issued it (signed alongside
            # the answer), or if raw SQL is switched on anyway.
            if not (ALLOW_RAW_SQL or signature_ok(sql, request.args.get('sig', ''))):
                return jsonify({"error": "That query did not come from this page."}), 403
            if sql.upper().startswith("EXPLAIN"):
                return jsonify({"error": "An EXPLAIN plan is not data - there is nothing to download."}), 400
            if not sql.upper().startswith(READ_ONLY_PREFIXES):
                return jsonify({"error": "Only SELECT / WITH queries can be exported."}), 400
            out_sql, err = answer_export_sql(sql, tables, fmt)
            stem = db

        if err:
            return jsonify({"error": err}), 400

        # Check the query and learn its columns before the browser is told a
        # download is coming - an error now is JSON, not a half-written file.
        cols, perr = probe_columns(db, out_sql)
        if perr:
            return jsonify({"error": perr}), 400

        if fmt == "shp":
            blob, err = shapefile_zip(db, out_sql, cols, stem)
            if err:
                return jsonify({"error": err}), 400
            return Response(
                blob,
                content_type="application/zip",
                headers={"Content-Disposition": 'attachment; filename="{}.zip"'.format(
                             safe_filename(stem)),
                         "Cache-Control": "no-store"})

        body = {"csv": csv_body, "json": json_body, "geojson": geojson_body}[fmt]
        mime = {"csv": "text/csv",
                "json": "application/json",
                "geojson": "application/geo+json"}[fmt]
        fname = "{}.{}".format(safe_filename(stem), fmt)

        return Response(
            body(db, out_sql, cols),
            content_type=mime + "; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="{}"'.format(fname),
                     "Cache-Control": "no-store"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/healthz')
def healthz():
    """So a host can tell the app is alive without touching the database."""
    return jsonify({"ok": True})


if __name__ == '__main__':
    # Hosts hand the port over in $PORT; locally it stays 5000 as before.
    port = int(os.environ.get("PORT", "5000"))
    print("\n  +----------------------------------------------+")
    print("  |   Database Chat is running                   |")
    print("  |   Open: http://localhost:{}{}|".format(port, " " * (18 - len(str(port)))))
    print("  |   No API key needed                         |")
    print("  |   Press Ctrl+C to stop                      |")
    print("  +----------------------------------------------+\n")
    app.run(host='0.0.0.0', port=port, debug=False)