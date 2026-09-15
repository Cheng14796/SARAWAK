# legacy

Two earlier read-only table viewers, kept for reference. **Neither is the
deployed site** - that is `chatbox.py` at the repo root, which is what the
`Procfile` and `render.yaml` start.

| file | what it was |
| --- | --- |
| `index.py` | the first viewer: one hard-coded database, tables rendered as HTML |
| `db_reader.py` | the same idea with database auto-discovery |

Both import `_load_dotenv` from `chatbox.py`, so if you want to run one, run it
from the repo root:

```bash
python -m legacy.db_reader
```

They have not been given the connection pooling, rate limiting, error handling
or security headers that `chatbox.py` has, so do not put either on a public
host.
