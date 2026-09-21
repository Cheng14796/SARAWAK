"""
Database Chat - ask a PostGIS database questions in plain English.

The page is templates/index.html with static/app.js beside it; everything
else - reading the schema, matching a question against the real values in the
data, building the SQL, and the export and map endpoints - is in here.

Run: python chatbox.py
Open: http://localhost:5000
"""

from flask import Flask, jsonify, Response, request, render_template, send_from_directory
import psycopg2
import psycopg2.pool
import logging
import threading
import socket
import time
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

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("database-chat")

# Static files are stamped with this in the page, so a browser can cache them
# hard and still pick up a new version the moment one ships.
ASSET_VERSION = "5"

# What the page calls itself. Set APP_TITLE to name your own deployment.
APP_TITLE = os.environ.get("APP_TITLE", "Database Chat").strip() or "Database Chat"
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 60 * 60 * 24

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

# A query that runs longer than this is a runaway. Without it one bad question
# holds a worker thread until gunicorn's 120s timeout - a quarter of this
# site's concurrency - and the visitor stares at a spinner the whole time.
STATEMENT_TIMEOUT_MS = int(os.environ.get("STATEMENT_TIMEOUT_MS", "20000"))
# Applied per connection in _Pool.borrow, not passed in the connection options.
# A pooler refuses it in the startup packet - Neon's "-pooler" host answers
# "unsupported startup parameter in options: statement_timeout" and drops the
# connection, so sending it there stops the app reaching the database at all.
DB_CONFIG["connect_timeout"] = int(os.environ.get("CONNECT_TIMEOUT", "10"))
DB_CONFIG["application_name"] = "database-chat"

# An optional allow-list: set VISIBLE_DATABASES to show only these, in this
# order. Left unset - the default - every database on the server is offered,
# so pointing this app at someone else's PostgreSQL just works.
VISIBLE_DATABASES = [d.strip() for d in os.environ.get(
    "VISIBLE_DATABASES", "").split(",") if d.strip()]

# Housekeeping databases that belong to PostgreSQL or to the hosting provider.
# Nobody wants to ask questions of these, and on a managed host some of them
# refuse connections outright.
INTERNAL_DATABASES = {
    "template0", "template1",                     # PostgreSQL's own
    "rdsadmin", "azure_maintenance", "azure_sys",  # AWS RDS, Azure
    "cloudsqladmin", "alloydbadmin", "alloydbmetadata",  # Google
    "defaultdb_replica", "_timescaledb_internal",
}

# "postgres", "neondb" and the like exist on every server as a default landing
# database and are usually empty - but some people do keep real tables in one,
# so emptiness is checked rather than assumed from the name.

# ============ SCHEMA CACHE ============
# Reading a schema is not cheap: a COUNT(*) per table, then a DISTINCT scan of
# every text column so questions can be matched against real values. Without a
# cache that lands on whoever asks the first question after a restart, so the
# cache is warmed in the background at startup and written to disk, and a
# restart reloads it instead of re-scanning.

SCHEMA_CACHE_FILE = os.environ.get("SCHEMA_CACHE_FILE") or os.path.join(
    tempfile.gettempdir(), "database_chat_schema.json")
# 0 means the cache never goes stale on its own - refresh it deliberately.
SCHEMA_TTL = int(os.environ.get("SCHEMA_TTL_SECONDS", "0"))

_schema_cache = {}          # dbname -> {"at": epoch, "tables": [...]}
_schema_locks = {}
_schema_meta_lock = threading.Lock()


def _schema_lock_for(dbname):
    with _schema_meta_lock:
        lock = _schema_locks.get(dbname)
        if lock is None:
            lock = _schema_locks[dbname] = threading.Lock()
        return lock


def _cached_schema(dbname):
    entry = _schema_cache.get(dbname)
    if not entry:
        return None
    if SCHEMA_TTL and (time.time() - entry["at"]) > SCHEMA_TTL:
        return None
    return entry["tables"]


def _load_schema_from_disk():
    try:
        with open(SCHEMA_CACHE_FILE, encoding="utf-8") as f:
            saved = json.load(f)
    except Exception:
        return
    if not isinstance(saved, dict):
        return
    for dbname, entry in saved.items():
        try:
            if db_allowed(dbname) and entry.get("tables") is not None:
                _schema_cache[dbname] = {"at": float(entry.get("at", 0)),
                                         "tables": entry["tables"]}
        except Exception:
            continue
    if _schema_cache:
        log.info("schema cache: loaded %d database(s) from %s",
                 len(_schema_cache), SCHEMA_CACHE_FILE)


def _save_schema_to_disk():
    # Write beside the target and rename, so a crash mid-write cannot leave a
    # half-written file that the next boot would refuse to parse.
    tmp = SCHEMA_CACHE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_schema_cache, f)
        os.replace(tmp, SCHEMA_CACHE_FILE)
    except Exception as e:
        log.warning("schema cache: could not save (%s)", e)
        try:
            os.remove(tmp)
        except Exception:
            pass


# ============ CONNECTION POOL ============
# Opening a connection to a managed Postgres costs a TCP round trip plus a TLS
# handshake - 100-300ms to somewhere like Neon, paid again on every question
# and several times over on an export. The pool pays it once and keeps the
# connection warm between requests.

POOL_MAX = int(os.environ.get("DB_POOL_MAX", "6"))
POOL_WAIT = float(os.environ.get("DB_POOL_WAIT", "10"))

_pools = {}
_pools_lock = threading.Lock()


# Managed databases usually publish both A and AAAA records. A host that has
# an IPv6 address but no route to the internet over it will sit there until the
# connection times out rather than failing, because the packets go nowhere.
# Set DB_PREFER_IPV4=1 to dial the IPv4 address and skip that entirely.
PREFER_IPV4 = os.environ.get("DB_PREFER_IPV4", "0").lower() in ("1", "true", "yes")


def _ipv4_for(host):
    try:
        infos = socket.getaddrinfo(host, 5432, socket.AF_INET, socket.SOCK_STREAM)
    except Exception as e:
        log.warning("no IPv4 address for %s (%s)", host, e)
        return None
    return infos[0][4][0] if infos else None


def _cap_query_time(conn):
    """Cap query time on the connection itself, as a statement.

    Re-applied on every borrow rather than set once: a transaction-pooled
    connection can come back with session settings already discarded, so it
    cannot be assumed to still be in force.
    """
    if STATEMENT_TIMEOUT_MS <= 0:
        return
    cur = conn.cursor()
    try:
        # The value is an int from the environment, so it cannot carry SQL.
        cur.execute("SET statement_timeout = {:d}".format(STATEMENT_TIMEOUT_MS))
        conn.commit()
    finally:
        cur.close()


class _Pool(object):
    """A psycopg2 pool with a doorman.

    psycopg2's own pool raises the moment it is empty; the semaphore makes a
    caller wait its turn instead, which is what you want when four threads
    share six connections.
    """

    def __init__(self, dbname):
        cfg = dict(DB_CONFIG)
        cfg["database"] = dbname
        if PREFER_IPV4 and cfg.get("host"):
            addr = _ipv4_for(cfg["host"])
            if addr:
                # host stays for TLS and certificate checking; hostaddr is what
                # actually gets dialled, so no IPv6 address is ever tried.
                cfg["hostaddr"] = addr
                log.info("connecting to %s over IPv4 %s", cfg["host"], addr)
        self.slots = threading.Semaphore(POOL_MAX)
        self.pool = psycopg2.pool.ThreadedConnectionPool(1, POOL_MAX, **cfg)

    def borrow(self):
        if not self.slots.acquire(timeout=POOL_WAIT):
            raise RuntimeError("The database is busy - please try again in a moment.")
        try:
            conn = self.pool.getconn()
        except Exception:
            self.slots.release()
            raise
        try:
            _cap_query_time(conn)
        except Exception as e:
            # The cap is a safety net, not a requirement - a server that will
            # not take it is still perfectly usable.
            log.debug("could not set statement_timeout: %s", e)
        return _PooledConnection(self, conn)

    def give_back(self, conn, broken):
        try:
            self.pool.putconn(conn, close=broken)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
        finally:
            self.slots.release()


class _PooledConnection(object):
    """Looks like a connection, but close() hands it back.

    Every call site here is written as `conn = get_connection(db)` then
    `conn.close()`, so the pool slots in without touching any of them. A
    connection that broke, or that comes back still inside a transaction, is
    dropped rather than passed to the next visitor.
    """

    def __init__(self, owner, conn):
        self._owner = owner
        self._conn = conn

    def __getattr__(self, name):
        conn = self.__dict__.get("_conn")
        if conn is None:
            raise AttributeError("connection already returned to the pool")
        return getattr(conn, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        conn, self._conn = self._conn, None
        if conn is None:
            return
        broken = conn.closed != 0
        if not broken:
            try:
                conn.rollback()
            except Exception:
                broken = True
        self._owner.give_back(conn, broken)


def _pool_for(dbname):
    with _pools_lock:
        pool = _pools.get(dbname)
        if pool is None:
            pool = _Pool(dbname)
            _pools[dbname] = pool
        return pool


def get_connection(dbname):
    return _pool_for(dbname).borrow()


_has_tables_cache = {}


def has_user_tables(dbname):
    """Whether a database holds anything worth asking about.

    Cached: this runs once per database to build the dropdown, and an empty
    database does not fill itself while the server is up. /api/refresh clears
    it for the case where it does.
    """
    if dbname in _has_tables_cache:
        return _has_tables_cache[dbname]
    found = False
    try:
        conn = get_connection(dbname)
        try:
            cur = conn.cursor()
            cur.execute("""SELECT 1 FROM information_schema.tables
                           WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                             AND table_type = 'BASE TABLE'
                             AND table_name NOT IN %s
                           LIMIT 1""", (tuple(SYSTEM_TABLES),))
            found = cur.fetchone() is not None
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        # Unreachable is not the same as empty - a database we cannot open is
        # better left in the list than silently dropped.
        log.info("could not inspect database %r: %s", dbname, e)
        found = True
    _has_tables_cache[dbname] = found
    return found


def get_databases():
    """Every database worth offering, or exactly the configured list."""
    conn = get_connection(ADMIN_DB)
    try:
        cur = conn.cursor()
        cur.execute("SELECT datname FROM pg_database "
                    "WHERE datistemplate = false AND datallowconn "
                    "ORDER BY datname")
        dbs = [r[0] for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()

    if VISIBLE_DATABASES:
        # Keep the configured order, and skip any that aren't on this server
        return [d for d in VISIBLE_DATABASES if d in dbs]

    # A database with no tables has nothing to answer, so it is left out
    # rather than offered as a dead end.
    offered = [d for d in dbs
               if d.lower() not in INTERNAL_DATABASES and has_user_tables(d)]
    # Everything was filtered out - better a usable dropdown than an empty one.
    return offered or [d for d in dbs if d.lower() not in INTERNAL_DATABASES]


def db_allowed(dbname):
    if VISIBLE_DATABASES:
        return dbname in VISIBLE_DATABASES
    return dbname.lower() not in INTERNAL_DATABASES


def get_schema_info(dbname, refresh=False):
    """The cached schema, reading it only if nobody has it yet.

    The lock matters: with four threads sharing one worker, several visitors
    arriving at once would otherwise each kick off the same full scan.
    """
    if not refresh:
        hit = _cached_schema(dbname)
        if hit is not None:
            return hit
    with _schema_lock_for(dbname):
        if not refresh:
            hit = _cached_schema(dbname)
            if hit is not None:
                return hit
        tables = _read_schema_info(dbname)
        _schema_cache[dbname] = {"at": time.time(), "tables": tables}
        _save_schema_to_disk()
        return tables


# Why the database could not be reached, kept for /healthz. A deployed app is
# a black box otherwise: the logs say what went wrong, but the person who has
# to fix it is usually looking at a URL, not at a log tail.
_startup_problem = None


def warm_schema_cache():
    """Read every visible schema up front, in the background.

    Called from a daemon thread at startup so the site is already warm by the
    time the first visitor has picked a database.
    """
    global _startup_problem
    _startup_problem = "still connecting..."
    try:
        names = get_databases()
    except Exception as e:
        _startup_problem = "listing databases - {}: {}".format(
            type(e).__name__, str(e).strip())[:300]
        log.warning("schema warm-up: cannot list databases (%s)", e)
        return
    if not names:
        _startup_problem = ("connected, but no database matched the allow-list "
                            "{!r}".format(VISIBLE_DATABASES))
        return

    # A per-database failure used to be logged and forgotten, which left
    # /healthz reporting no problem while nothing had actually loaded.
    trouble = []
    for dbname in names:
        if _cached_schema(dbname) is not None:
            continue
        started = time.time()
        try:
            get_schema_info(dbname)
            log.info("schema warm-up: %s ready in %.1fs", dbname, time.time() - started)
        except Exception as e:
            trouble.append("{} after {:.0f}s - {}: {}".format(
                dbname, time.time() - started, type(e).__name__, str(e).strip()))
            log.warning("schema warm-up: %s failed (%s)", dbname, e)
    _startup_problem = " | ".join(trouble)[:400] if trouble else None


def _read_schema_info(dbname):
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
    return result


READ_ONLY_PREFIXES = ("SELECT", "WITH", "EXPLAIN")

# A question is only treated as typed SQL when it is actually shaped like SQL.
# Matching on the first word alone meant "With which division are the most
# stations?" was answered with "typing SQL is switched off here".
RE_LOOKS_LIKE_SQL = re.compile(
    r"""^\s*(?:SELECT\s+.+?\s+FROM\s+|WITH\s+\S+\s+AS\s*[(]|EXPLAIN\s+(?:ANALYZE\s+)?SELECT\s+)""",
    re.I | re.S)


def looks_like_sql(text):
    return bool(RE_LOOKS_LIKE_SQL.match(text or ""))


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
GEOM_4326 = ("CASE WHEN ST_SRID({g}) IN (0, 4326) "
             "THEN {g} ELSE ST_Transform({g}, 4326) END")

GEOM_AS_GEOJSON = "ST_AsGeoJSON(" + GEOM_4326 + ") AS {a}"

# The map only needs what a screen can show. Simplifying and rounding to six
# decimals (about 10cm) turns a basin boundary from megabytes into kilobytes;
# the download still gets the full-fidelity geometry.
GEOM_AS_GEOJSON_MAP = ("ST_AsGeoJSON(ST_SimplifyPreserveTopology("
                       + GEOM_4326 + ", {tol}), 6) AS {a}")

MAP_SIMPLIFY = os.environ.get("MAP_SIMPLIFY_DEGREES", "0.0005")
MAP_MAX_FEATURES = int(os.environ.get("MAP_MAX_FEATURES", "5000"))


# ---- Download CRS choices, read from the bundled GIS skill ----
# A shapefile carries its own .prj, so it can be delivered in a projected grid
# rather than lon/lat - which is what you want before measuring anything, since
# areas and distances in EPSG:4326 are in degrees.
#
# The EPSG codes come from skills/gis-spatial-analysis/references/malaysian_crs.md
# rather than from memory, on that file's own instruction: the modern GDM2000
# grids and the legacy Kertau/Timbalai ones have nearly identical projection
# parameters but different ellipsoids, so a code recalled from memory can be
# wrong by hundreds of metres without erroring. Parsing the file keeps one
# verified table as the single source of truth.
#
# GeoJSON is deliberately not offered a choice - the format is specified as
# WGS84 lon/lat, so shipping projected coordinates in it would be malformed.

SKILL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "skills", "gis-spatial-analysis")

CRS_REFERENCE = os.path.join(SKILL_DIR, "references", "malaysian_crs.md")

WGS84 = {"epsg": 4326, "label": "WGS84 lon/lat (EPSG:4326)", "note": "as stored"}

# Which projected grid to recommend depends on where the data actually is, so
# it is worked out from the layer's own extent rather than fixed in advance.
# Malaysian data gets the verified national grid from the skill's table;
# anything else gets its UTM zone, which is defined worldwide.
#
# Malaysia splits across two national grids that are easy to mix up: the
# Peninsula in the west and Borneo (Sabah/Sarawak) in the east. They share
# nearly all their projection parameters, so the wrong one produces a
# plausible-looking answer - about 1.9% out on area for Sarawak data.
MY_PENINSULA_EPSG = 3375
MY_BORNEO_EPSG = 3376

# Rough boxes, only ever used to pick which grid to *suggest*.
MY_BOUNDS = (99.0, 0.5, 120.0, 7.6)          # all of Malaysia
MY_PENINSULA_MAX_LON = 105.0                  # west of this is the Peninsula
MY_BORNEO_MIN_LON = 108.0                     # east of this is Borneo


def utm_epsg(lon, lat):
    """The UTM zone covering a point. Defined for the whole world, metre units,
    and about as good as a general-purpose projected CRS gets without knowing
    the local national grid."""
    zone = int((lon + 180.0) // 6.0) + 1
    zone = max(1, min(60, zone))
    return (32600 if lat >= 0 else 32700) + zone


def within(bounds, lon, lat):
    return bounds[0] <= lon <= bounds[2] and bounds[1] <= lat <= bounds[3]


def recommend_epsg(extent):
    """(epsg, why) for a layer, from its centre. None when there is no extent."""
    if not extent:
        return None, ""
    lon = (extent[0] + extent[2]) / 2.0
    lat = (extent[1] + extent[3]) / 2.0

    if within(MY_BOUNDS, lon, lat):
        if lon >= MY_BORNEO_MIN_LON:
            return MY_BORNEO_EPSG, "recommended - Sabah/Sarawak national grid"
        if lon <= MY_PENINSULA_MAX_LON:
            return MY_PENINSULA_EPSG, "recommended - Peninsular Malaysia grid"

    zone = utm_epsg(lon, lat)
    return zone, "recommended - UTM zone {}{} for this data".format(
        zone % 100, "N" if lat >= 0 else "S")

RE_CRS_ROW = re.compile(r'^\|\s*(?!-)(?P<name>[^|]+?)\s*\|\s*\*{0,2}(?P<epsg>\d{4,5})\*{0,2}\s*\|')


def _parse_crs_reference(path=CRS_REFERENCE):
    """Pull (epsg, name) out of the skill's verified CRS tables."""
    out = []
    seen = set()
    try:
        with open(path, encoding="utf-8") as f:
            # Tracked at the top heading level only: the legacy datums live
            # under one "## Legacy datums" heading but are split across several
            # sub-headings, and every grid below it needs the same warning.
            legacy_section = False
            for line in f:
                s = line.strip()
                if s.startswith("## ") and not s.startswith("### "):
                    legacy_section = "legacy" in s.lower()
                    continue
                if s.startswith("#"):
                    continue
                m = RE_CRS_ROW.match(s)
                if not m:
                    continue
                epsg = int(m.group("epsg"))
                if epsg in seen:
                    continue
                name = m.group("name").strip().strip("*")
                if not name or name.lower() in ("name", "state", "state grid"):
                    continue
                legacy = legacy_section or "Cassini Grid" in name
                seen.add(epsg)
                out.append({"epsg": epsg,
                            "label": "{} (EPSG:{})".format(name, epsg),
                            "note": "legacy datum" if legacy else ""})
    except OSError:
        log.warning("CRS reference not readable at %s - offering WGS84 only", path)
    return out


def malaysian_grids():
    grids = _crs_cache.get("my")
    if grids is None:
        grids = _parse_crs_reference()
        _crs_cache["my"] = grids
        log.info("CRS reference: %d Malaysian grids from %s",
                 len(grids), os.path.basename(CRS_REFERENCE))
    return grids


def layer_extent(dbname, table):
    """A layer's bounding box in lon/lat, or None. Cached - it only changes
    when the data does, and it is only ever used to suggest a CRS."""
    key = (dbname, table["schema"], table["table"])
    if key in _extent_cache:
        return _extent_cache[key]

    extent = None
    geom = geom_column(table["columns"])
    if geom:
        box = GEOM_4326.format(g=quote_ident(geom))
        res, err = execute_sql(dbname,
                               "SELECT ST_XMin(e) a, ST_YMin(e) b, "
                               "ST_XMax(e) c, ST_YMax(e) d FROM "
                               "(SELECT ST_Extent({}) e FROM {}.{}) q".format(
                                   box, quote_ident(table["schema"]),
                                   quote_ident(table["table"])))
        if not err and res["rows"] and res["rows"][0]["a"] is not None:
            r = res["rows"][0]
            extent = (float(r["a"]), float(r["b"]), float(r["c"]), float(r["d"]))
    _extent_cache[key] = extent
    return extent


def crs_choices(dbname=None, table=None):
    """The coordinate systems a shapefile can be delivered in.

    WGS84 always, then whatever suits this particular layer - its national grid
    if the data sits in Malaysia, otherwise its UTM zone. The Malaysian grids
    are only listed when the data is actually there; for a user in Peru they
    would be twenty lines of noise.
    """
    extent = layer_extent(dbname, table) if (dbname and table) else None
    epsg, why = recommend_epsg(extent)

    choices = [dict(WGS84)]
    grids = malaysian_grids()
    by_epsg = {c["epsg"]: c for c in grids}
    in_malaysia = bool(extent) and within(
        MY_BOUNDS, (extent[0] + extent[2]) / 2.0, (extent[1] + extent[3]) / 2.0)

    if epsg is not None:
        known = by_epsg.get(epsg)
        label = known["label"] if known else "UTM zone {}{} (EPSG:{})".format(
            epsg % 100, "N" if epsg < 32700 else "S", epsg)
        choices.append({"epsg": epsg, "label": label, "note": why})

    if in_malaysia:
        choices += [c for c in grids if c["epsg"] != epsg]
    elif extent is None:
        # Nothing to go on - offer the verified table rather than nothing.
        choices += grids
    return choices


_crs_cache = {}
_extent_cache = {}


def crs_allowed(epsg, dbname=None, table=None):
    """Only a code this server offered - never an arbitrary SRID."""
    try:
        epsg = int(epsg)
    except (TypeError, ValueError):
        return None
    for c in crs_choices(dbname, table):
        if c["epsg"] == epsg:
            return epsg
    # A UTM zone is always a safe, well-defined target even if this particular
    # layer suggested a different one.
    if 32601 <= epsg <= 32660 or 32701 <= epsg <= 32760:
        return epsg
    return None


# smart_ask caps its display query at 100 rows; an explicit "top 5" sets its own
# limit instead. Only the display cap is dropped on export - "top 5" must stay 5.
DISPLAY_LIMIT = 100

RE_TRAILING_LIMIT = re.compile(r'\s+LIMIT\s+(\d+)\s*;?\s*$', re.I)

RE_SELECT_FROM = re.compile(r'^\s*SELECT\s+(?P<cols>.+?)\s+FROM\s+(?P<rest>.+)$',
                            re.I | re.S)

RE_FIRST_TABLE = re.compile(r'^"(?P<schema>[^"]+)"\."(?P<table>[^"]+)"')

# "how many stations" is one number, not 345 map features. Without this the
# geometry gets bolted onto an aggregate and Postgres rejects the whole query.
RE_AGGREGATE = re.compile(r'\b(?:COUNT|SUM|AVG|MIN|MAX|STRING_AGG|ARRAY_AGG)\s*[(]', re.I)


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


def geom_select(geom, for_map=False):
    tpl = GEOM_AS_GEOJSON_MAP if for_map else GEOM_AS_GEOJSON
    return tpl.format(g=quote_ident(geom), a=quote_ident(GEOJSON_COL),
                      tol=MAP_SIMPLIFY)


def no_geometry(name, fmt):
    return "**{}** has no geometry column, so it cannot be exported as {}.".format(
        name, "a shapefile" if fmt == "shp" else "GeoJSON")


def table_export_sql(table, fmt, for_map=False):
    """SELECT for a whole table - geometry as GeoJSON text, or dropped."""
    geom = geom_column(table["columns"])
    sel = get_display_cols(table["columns"])
    if fmt in GEO_FORMATS:
        if not geom:
            return None, no_geometry(table["table"], fmt)
        sel = sel + [geom_select(geom, for_map)]
    return "SELECT {} FROM {}.{}".format(
        ", ".join(sel) or "*",
        quote_ident(table["schema"]), quote_ident(table["table"])), None


def answer_export_sql(sql, tables, fmt, for_map=False):
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
    if RE_AGGREGATE.search(m.group("cols")):
        return None, ("That answer is a single figure, not map features - "
                      "ask for the rows themselves to map them.")
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
    sel = "{}, {}".format(m.group("cols").strip(), geom_select(geom, for_map))
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


def shapefile_zip(dbname, sql, cols, stem, epsg=None):
    """A real shapefile set - .shp/.shx/.dbf/.prj/.cpg - bundled into one zip,
    because a shapefile is never a single file. `epsg` delivers it in a
    projected grid instead of lon/lat.

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

    # The rows arrive as GeoJSON, which is always WGS84 lon/lat, so the frame
    # starts there. Reprojecting afterwards goes through pyproj rather than
    # hand-written parameters - the whole point of not transcribing a grid
    # definition from memory.
    gdf = gpd.GeoDataFrame(records, geometry=geoms, crs="EPSG:4326")
    if epsg and int(epsg) != 4326:
        try:
            gdf = gdf.to_crs(epsg=int(epsg))
        except Exception as e:
            return None, "Could not reproject to EPSG:{} - {}".format(epsg, e)

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


# ============ SPATIAL QUESTIONS ============
# "which subbasin is Kapit station in", "stations within 10 km of Sibu",
# "how many stations in each subbasin" - questions the attribute matcher above
# answers by accident, with a text filter on a similarly-named column, when the
# honest answer needs the geometry.
#
# Written against the gis-spatial-analysis skill's references/postgis_patterns.md.
# Three rules from it govern everything below:
#   1. EPSG:4326 is degrees. ST_DWithin(geom, pt, 10000) there means 10000
#      DEGREES, not 10 km - so every distance goes through ::geography, which
#      measures metres on the sphere. (Good enough for rainfall stations; a
#      cadastral job would want a projected local grid instead.)
#   2. ST_Contains(polygon, point) for containment, with the polygon side left
#      untouched so its GiST index still applies.
#   3. Nearest-neighbour goes through CROSS JOIN LATERAL ... ORDER BY <-> so the
#      index's KNN support is used instead of a full scan per row.

RE_WITHIN_DIST = re.compile(
    r'\bwithin\s+(\d+(?:\.\d+)?)\s*(km|kilometre?s?|kilometer?s?|m|metre?s?|meter?s?)\b')

RE_NEAREST = re.compile(r'\b(nearest|closest|next to)\b')

RE_INSIDE = re.compile(
    r'\b(inside|within|contains?|containing|falls?|located|sits?|belongs?|which|what)\b')

METRES_PER = {"m": 1.0, "metre": 1.0, "metres": 1.0, "meter": 1.0, "meters": 1.0,
              "km": 1000.0, "kilometre": 1000.0, "kilometres": 1000.0,
              "kilometer": 1000.0, "kilometers": 1000.0}

SPATIAL_LIMIT = 200


def geometry_registry(dbname):
    """{(schema, table): {"col", "type", "srid"}} straight from PostGIS.

    geometry_columns is the authority on which column holds geometry, what kind
    of shape it is and which SRID it is stored in - all three of which have to
    be known before a spatial query can be written honestly.
    """
    reg = _geom_registry.get(dbname)
    if reg is not None:
        return reg
    reg = {}
    res, err = execute_sql(dbname,
                           "SELECT f_table_schema, f_table_name, f_geometry_column, "
                           "type, srid FROM geometry_columns")
    if not err and res:
        for r in res["rows"]:
            reg[(r["f_table_schema"], r["f_table_name"])] = {
                "col": r["f_geometry_column"],
                "type": (r["type"] or "").upper(),
                "srid": int(r["srid"] or 0)}
    _geom_registry[dbname] = reg
    return reg


_geom_registry = {}


def geom_info(dbname, table):
    return geometry_registry(dbname).get((table["schema"], table["table"]))


def split_geom_tables(dbname, tables, accepted=None):
    """The point layer and the polygon layer this question is about.

    One of each and the choice is obvious. With several - a database holding
    wells, towns, parcels and districts - the question decides: whichever layer
    it names wins, and if it names none, there is nothing to guess at.
    """
    pts, polys = [], []
    for t in tables:
        info = geom_info(dbname, t)
        if not info:
            continue
        if "POINT" in info["type"]:
            pts.append((t, info))
        elif "POLYGON" in info["type"]:
            polys.append((t, info))

    def pick(candidates):
        if len(candidates) == 1:
            return candidates[0]
        if not candidates or accepted is None:
            return (None, None)
        named = [c for c in candidates if mentions_table(accepted, c[0])]
        return named[0] if len(named) == 1 else (None, None)

    point, poly = pick(pts), pick(polys)
    if point[0] is None or poly[0] is None:
        return (None, None), (None, None)
    return point, poly


def qualified(t):
    return "{}.{}".format(quote_ident(t["schema"]), quote_ident(t["table"]))


def aligned_geom(alias, info, target_srid):
    """The geometry of `alias`, reprojected only if it is not already in the
    target SRID. PostGIS refuses to compare mismatched SRIDs outright, so this
    is the difference between a working join and a hard error - and leaving the
    expression bare when the SRIDs agree keeps the GiST index usable."""
    col = "{}.{}".format(alias, quote_ident(info["col"]))
    if info["srid"] and target_srid and info["srid"] != target_srid:
        return "ST_Transform({}, {})".format(col, target_srid)
    return col


def label_column(table):
    """The column a person would recognise a row by."""
    for c in table["columns"]:
        n = str(c["name"]).lower()
        if is_text_type(c["type"]) and (n.endswith("_name") or n.endswith("_n")
                                        or n == "name"):
            return c["name"]
    for c in table["columns"]:
        if is_text_type(c["type"]) and not is_geom_col(c["name"]):
            return c["name"]
    return table["columns"][0]["name"]


def spatial_descr_cols(table, limit=4):
    """A few readable columns to show alongside a spatial answer."""
    out = []
    for c in table["columns"]:
        if is_geom_col(c["name"]):
            continue
        if is_text_type(c["type"]):
            out.append(c["name"])
        if len(out) >= limit:
            break
    return out or [table["columns"][0]["name"]]


def reference_filter(q_orig, table):
    """The place named in the question, resolved inside one table - the 'Sibu'
    in 'stations within 10 km of Sibu'.

    Matched against this table alone rather than against the question's overall
    hits: a name like 'Kapit' is both a station division and a district, and the
    overall matcher keeps only the higher-scoring of the two.
    """
    _toks, hits, _used = collect_hits(q_orig, [table])
    for h in hits:
        if h["kind"] == "value":
            return h
    return None


def geography_expr(alias, info):
    """Metres, not degrees. A geographic CRS measures in degrees, so a distance
    in metres has to go through the geography type - and geography is defined on
    4326, so anything else is reprojected first."""
    col = "{}.{}".format(alias, quote_ident(info["col"]))
    if info["srid"] and info["srid"] != 4326:
        col = "ST_Transform({}, 4326)".format(col)
    return col + "::geography"


def mentions_table(accepted, table):
    for h in accepted:
        t = h.get("table")
        if t is not None and t["table"] == table["table"]:
            return True
    return False


def value_in_clause(hit):
    vals = hit["values"][:MAX_IN_VALUES]
    return "{} IN ({})".format(
        quote_ident(hit["column_name"]),
        ", ".join("'{}'".format(safe_sql_string(v)) for v in vals))


def matched_column(q_orig, table):
    """The column of `table` the question points at, if any."""
    _t, hits, _u = collect_hits(q_orig, [table])
    cols = [h for h in hits if h["kind"] == "column"]
    cols = prefer_names(cols, table, norm_text(q_orig), q_orig)
    return cols[0]["column"] if cols else None


def parse_distance(q):
    m = RE_WITHIN_DIST.search(q)
    if not m:
        return None, None
    unit = m.group(2).lower()
    per = METRES_PER.get(unit)
    if per is None:
        per = 1000.0 if unit.startswith("k") else 1.0
    return float(m.group(1)) * per, m.group(0)


def spatial_answer(q, q_orig, dbname, tables, accepted, ctx_key):
    """Answer with geometry, or return None and let the attribute matcher try.

    Only runs where the database really has one point layer and one polygon
    layer - anything else and a spatial reading would be guesswork.
    """
    (pt_t, pt_i), (poly_t, poly_i) = split_geom_tables(dbname, tables, accepted)
    if pt_t is None:
        return None

    metres, dist_phrase = parse_distance(q)
    wants_near = bool(RE_NEAREST.search(q))
    wants_group = bool(RE_GROUP.search(q))

    names_points = mentions_table(accepted, pt_t)
    names_polys = mentions_table(accepted, poly_t)

    # ---- how many points in each polygon ----
    # Only spatial when the question actually spans both layers; "stations per
    # division" is a plain column grouping and must stay one.
    if wants_group and names_points and names_polys and not metres and not wants_near:
        gcol = matched_column(q_orig, poly_t) or {"name": label_column(poly_t)}
        sql = (
            'SELECT d.{g} AS {galias}, COUNT(s.*) AS total\n'
            'FROM {poly} d\n'
            'LEFT JOIN {pts} s ON ST_Contains(d.{pgeom}, {sgeom})\n'
            'GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {lim}'
        ).format(g=quote_ident(gcol["name"]), galias=quote_ident(gcol["name"]),
                 poly=qualified(poly_t), pts=qualified(pt_t),
                 pgeom=quote_ident(poly_i["col"]),
                 sgeom=aligned_geom("s", pt_i, poly_i["srid"]),
                 lim=SPATIAL_LIMIT)
        res, err = execute_sql(dbname, sql)
        if err:
            return None
        remember(ctx_key, poly_t, "spatial_group", gcol["name"])
        pairs = inline_pairs(res["rows"], gcol["name"], "total")
        return ('**{}** counted inside each **{}**, by where they actually fall: {}'.format(
                    pt_t["table"], gcol["name"], pairs),
                sql, res, None)

    # ---- within N km / nearest ----
    if metres or wants_near:
        ref_hit = reference_filter(q_orig, poly_t) or reference_filter(q_orig, pt_t)
        if ref_hit is None:
            return None
        ref_t = poly_t if ref_hit["table"]["table"] == poly_t["table"] else pt_t
        ref_i = poly_i if ref_t is poly_t else pt_i

        # The layer being listed is whichever one is not the reference, unless
        # the question never mentions it - then it is the points.
        if ref_t is poly_t:
            tgt_t, tgt_i = pt_t, pt_i
        elif names_polys:
            tgt_t, tgt_i = poly_t, poly_i
        else:
            tgt_t, tgt_i = pt_t, pt_i

        ref_geom = quote_ident(ref_i["col"])
        if ref_i["srid"] and tgt_i["srid"] and ref_i["srid"] != tgt_i["srid"]:
            ref_geom = "ST_Transform({}, {})".format(ref_geom, tgt_i["srid"])
        cte = ('WITH ref AS (SELECT ST_Union({rg}) AS g FROM {rt} WHERE {w})\n'
               ).format(rg=ref_geom, rt=qualified(ref_t), w=value_in_clause(ref_hit))

        cols = ", ".join("t." + quote_ident(c) for c in spatial_descr_cols(tgt_t))
        tgt_geog = geography_expr("t", tgt_i)
        ref_geog = "ref.g::geography" if (not tgt_i["srid"] or tgt_i["srid"] == 4326) \
            else "ST_Transform(ref.g, 4326)::geography"
        dist = ("ROUND((ST_Distance({}, {}) / 1000.0)::numeric, 2) AS km_away"
                ).format(tgt_geog, ref_geog)
        where = ref_hit["display"]

        if metres:
            sql = cte + (
                'SELECT {cols}, {dist}\n'
                'FROM {tgt} t, ref\n'
                'WHERE ST_DWithin({tg}, {rg}, {m})\n'
                'ORDER BY km_away LIMIT {lim}'
            ).format(cols=cols, dist=dist, tgt=qualified(tgt_t),
                     tg=tgt_geog, rg=ref_geog, m=metres, lim=SPATIAL_LIMIT)
            lead = '**{}** within {} of **{}**'.format(
                tgt_t["table"], dist_phrase.replace("within ", ""), where)
        else:
            # KNN through LATERAL so the GiST index does the work.
            sql = cte + (
                'SELECT {cols}, {dist}\n'
                'FROM ref\n'
                'CROSS JOIN LATERAL (\n'
                '  SELECT * FROM {tgt} ORDER BY {tgeom} <-> ref.g LIMIT {n}\n'
                ') t\n'
                'ORDER BY km_away'
            ).format(cols=cols, dist=dist, tgt=qualified(tgt_t),
                     tgeom=quote_ident(tgt_i["col"]), n=10)
            lead = '**{}** nearest to **{}**'.format(tgt_t["table"], where)

        res, err = execute_sql(dbname, sql)
        if err:
            return None
        if not res["rows"]:
            return ('Nothing in **{}** is that close to **{}**.'.format(
                tgt_t["table"], where), sql, None, None)
        remember(ctx_key, tgt_t, "spatial_rows")
        return (lead + ' - **{}** of them, measured on the ground:'.format(
            res["count"]), sql, res, where)

    # ---- which polygon is this point in ----
    if names_points and names_polys and RE_INSIDE.search(q):
        ref_hit = reference_filter(q_orig, pt_t)
        if ref_hit is None:
            return None
        pcols = ", ".join("d." + quote_ident(c) for c in spatial_descr_cols(poly_t))
        sql = (
            'SELECT s.{lab}, {pcols}\n'
            'FROM {pts} s\n'
            'JOIN {poly} d ON ST_Contains(d.{pgeom}, {sgeom})\n'
            'WHERE s.{w}\n'
            'ORDER BY 1 LIMIT {lim}'
        ).format(lab=quote_ident(label_column(pt_t)), pcols=pcols,
                 pts=qualified(pt_t), poly=qualified(poly_t),
                 pgeom=quote_ident(poly_i["col"]),
                 sgeom=aligned_geom("s", pt_i, poly_i["srid"]),
                 w=value_in_clause(ref_hit), lim=SPATIAL_LIMIT)
        res, err = execute_sql(dbname, sql)
        if err:
            return None
        if not res["rows"]:
            return ('**{}** does not fall inside any **{}**.'.format(
                ref_hit["display"], poly_t["table"]), sql, None, None)
        remember(ctx_key, poly_t, "spatial_rows")
        return ('Where **{}** falls, by point-in-polygon:'.format(ref_hit["display"]),
                sql, res, ref_hit["display"])

    return None


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

    # ---- SPATIAL ----
    # Tried before the attribute matcher, because that matcher will happily
    # answer "which subbasin is station X in" with a text filter on a
    # similarly-named column and never touch the geometry. Returns None for
    # anything that is not genuinely spatial, so ordinary questions fall
    # straight through.
    spatial = spatial_answer(q, q_orig, dbname, tables, accepted, ctx_key)
    if spatial is not None:
        return spatial

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


# ============ THE PAGE ============
# The page itself lives in templates/index.html, with static/app.css and
# static/app.js beside it. It used to be one long string in this file, which
# meant no syntax highlighting, no caching, and a Content-Security-Policy that
# had to allow inline script. Now the browser caches all three and the policy
# below can forbid inline script outright.


# ============ PUBLIC-FACING GUARDS ============
# Everything below this line assumes the visitor is a stranger: their questions
# are rate limited, their errors say nothing about the database internals, and
# the browser is told exactly what the page is allowed to load.

# Per IP, per minute. 0 switches a limit off.
ASK_LIMIT = int(os.environ.get("ASK_LIMIT_PER_MINUTE", "40"))
EXPORT_LIMIT = int(os.environ.get("EXPORT_LIMIT_PER_MINUTE", "10"))

# Refuse to build a download bigger than this many rows - 0 means no ceiling.
# Whole-table downloads are the expensive ones, and the row count is already in
# the schema cache, so the refusal costs nothing.
MAX_EXPORT_ROWS = int(os.environ.get("MAX_EXPORT_ROWS", "0"))

# Lets an operator rebuild the schema cache without a redeploy. Unset means the
# endpoint is closed - it is a full re-scan, not something to leave open.
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN", "").strip()

# Long enough for any real question, short enough that nobody can post a novel.
MAX_QUESTION_CHARS = 500

# The signed SQL a page sends back. Generous, but not a place to paste a book.
MAX_EXPORT_SQL_CHARS = 20000

# Questions the engine could not turn into a query. This list is the only
# honest guide to what the matcher is missing - it cannot be guessed from here.
MISS_LOG = os.environ.get("MISS_LOG") or os.path.join(
    tempfile.gettempdir(), "database_chat_misses.log")

# The page loads nothing from anywhere else: its script, styles, icons and the
# map library are all served from here. Map tiles are the one exception, and
# they are images only.
CSP = ("default-src 'none'; "
       "base-uri 'none'; "
       "form-action 'none'; "
       "frame-ancestors 'none'; "
       "script-src 'self'; "
       # Leaflet positions its tiles by writing style properties, so styles
       # cannot be locked down the way scripts can. Scripts are the half that
       # matters for XSS, and those load from here only.
       "style-src 'self' 'unsafe-inline'; "
       "font-src 'self'; "
       "img-src 'self' data: https://*.tile.openstreetmap.org; "
       "connect-src 'self'")

GENERIC_ERROR = "Something went wrong at our end. Please try again."

_rate_state = {}
_rate_lock = threading.Lock()
_miss_lock = threading.Lock()


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or request.remote_addr or "unknown"


def rate_ok(bucket, per_minute):
    """A leaky bucket per visitor. One process, one worker, so a dict does."""
    if per_minute <= 0:
        return True
    key = (client_ip(), bucket)
    now = time.time()
    with _rate_lock:
        tokens, last = _rate_state.get(key, (float(per_minute), now))
        tokens = min(per_minute, tokens + (now - last) * per_minute / 60.0)
        if tokens < 1:
            _rate_state[key] = (tokens, now)
            return False
        _rate_state[key] = (tokens - 1.0, now)
        if len(_rate_state) > 5000:
            for k, (_, seen) in list(_rate_state.items()):
                if now - seen > 300:
                    _rate_state.pop(k, None)
        return True


def too_many(what):
    return jsonify({"error": "That is a lot of {} at once - "
                             "give it a moment and try again.".format(what)}), 429


def oops(exc, public=GENERIC_ERROR):
    """Log what actually happened, tell the visitor something safe.

    Postgres errors name schemas, roles and columns; a stranger on the public
    site has no business reading them, and an attacker would enjoy them.
    """
    if isinstance(exc, RuntimeError):        # our own "the database is busy"
        return str(exc)
    log.exception("request failed: %s %s", request.method, request.path)
    return public


def db_error_text(err):
    """A failed generated query. Locally the raw text helps; publicly it leaks."""
    log.warning("query failed: %s", err)
    if ALLOW_RAW_SQL:
        return "Error: " + str(err)
    return "That question could not be answered against this database."


def record_miss(dbname, question):
    """Note a question that got the help text instead of an answer."""
    log.info("unanswered [%s]: %s", dbname, question)
    line = json.dumps({"at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       "db": dbname, "question": question}, ensure_ascii=False)
    try:
        with _miss_lock:
            with open(MISS_LOG, "a", encoding="utf-8") as f:
                f.write(line + chr(10))
    except Exception:
        pass          # a log that cannot be written must never break an answer


def mappable(sql, tables):
    """Can the rows behind this answer be drawn? Same test the export uses."""
    if not sql:
        return False
    try:
        built, err = answer_export_sql(sql, tables, "geojson", for_map=True)
        return bool(built) and not err
    except Exception:
        return False


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Permissions-Policy",
                            "geolocation=(), microphone=(), camera=()")
    if request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
    return resp


@app.route('/')
def index():
    return render_template("index.html", v=ASSET_VERSION,
                           app_title=APP_TITLE,
                           raw_sql=1 if ALLOW_RAW_SQL else 0)


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(app.static_folder, "favicon.svg",
                               mimetype="image/svg+xml")


@app.route('/robots.txt')
def robots():
    # The data is public; the endpoints that cost money are not worth crawling.
    body = chr(10).join(["User-agent: *", "Disallow: /api/", ""])
    return Response(body, mimetype="text/plain")


@app.route('/api/databases')
def list_databases():
    try:
        return jsonify({"databases": get_databases()})
    except Exception as e:
        return jsonify({"error": oops(e, "Cannot reach the database right now.")}), 503


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
            "filterable": {k: v for k, v in t.get("values", {}).items() if 1 < len(v) <= 25},
            "geometry": bool(geom_column(t["columns"]))
        } for t in get_schema_info(db)]
        return jsonify({"tables": payload})
    except Exception as e:
        return jsonify({"error": oops(e, "Cannot read that database right now.")}), 503


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
    if not rate_ok("ask", ASK_LIMIT):
        return too_many("questions")

    data = request.get_json(silent=True) or {}
    db = data.get("database", "")
    question = (data.get("question") or "").strip()[:MAX_QUESTION_CHARS]
    sid = request.cookies.get(SESSION_COOKIE) or uuid.uuid4().hex

    if not db:
        return with_session({"error": "No database selected"}, sid, 400)
    if not db_allowed(db):
        return with_session({"error": "That database is not available here."}, sid, 403)
    if not question:
        return with_session({"error": "Empty question"}, sid, 400)

    # Raw SQL
    if looks_like_sql(question):
        if not ALLOW_RAW_SQL:
            return with_session({"reply": "Typing SQL is switched off here - ask in plain English instead.",
                                 "sql": None, "data": None, "highlight": None}, sid)
        res, err = execute_sql(db, question)
        if err:
            return with_session({"reply": db_error_text(err), "sql": question,
                                 "data": None, "highlight": None}, sid)
        if not res or not res["rows"]:
            return with_session({"reply": "Query returned 0 rows.", "sql": question, "data": None, "highlight": None}, sid)
        return with_session({"reply": 'Query returned **{}** rows:'.format(res["count"]),
                             "sql": question, "sig": sign_sql(question),
                             "data": res, "highlight": None}, sid)

    try:
        tables = get_schema_info(db)
        reply, sql, result, highlight = smart_ask(question, db, tables, sid)
    except Exception as e:
        return with_session({"error": oops(e)}, sid, 500)

    if sql is None and result is None and not RE_OVERVIEW.search(norm_text(question)):
        record_miss(db, question)

    return with_session({"reply": reply, "sql": sql, "sig": sign_sql(sql) if sql else None,
                         "data": result, "highlight": highlight,
                         "mappable": mappable(sql, tables)}, sid)


@app.route('/api/export')
def export():
    """Download rows as CSV, JSON or GeoJSON.

    Either ?table=<name> for a whole table, or ?sql=<answer sql> for the rows
    behind an answer. Read-only queries only, same as /api/ask.
    """
    if not rate_ok("export", EXPORT_LIMIT):
        return too_many("downloads")

    db = request.args.get('db', '')
    fmt = (request.args.get('fmt') or 'csv').lower()
    table_name = (request.args.get('table') or '').strip()
    sql = (request.args.get('sql') or '').strip()

    if len(sql) > MAX_EXPORT_SQL_CHARS:
        return jsonify({"error": "That query is too long."}), 400
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
            if MAX_EXPORT_ROWS and table["rows"] > MAX_EXPORT_ROWS:
                # Better an honest refusal than a file that is quietly
                # truncated - a half table looks exactly like a whole one.
                return jsonify({"error": (
                    "{} has {:,} rows, more than this site will build in one "
                    "download ({:,}). Ask a question to narrow it down first - "
                    "every answer has its own download buttons.".format(
                        table["table"], table["rows"], MAX_EXPORT_ROWS))}), 413
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
            want_crs = request.args.get("crs")
            epsg = crs_allowed(want_crs, db, table if table_name else None) \
                if want_crs else None
            if want_crs and epsg is None:
                return jsonify({"error": "EPSG:{} is not one of the offered "
                                         "coordinate systems.".format(want_crs)}), 400
            blob, err = shapefile_zip(db, out_sql, cols, stem, epsg)
            if err:
                return jsonify({"error": err}), 400
            if epsg and epsg != 4326:
                stem = "{}_epsg{}".format(stem, epsg)
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
        return jsonify({"error": oops(e, "That download could not be built.")}), 500


@app.route('/api/geojson')
def geojson_for_map():
    """The rows behind an answer, as features the page can draw.

    Deliberately not the export: geometry is simplified and rounded, and the
    feature count is capped, because this is for a screen rather than for QGIS.
    """
    if not rate_ok("map", EXPORT_LIMIT * 3):
        return too_many("map requests")

    db = request.args.get('db', '')
    sql = (request.args.get('sql') or '').strip()
    table_name = (request.args.get('table') or '').strip()

    if not db or not db_allowed(db):
        return jsonify({"error": "That database is not available here."}), 403
    if not sql and not table_name:
        return jsonify({"error": "Nothing to draw"}), 400
    if len(sql) > MAX_EXPORT_SQL_CHARS:
        return jsonify({"error": "That query is too long."}), 400
    if sql and not (ALLOW_RAW_SQL or signature_ok(sql, request.args.get('sig', ''))):
        return jsonify({"error": "That query did not come from this page."}), 403

    try:
        tables = get_schema_info(db)
        if table_name:
            table = find_table(tables, table_name)
            if not table:
                return jsonify({"error": "No table called " + table_name}), 404
            built, err = table_export_sql(table, "geojson", for_map=True)
        else:
            if not sql.upper().startswith(READ_ONLY_PREFIXES):
                return jsonify({"error": "Only SELECT / WITH queries can be drawn."}), 400
            built, err = answer_export_sql(sql, tables, "geojson", for_map=True)
        if err:
            return jsonify({"error": err}), 400

        capped = "SELECT * FROM ({}) _map LIMIT {}".format(built, MAP_MAX_FEATURES + 1)
        res, qerr = execute_sql(db, capped)
        if qerr:
            return jsonify({"error": db_error_text(qerr)}), 400

        rows = res["rows"]
        truncated = len(rows) > MAP_MAX_FEATURES
        features = []
        for row in rows[:MAP_MAX_FEATURES]:
            raw = row.get(GEOJSON_COL)
            if not raw:
                continue
            try:
                geometry = json.loads(raw)
            except (TypeError, ValueError):
                continue
            props = {k: export_value(v) for k, v in row.items() if k != GEOJSON_COL}
            features.append({"type": "Feature", "geometry": geometry, "properties": props})

        return jsonify({"type": "FeatureCollection", "features": features,
                        "truncated": truncated, "limit": MAP_MAX_FEATURES})
    except Exception as e:
        return jsonify({"error": oops(e, "That map could not be drawn.")}), 500


@app.route('/api/refresh', methods=['POST'])
def refresh_schema():
    """Re-read a schema after the data changes, without a redeploy."""
    if not REFRESH_TOKEN:
        return jsonify({"error": "Refresh is not enabled here."}), 404
    token = request.headers.get("X-Refresh-Token", "")
    if not hmac.compare_digest(token, REFRESH_TOKEN):
        return jsonify({"error": "No."}), 403
    db = request.args.get('db', '')
    try:
        names = [db] if db else get_databases()
        for name in names:
            if db_allowed(name):
                get_schema_info(name, refresh=True)
        return jsonify({"refreshed": names})
    except Exception as e:
        return jsonify({"error": oops(e)}), 500


@app.route('/api/crs')
def list_crs():
    """The coordinate systems a shapefile can be delivered in.

    Pass ?db= and ?table= to get the list that suits that layer - its national
    grid or its UTM zone. Without them, only the general choices.
    """
    db = request.args.get("db", "")
    table_name = (request.args.get("table") or "").strip()
    try:
        table = None
        if db and db_allowed(db) and table_name:
            table = find_table(get_schema_info(db), table_name)
        return jsonify({"crs": crs_choices(db if table else None, table)})
    except Exception as e:
        return oops(e)


def db_diagnosis():
    """Where the app is trying to connect, and what went wrong.

    Never includes the password. The host and user are what separate the two
    ways this goes wrong on a host like Render: "DATABASE_URL was never set"
    shows localhost, "set but wrong" shows the real server.
    """
    return {
        "reading": "DATABASE_URL" if os.environ.get("DATABASE_URL", "").strip()
                   else "PGHOST/PGUSER (DATABASE_URL is not set)",
        "host": DB_CONFIG.get("host"),
        "user": DB_CONFIG.get("user"),
        "sslmode": DB_CONFIG.get("sslmode", "(not set)"),
        "looks_up_databases_in": ADMIN_DB,
        "allow_list": VISIBLE_DATABASES or "(none - every database is offered)",
        "problem": _startup_problem or "(no error recorded yet)",
    }


def probe_network(host, port=5432, timeout=8):
    """Can this machine open a plain TCP socket to the database server?

    Nothing here touches psycopg2 or the connection pool, so it still answers
    when those are wedged - which is the case worth telling apart. A platform
    that cannot reach the server at all looks, from outside, exactly like a
    driver that is hanging.
    """
    out = {"host": host, "port": port}
    started = time.time()
    try:
        addrs = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        out["resolved_to"] = sorted({a[4][0] for a in addrs})
        out["dns_took"] = "{:.1f}s".format(time.time() - started)
    except Exception as e:
        out["dns_took"] = "{:.1f}s".format(time.time() - started)
        out["error"] = "DNS {}: {}".format(type(e).__name__, str(e)[:160])
        return out

    started = time.time()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        out["tcp"] = "connected in {:.1f}s".format(time.time() - started)
    except Exception as e:
        out["tcp"] = "failed after {:.1f}s".format(time.time() - started)
        out["error"] = "TCP {}: {}".format(type(e).__name__, str(e)[:160])
    return out


def probe_database():
    """Try the connection now and report what happened, with timings.

    The warm-up runs once at startup; this lets someone re-test after changing
    a setting without waiting for a restart.
    """
    out = {}
    started = time.time()
    try:
        names = get_databases()
        out["listed_databases"] = names
        out["listing_took"] = "{:.1f}s".format(time.time() - started)
    except Exception as e:
        out["listing_took"] = "{:.1f}s".format(time.time() - started)
        out["failed_at"] = "listing databases"
        out["error"] = "{}: {}".format(type(e).__name__, str(e).strip())[:300]
        return out

    if not names:
        out["failed_at"] = "allow-list left nothing to show"
        return out

    started = time.time()
    try:
        tables = get_schema_info(names[0])
        out["read_schema_of"] = names[0]
        out["schema_took"] = "{:.1f}s".format(time.time() - started)
        out["tables_found"] = [t["table"] for t in tables]
    except Exception as e:
        out["schema_took"] = "{:.1f}s".format(time.time() - started)
        out["failed_at"] = "reading the schema of " + names[0]
        out["error"] = "{}: {}".format(type(e).__name__, str(e).strip())[:300]
    return out


@app.route('/healthz')
def healthz():
    """So a host can tell the app is alive without touching the database.

    While nothing has loaded, it also says why - that is exactly when someone
    is staring at a deployed URL wondering what is wrong. It goes away on its
    own once a schema is in, so a working site publishes nothing extra.

    /healthz?probe=1 retries the connection there and then, which saves a
    restart after changing a setting. Only while nothing has loaded, so it
    cannot be used to hammer a healthy database.
    """
    warm = sorted(_schema_cache.keys())
    out = {"ok": True, "warm": warm}
    if not warm:
        out["diagnosis"] = db_diagnosis()
        probe = request.args.get("probe")
        if probe == "1":
            # Network only: always answers, even with the pool wedged.
            out["network"] = probe_network(DB_CONFIG.get("host", ""))
        elif probe == "db":
            # Goes through psycopg2 and the pool, so it can hang if they are.
            out["probe"] = probe_database()
    return jsonify(out)


def start_up():
    """Load yesterday's cache, then fill in whatever is missing, in the
    background. The page and /healthz answer immediately either way."""
    _load_schema_from_disk()
    if os.environ.get("WARM_SCHEMA", "1").lower() in ("0", "false", "no"):
        return
    threading.Thread(target=warm_schema_cache, name="schema-warm-up",
                     daemon=True).start()


start_up()


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