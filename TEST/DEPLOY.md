# Putting Database Chat online

## Why Netlify gave you "Page not found"

Netlify hosts **static files** — HTML, CSS, images. This app is not static:

- The page does not exist as a file. Flask builds it at runtime from the
  `HTML_PAGE` string in `chatbox.py` when a browser asks for `/`.
- Every action calls back to Python: `/api/databases`, `/api/schema`,
  `/api/ask`, `/api/export`.
- The data lives in PostgreSQL **on your own PC**. Nothing on the internet
  can reach `localhost:5432`.

Netlify's serverless functions run JavaScript and Go, not Python, so there is
no small fix. The app needs a host that runs Python. Everything below keeps
the app exactly as it is — same chat, same four export formats, both
databases — just reachable from anywhere.

You will use two free services:

| what | service | why |
|---|---|---|
| the database | **Neon** | free Postgres with PostGIS, and it lets you create more than one database — this app needs two |
| the web app | **Render** | runs Python, reads the `render.yaml` in this folder |

---

## 1. Create the database

1. Sign up at <https://neon.tech> and create a project.
2. Copy the connection string. It looks like:

   ```
   postgresql://user:PASSWORD@ep-something.aws.neon.tech/postgres?sslmode=require
   ```

Keep it somewhere safe for the next two steps. Treat it like a password.

## 2. Copy your data up

`migrate_to_host.py` creates both databases on the remote server, turns on
PostGIS, and copies every table across. Check what it will do first:

```bash
python migrate_to_host.py --check
```

That should list `sarawak basin` (314 rows) and `subbasin_gis` (345 + 314
rows), and find `pg_dump`. Then run it for real:

```bash
python migrate_to_host.py --target "postgresql://user:PASSWORD@ep-something.aws.neon.tech/postgres?sslmode=require"
```

It prints the row counts on the target when it finishes. They should match.

> If the host refuses `CREATE DATABASE`, put everything in one database
> instead: copy only `subbasin_gis` (it already contains `subbasin_district`,
> identical to the one in `sarawak basin`), and set
> `VISIBLE_DATABASES=subbasin_gis` in step 4.

## 3. Put the code on GitHub

```bash
git init
git add .
git commit -m "Database Chat"
```

Create an empty repository on GitHub, then follow the two commands it shows
you to push. `.gitignore` already keeps `.env` and `__pycache__` out.

## 4. Deploy on Render

1. Sign up at <https://render.com>, choose **New → Blueprint**, and pick your
   repository. Render reads `render.yaml` and fills in the build and start
   commands for you.
2. Set **`DATABASE_URL`** to the connection string from step 1. This is the
   only value you have to type; `render.yaml` handles the rest.
3. Deploy. The first build takes a few minutes — geopandas is a large install.

Your site appears at `https://<name>.onrender.com`.

## 5. Check it

- The dropdown lists **sarawak basin** and **subbasin_gis**
- "how many type of subbasin here" answers with names — Baleh 1, Rajang 2, …
- Export → Shapefile downloads a `.zip` that opens in QGIS

---

## Settings

All optional except `DATABASE_URL`. Set them in Render's dashboard.

| variable | default | what it does |
|---|---|---|
| `DATABASE_URL` | — | the managed Postgres connection string |
| `VISIBLE_DATABASES` | `sarawak basin,subbasin_gis` | which databases appear in the dropdown, in order. Empty shows all |
| `ALLOW_RAW_SQL` | `1` locally, `0` in `render.yaml` | whether visitors may type `SELECT …` into the chat box |
| `SECRET_KEY` | random each restart | signs download links. Set a fixed value so links survive a restart |
| `PORT` | `5000` | the host sets this itself |

Running locally still needs none of them — the defaults are your own
PostgreSQL, exactly as before.

## Things worth knowing

**Raw SQL is off in production.** `render.yaml` sets `ALLOW_RAW_SQL=0`, so
visitors can only ask in plain English. Downloads still work: the server signs
each answer's query and the export endpoint accepts only queries carrying that
signature. Set it to `1` if you want the SQL box back — but then anyone can
read anything in those two databases.

**No password is in the source.** Your local settings live in `.env`, which
`.gitignore` keeps out of the repo. All three scripts read it on startup, and
real environment variables always win — which is how Render overrides it with
`DATABASE_URL`. If you ever clone this repo somewhere fresh, copy
`.env.example` to `.env` and fill it in.

**Use a read-only database user.** Create one in Neon and put *that* user in
`DATABASE_URL`. The app never writes, so it never needs write permission:

```sql
CREATE USER webapp WITH PASSWORD 'something-long';
GRANT CONNECT ON DATABASE "sarawak basin" TO webapp;
GRANT USAGE ON SCHEMA public TO webapp;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO webapp;
```

Run it in both databases.

**One worker on purpose.** The schema cache and the follow-up memory ("show
them", "what about Miri") live in the process. Several workers would each keep
their own half-warm copy, so `render.yaml` uses one worker with four threads.
Follow-ups are per visitor — a cookie keeps two people's conversations apart.

**The free tier sleeps.** After ~15 minutes idle, Render parks the app and the
next visit takes up to a minute to wake it. The paid tier removes this.

**If the build runs out of space,** delete the last three lines of
`requirements.txt` (geopandas, shapely, pyogrio). Everything keeps working
except Shapefile export, which will say the server cannot build one. CSV, JSON
and GeoJSON are unaffected — and GeoJSON opens in QGIS just as well.
