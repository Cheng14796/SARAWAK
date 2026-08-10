"""
Copy the local databases up to a managed PostgreSQL host.

    python migrate_to_host.py --check
    python migrate_to_host.py --target "postgresql://user:pw@host/postgres?sslmode=require"

What it does, per database in VISIBLE_DATABASES:
  1. creates it on the target if it is not there yet
  2. enables PostGIS in it
  3. pg_dump from local -> psql into the target

spatial_ref_sys is deliberately left behind: PostGIS fills it in itself, and
8500 rows of projection text is the slowest, least useful part of the copy.
"""

import argparse
import os
import subprocess
import sys
from urllib.parse import urlparse, unquote, quote

import psycopg2
from psycopg2 import sql as pgsql

# Same .env the app reads, so the local password lives in one ignored file.
from chatbox import _load_dotenv

_load_dotenv()

LOCAL = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": os.environ.get("PGPORT", "5432"),
    "user": os.environ.get("PGUSER", "postgres"),
    "password": os.environ.get("PGPASSWORD", ""),
}

DATABASES = [d.strip() for d in os.environ.get(
    "VISIBLE_DATABASES", "sarawak basin,subbasin_gis").split(",") if d.strip()]

SKIP_TABLES = ["public.spatial_ref_sys"]


def find_tool(name):
    """pg_dump/psql are usually installed but rarely on PATH on Windows."""
    from shutil import which
    found = which(name)
    if found:
        return found
    roots = [r"C:\Program Files\PostgreSQL", r"C:\Program Files (x86)\PostgreSQL"]
    cands = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for ver in os.listdir(root):
            exe = os.path.join(root, ver, "bin", name + ".exe")
            if os.path.isfile(exe):
                cands.append((ver, exe))
    if cands:
        # highest version number wins
        cands.sort(key=lambda v: [int(p) if p.isdigit() else 0
                                  for p in v[0].split(".")], reverse=True)
        return cands[0][1]
    return None


def parse_target(url):
    p = urlparse(url)
    host = p.hostname or ""
    # Neon's "-pooler" host runs PgBouncer, which will not carry CREATE
    # DATABASE or a whole-schema restore. The direct endpoint is the same
    # server without the pooler in front, so migrate through that.
    if "-pooler." in host:
        host = host.replace("-pooler.", ".")
        print("using the direct endpoint for the copy:", host)
    return {"host": host, "port": str(p.port or 5432),
            "user": unquote(p.username or ""), "password": unquote(p.password or ""),
            "database": (p.path or "/postgres").lstrip("/") or "postgres",
            "sslmode": "require" if "sslmode=require" in (p.query or "") else "prefer"}


def connect(cfg, dbname=None):
    kw = {k: v for k, v in cfg.items() if k in
          ("host", "port", "user", "password", "sslmode")}
    kw["database"] = dbname or cfg.get("database", "postgres")
    return psycopg2.connect(**kw)


def local_report():
    print("Local databases to copy:")
    ok = True
    for db in DATABASES:
        try:
            c = connect(LOCAL, db)
            cur = c.cursor()
            cur.execute("""SELECT table_name FROM information_schema.tables
                           WHERE table_schema='public' AND table_type='BASE TABLE'
                           ORDER BY 1""")
            names = [r[0] for r in cur.fetchall() if r[0] not in ("spatial_ref_sys",)]
            sizes = []
            for n in names:
                cur.execute(pgsql.SQL("SELECT COUNT(*) FROM public.{}").format(
                    pgsql.Identifier(n)))
                sizes.append("{} ({} rows)".format(n, cur.fetchone()[0]))
            print("  {:<16} {}".format(db, ", ".join(sizes) or "empty"))
            c.close()
        except Exception as e:
            ok = False
            print("  {:<16} CANNOT READ: {}".format(db, e))
    return ok


def ensure_database(target, dbname):
    con = connect(target, target["database"])
    con.autocommit = True
    cur = con.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
    if cur.fetchone():
        print("  database {!r} already there".format(dbname))
    else:
        cur.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(dbname)))
        print("  created database {!r}".format(dbname))
    con.close()


def ensure_postgis(target, dbname):
    con = connect(target, dbname)
    con.autocommit = True
    cur = con.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    cur.execute("SELECT postgis_version()")
    print("  postgis:", cur.fetchone()[0])
    con.close()


def copy_database(pg_dump, psql, target, dbname):
    dump_cmd = [pg_dump,
                "--host", LOCAL["host"], "--port", str(LOCAL["port"]),
                "--username", LOCAL["user"], "--dbname", dbname,
                "--no-owner", "--no-privileges", "--no-acl",
                "--clean", "--if-exists"]
    for t in SKIP_TABLES:
        dump_cmd += ["--exclude-table", t]

    # Everything user-supplied gets percent-encoded - "sarawak basin" has a
    # space in it, and psql rejects a URL that carries one literally.
    target_url = "postgresql://{u}:{p}@{h}:{P}/{d}?sslmode={s}".format(
        u=quote(target["user"], safe=""), p=quote(target["password"], safe=""),
        h=target["host"], P=target["port"],
        d=quote(dbname, safe=""), s=target["sslmode"])
    load_cmd = [psql, "--dbname", target_url, "--quiet",
                "--set", "ON_ERROR_STOP=0", "--file", "-"]

    env = dict(os.environ)
    env["PGPASSWORD"] = LOCAL["password"]

    print("  copying ...")
    dump = subprocess.Popen(dump_cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    load = subprocess.Popen(load_cmd, stdin=dump.stdout,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dump.stdout.close()
    out, err = load.communicate()
    dump.wait()
    derr = dump.stderr.read().decode("utf-8", "replace").strip()
    if derr:
        print("  pg_dump:", derr[:500])
    lerr = err.decode("utf-8", "replace").strip()
    if lerr:
        # Extension noise is expected and harmless; anything else is worth eyes.
        interesting = [l for l in lerr.splitlines()
                       if "extension" not in l.lower() and "already exists" not in l.lower()]
        if interesting:
            print("  psql:", "\n        ".join(interesting[:15]))
    return dump.returncode == 0


def verify(target, dbname):
    con = connect(target, dbname)
    cur = con.cursor()
    cur.execute("""SELECT table_name FROM information_schema.tables
                   WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1""")
    names = [r[0] for r in cur.fetchall() if r[0] != "spatial_ref_sys"]
    for n in names:
        cur.execute(pgsql.SQL("SELECT COUNT(*) FROM public.{}").format(pgsql.Identifier(n)))
        print("    {:<22} {} rows".format(n, cur.fetchone()[0]))
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", help="DATABASE_URL of the managed Postgres")
    ap.add_argument("--check", action="store_true",
                    help="just report what is here and which tools were found")
    args = ap.parse_args()

    pg_dump, psql = find_tool("pg_dump"), find_tool("psql")
    print("pg_dump:", pg_dump or "NOT FOUND")
    print("psql   :", psql or "NOT FOUND")
    print()
    local_ok = local_report()
    print()

    if args.check:
        if not (pg_dump and psql):
            print("Install the PostgreSQL client tools, or add their bin folder to PATH.")
        print("Check only - nothing was copied.")
        return 0 if (local_ok and pg_dump and psql) else 1

    if not args.target:
        print("Give me the target: --target \"postgresql://...\"")
        return 2
    if not (pg_dump and psql):
        print("pg_dump/psql not found - cannot copy.")
        return 2

    target = parse_target(args.target)
    print("Target host:", target["host"], "as", target["user"])
    print()
    for db in DATABASES:
        print(db)
        try:
            ensure_database(target, db)
            ensure_postgis(target, db)
            copy_database(pg_dump, psql, target, db)
            print("  now on the target:")
            verify(target, db)
        except Exception as e:
            print("  FAILED:", e)
        print()
    print("Done. Set DATABASE_URL on your host to the same URL and redeploy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
