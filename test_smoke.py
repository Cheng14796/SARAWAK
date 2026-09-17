"""Smoke test for Database Chat.

    python test_smoke.py

Runs against the app itself through Flask's test client, so it needs no server
and no browser. Checks that need a database are skipped, not failed, when
there is no database to reach - that way this is safe to run on a fresh clone.
"""

import json
import os
import sys
import tempfile

os.environ.setdefault("WARM_SCHEMA", "0")     # no background scan during tests
os.environ.setdefault("MISS_LOG", os.path.join(tempfile.gettempdir(),
                                               "database_chat_misses_test.log"))

import chatbox                                # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
    else:
        FAIL.append("{}{}".format(name, " - " + detail if detail else ""))


def skip(name, why):
    SKIP.append("{} ({})".format(name, why))


client = chatbox.app.test_client()


# ---------------------------------------------------------------- the page

def test_page():
    r = client.get("/")
    body = r.get_data(as_text=True)
    check("page loads", r.status_code == 200, "status %s" % r.status_code)
    check("page has no inline handlers", "onclick" not in body)
    check("page links its own script", "app.js?v=" in body)
    check("page links its own styles", "app.css?v=" in body)
    check("page ships the icon sprite", "ic-db" in body)

    for path, kind in [("/robots.txt", "text/plain"),
                       ("/favicon.ico", "image/svg+xml"),
                       ("/static/app.js", "javascript"),
                       ("/static/app.css", "css"),
                       ("/static/vendor/leaflet.js", "javascript")]:
        r = client.get(path)
        check("serves " + path,
              r.status_code == 200 and kind in r.headers.get("Content-Type", ""),
              "status %s type %s" % (r.status_code, r.headers.get("Content-Type")))

    r = client.get("/healthz")
    check("healthz answers without touching the database",
          r.status_code == 200 and r.get_json().get("ok") is True)


def test_headers():
    csp = client.get("/").headers.get("Content-Security-Policy", "")
    check("CSP is set", bool(csp))
    check("CSP forbids inline script",
          "script-src 'self';" in csp and "script-src 'self' 'unsafe-inline'" not in csp)
    check("CSP allows only map tiles from outside", "tile.openstreetmap.org" in csp)
    headers = client.get("/").headers
    for header, want in [("X-Content-Type-Options", "nosniff"),
                         ("X-Frame-Options", "DENY"),
                         ("Referrer-Policy", "no-referrer")]:
        check("sends " + header, headers.get(header) == want, headers.get(header))


# ------------------------------------------------------- the question router

def test_sql_router():
    # These are questions, not SQL, even though they open with a SQL keyword.
    for question in ["With which division are the most stations?",
                     "with the most rainfall",
                     "select a district for me",
                     "explain the subbasin table"]:
        check("treats as a question: " + question[:34],
              not chatbox.looks_like_sql(question))

    for statement in ["SELECT * FROM rainfall_stations",
                      "select name from t where x = 1",
                      "WITH x AS (SELECT 1) SELECT * FROM x",
                      "EXPLAIN ANALYZE SELECT 1"]:
        check("treats as SQL: " + statement[:34], chatbox.looks_like_sql(statement))


def test_export_guards():
    tables = [{"schema": "public", "table": "stations",
               "columns": [{"name": "name", "type": "text"},
                           {"name": "geom", "type": "USER-DEFINED"}],
               "rows": 10}]

    built, err = chatbox.answer_export_sql(
        'SELECT COUNT(*) AS total FROM "public"."stations"', tables, "geojson")
    check("a count is not map features", built is None and bool(err))

    built, err = chatbox.answer_export_sql(
        'SELECT "name", COUNT(*) FROM "public"."stations" GROUP BY 1', tables, "geojson")
    check("a summary is not map features", built is None and bool(err))

    built, err = chatbox.answer_export_sql(
        'SELECT "name" FROM "public"."stations" LIMIT 100', tables, "geojson")
    check("plain rows can be mapped", bool(built) and not err)
    check("mapping adds the geometry back", built and chatbox.GEOJSON_COL in built)

    check("display limit is dropped for exports",
          chatbox.drop_display_limit('SELECT 1 FROM t LIMIT 100') == 'SELECT 1 FROM t')
    check("an explicit limit is kept",
          chatbox.drop_display_limit('SELECT 1 FROM t LIMIT 5') == 'SELECT 1 FROM t LIMIT 5')


def test_signature():
    sql = 'SELECT 1'
    check("a signed query verifies", chatbox.signature_ok(sql, chatbox.sign_sql(sql)))
    check("a tampered query does not", not chatbox.signature_ok(sql + ' OR 1=1',
                                                                chatbox.sign_sql(sql)))
    check("an unsigned query does not", not chatbox.signature_ok(sql, ""))


def test_rate_limit():
    original = chatbox.ASK_LIMIT
    chatbox.ASK_LIMIT = 2
    chatbox._rate_state.clear()
    try:
        codes = [client.post("/api/ask", json={"database": "", "question": "hi"}).status_code
                 for _ in range(4)]
        check("rate limiting kicks in", 429 in codes, "got %s" % codes)
    finally:
        chatbox.ASK_LIMIT = original
        chatbox._rate_state.clear()


def test_schema_cache_file():
    path = chatbox.SCHEMA_CACHE_FILE
    chatbox._schema_cache["__test__"] = {"at": 0, "tables": []}
    try:
        chatbox._save_schema_to_disk()
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        check("schema cache is written to disk", "__test__" in saved)
    except Exception as e:
        check("schema cache is written to disk", False, str(e))
    finally:
        chatbox._schema_cache.pop("__test__", None)
        chatbox._save_schema_to_disk()


# ------------------------------------------------------ needs a live database

def database_reachable():
    try:
        return bool(chatbox.get_databases())
    except Exception:
        return False


def test_with_database():
    if not database_reachable():
        skip("database round trip", "no database reachable")
        return
    db = chatbox.get_databases()[0]

    r = client.get("/api/schema?db=" + db)
    tables = r.get_json().get("tables", [])
    check("schema loads", r.status_code == 200 and isinstance(tables, list))
    check("schema says which tables can be mapped",
          all("geometry" in t for t in tables))

    r = client.post("/api/ask", json={"database": db, "question": "what is in here"})
    answer = r.get_json()
    check("the overview question answers", r.status_code == 200 and bool(answer.get("reply")))

    # With raw SQL switched off - the public setting - a query the server did
    # not sign is the only thing standing between a visitor and any table.
    raw_sql_was = chatbox.ALLOW_RAW_SQL
    chatbox.ALLOW_RAW_SQL = False
    try:
        r = client.get("/api/export", query_string={
            "db": db, "sql": "SELECT 1", "sig": "not-a-signature", "fmt": "csv"})
        check("an unsigned export is refused", r.status_code == 403,
              "status %s" % r.status_code)

        r = client.get("/api/geojson", query_string={
            "db": db, "sql": "SELECT 1", "sig": "not-a-signature"})
        check("an unsigned map request is refused", r.status_code == 403,
              "status %s" % r.status_code)

        r = client.post("/api/ask", json={"database": db,
                                          "question": "SELECT * FROM pg_user"})
        check("typed SQL is turned away in public",
              "switched off" in (r.get_json().get("reply") or ""))
    finally:
        chatbox.ALLOW_RAW_SQL = raw_sql_was

    if not tables:
        skip("table export", "database has no tables")
        return
    name = tables[0]["table"]
    r = client.get("/api/export", query_string={"db": db, "table": name, "fmt": "csv"})
    check("a table exports as CSV",
          r.status_code == 200 and r.get_data(as_text=True).count(chr(10)) > 1)

    # Spatial questions need an unambiguous point layer and polygon layer,
    # which is not necessarily the first database on the server.
    for candidate in chatbox.get_databases():
        its_tables = chatbox.get_schema_info(candidate)
        (pt, _pi), (poly, _qi) = chatbox.split_geom_tables(candidate, its_tables)
        if pt is not None:
            check_spatial_questions(client, candidate, pt, poly)
            return
    skip("spatial questions", "no database has a point layer and a polygon layer")


def check_spatial_questions(client, db, pt, poly):
    """A question about where things are must reach the geometry.

    The attribute matcher answers several of these by accident - "which area is
    point X in" becomes a text filter on a similarly-named column - so the check
    is not that an answer came back, but that the SQL behind it used PostGIS.

    The questions are built from this database's own table and column names, so
    the test travels to whatever data someone else points the app at.
    """
    def sql_for(question):
        r = client.post("/api/ask", json={"database": db, "question": question})
        return (r.get_json() or {}).get("sql") or ""

    spatial = ("ST_Contains", "ST_DWithin", "<->")
    points, areas = pt["table"], poly["table"]

    # A value from the polygon layer to measure from, e.g. a district name.
    place = None
    for column, values in (poly.get("values") or {}).items():
        if 1 < len(values) <= 25:
            place, place_col = values[0], column
            break

    questions = ["how many {} in each {}".format(points, areas)]
    if place:
        questions += ["{} within 10 km of {}".format(points, place),
                      "nearest {} to {}".format(points, place)]

    for question in questions:
        s = sql_for(question)
        check("answers spatially: " + question[:40],
              any(k in s for k in spatial), "sql was: " + " ".join(s.split())[:90])

    # Distances in a geographic CRS are degrees, so metres have to go through
    # ::geography. Without the cast "within 10 km" silently matches everything.
    if place:
        s = sql_for("{} within 10 km of {}".format(points, place))
        check("distance is measured in metres, not degrees", "::geography" in s)
    else:
        skip("distance check", "no short value list to measure from")

    # ...and the reverse: an ordinary grouping must not become a spatial join.
    plain = None
    for column, values in (pt.get("values") or {}).items():
        if 1 < len(values) <= 25:
            plain = column
            break
    if not plain:
        skip("attribute questions stay attribute questions", "nothing to group by")
        return
    for question in ["{} per {}".format(points, plain),
                     "list all {}".format(plain),
                     "how many {} at {}".format(points, pt["values"][plain][0])]:
        s = sql_for(question)
        check("stays an attribute question: " + question[:34],
              not any(k in s for k in spatial),
              "sql was: " + " ".join(sql_for(question).split())[:80])


def test_crs_reference():
    """The download CRS list is read from the bundled skill, not from memory."""
    grids = chatbox.malaysian_grids()
    by_epsg = {c["epsg"]: c for c in grids}

    check("CRS reference is read from the skill file", len(grids) > 5,
          "got %d" % len(grids))
    check("WGS84 is the default first choice",
          chatbox.crs_choices()[0]["epsg"] == 4326)

    # Legacy datums share their projection parameters with the modern grids but
    # use a different ellipsoid, so they have to be labelled.
    for epsg in (3168, 29873, 4117):
        check("EPSG:%d is flagged as a legacy datum" % epsg,
              epsg in by_epsg and by_epsg[epsg]["note"] == "legacy datum")

    check("an unlisted SRID is refused", chatbox.crs_allowed(9999) is None)
    check("a junk SRID is refused", chatbox.crs_allowed("; DROP") is None)
    check("any UTM zone is accepted", chatbox.crs_allowed(32630) == 32630)


def test_crs_recommendation():
    """Which grid to suggest comes from where the data is, not from a constant.

    Malaysia is the interesting case: it straddles two national grids whose
    parameters are nearly identical, so picking by longitude matters - for the
    Sarawak data, the wrong one is 1.9% out on area.
    """
    for name, extent, want in [
            ("Sarawak", (111.16, 1.22, 115.23, 3.35), 3376),
            ("Peninsular Malaysia", (100.1, 2.0, 104.5, 6.5), 3375),
            ("London", (-0.51, 51.28, 0.33, 51.69), 32630),
            ("Lima", (-77.2, -12.3, -76.8, -11.9), 32718),
            ("Sydney", (150.9, -34.1, 151.3, -33.7), 32756),
    ]:
        got, why = chatbox.recommend_epsg(extent)
        check("recommends EPSG:%d for %s" % (want, name), got == want,
              "got %s (%s)" % (got, why))

    check("no extent means no recommendation",
          chatbox.recommend_epsg(None) == (None, ""))

    # Twenty Malaysian grids are noise to someone mapping London.
    fake = {"schema": "public", "table": "_crs_test",
            "columns": [{"name": "geom", "type": "USER-DEFINED"}]}
    chatbox._extent_cache[("_t", "public", "_crs_test")] = (-0.51, 51.28, 0.33, 51.69)
    try:
        abroad = chatbox.crs_choices("_t", fake)
        check("data outside Malaysia is offered WGS84 and its UTM zone only",
              len(abroad) == 2 and abroad[1]["epsg"] == 32630,
              "got %s" % [c["epsg"] for c in abroad])
    finally:
        chatbox._extent_cache.pop(("_t", "public", "_crs_test"), None)


def test_database_discovery():
    """Someone else's server must not come up with an empty dropdown."""
    check("no database allow-list by default", chatbox.VISIBLE_DATABASES == [],
          "got %s" % chatbox.VISIBLE_DATABASES)
    check("PostgreSQL's own databases are hidden",
          "template1" in chatbox.INTERNAL_DATABASES)
    check("a provider's admin database is hidden",
          "rdsadmin" in chatbox.INTERNAL_DATABASES)
    check("an internal database is not allowed through the API",
          not chatbox.db_allowed("template1"))
    check("an ordinary database is allowed through the API",
          chatbox.db_allowed("anybody_elses_database"))


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()

    for line in PASS:
        print("  ok    " + line)
    for line in SKIP:
        print("  skip  " + line)
    for line in FAIL:
        print("  FAIL  " + line)
    print("\n{} passed, {} skipped, {} failed".format(len(PASS), len(SKIP), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
