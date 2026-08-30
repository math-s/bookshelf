# Bookshelf

A personal library. Feed it ISBNs, it fetches the book metadata, and you get a
UI to browse, filter and annotate your shelf.

- **CLI** for bulk ingest, refetching and export.
- **JSON API** (FastAPI + SQLite) for the UI's reads and edits.
- **Web UI** with toggleable grid and table views, faceted filters, full-text
  search, and a phone barcode scanner.

Your own layer — reading status, tags, rating, notes, start/finish dates — lives
in a separate table from the provider metadata, so re-fetching a book can never
overwrite what you wrote about it.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env          # then set GOOGLE_BOOKS_API_KEY
export $(grep -v '^#' .env | xargs)

bookshelf init
bookshelf import my-isbns.txt   # or: pbpaste | bookshelf import -
bookshelf serve                 # http://127.0.0.1:8000
```

`my-isbns.txt` is one ISBN per line. ISBN-10 and ISBN-13 both work, hyphens are
fine, `#` lines are comments, and duplicates collapse — an ISBN-10 and its
ISBN-13 are recognised as the same book.

## About the Google Books API key

**Get a key.** The Books API's anonymous quota is frequently *zero*, not the
old ~1000/day — requests without a key come back `429` immediately. Create one
at [Google Cloud credentials](https://console.cloud.google.com/apis/credentials)
with the Books API enabled, and set `GOOGLE_BOOKS_API_KEY`.

Because that failure mode is common, lookups fall back to
[OpenLibrary](https://openlibrary.org), which needs no key. So the system works
without a Google key — you just get OpenLibrary's slightly thinner metadata
(no language field, no editorial description). Each book records which provider
it came from, and `bookshelf refetch` will upgrade them once a key is in place.

Control the order explicitly with `--provider google,openlibrary`.

## CLI

| Command | What it does |
| --- | --- |
| `bookshelf init` | Create or migrate the database |
| `bookshelf import <file\|->` | Import ISBNs. `--dry-run`, `--refresh`, `--delay`, `--json` |
| `bookshelf refetch` | Refresh provider metadata. `--stale-days N`, `--missing-only` |
| `bookshelf list [query]` | Query from the terminal. `--status`, `--tag`, `--author`, `--rating-min` |
| `bookshelf export` | Dump everything. `--format json\|csv` |
| `bookshelf stats` | Totals, pages read, average rating, breakdowns |
| `bookshelf serve` | Run the API and UI |

Repeated `--tag` flags are ANDed: `--tag owned --tag sci-fi` means both.

## API

All endpoints are under `/api`. `GET /api/docs` serves the generated OpenAPI page.

```
GET    /api/books        ?q=&status=&tag=&author=&language=&rating_min=&untagged=
                         &sort=title|author|added|rating|published|pages&order=&limit=&offset=
GET    /api/books/{isbn13}
PATCH  /api/books/{isbn13}          status, rating, notes, started_on, finished_on
PUT    /api/books/{isbn13}/tags     replaces the tag set
DELETE /api/books/{isbn13}
POST   /api/import                  {"text": "..."} or {"isbns": [...]}
GET    /api/tags   GET /api/stats   GET /healthz
```

Search goes through SQLite FTS5 with prefix matching; input is tokenised and
quoted, so punctuation in the search box can't produce a query error.

## Deploying

The UI is static and the API is not, so they deploy separately: **GitHub Pages**
for the frontend, **Fly.io** for the API and its SQLite volume.

### API on Fly.io

```bash
fly launch --no-deploy --copy-config
fly volumes create bookshelf_data --size 1 --region <your-region>

fly secrets set \
  GOOGLE_BOOKS_API_KEY=... \
  BOOKSHELF_TOKEN=$(openssl rand -hex 24) \
  BOOKSHELF_CORS_ORIGINS=https://<your-user>.github.io

fly deploy
```

The volume matters: SQLite lives at `/data/bookshelf.db`, and without a mounted
volume every deploy would start from an empty library. Keep the app on a single
machine — one SQLite file cannot be shared across several.

### UI on GitHub Pages

Enable Pages for the repo (Settings → Pages → Source: GitHub Actions), then set
a repository **variable** `BOOKSHELF_API_BASE` to your Fly URL
(e.g. `https://bookshelf.fly.dev`). The workflow in
`.github/workflows/pages.yml` bakes it into `config.js` at deploy time, so
visitors don't have to configure anything.

Without that variable the page still works — open the ⚙ Settings dialog and
enter the API URL and token by hand; both are stored in that browser only.

### Two things to get right

**CORS.** `BOOKSHELF_CORS_ORIGINS` must list your Pages origin exactly
(`https://user.github.io`, no path, no trailing slash) or the browser blocks
every request. It is comma-separated; add `http://localhost:8000` for local work.

**The token.** A public Pages frontend talking to a public Fly API is open to
anyone who finds the URL, and the API can write and delete. **Set
`BOOKSHELF_TOKEN`.** Requests must then carry `X-Bookshelf-Token`.

Be clear-eyed about what that gets you: the token is entered in the browser and
kept in `localStorage`, so it protects against a passer-by, not against someone
using a browser you left unlocked. For anything stronger, keep the API private
(Tailscale, or Fly's private networking) rather than relying on the header.

## Barcode scanning

`/scan.html` reads EAN-13 barcodes — which, for books, *are* ISBN-13s. It uses
the browser's native `BarcodeDetector` where available and falls back to ZXing
from a CDN elsewhere (iOS Safari needs the fallback).

Camera access requires a **secure context — HTTPS or localhost**. Over plain
HTTP on a phone the camera silently won't start; the page says so and offers a
manual entry box. Scans queue up, are flagged if already in your library, and
import in one batch.

## Development

```bash
pytest              # 93 tests, none touch the network
```

Provider parsing is tested against recorded payloads in `tests/fixtures/`,
including the Google-429 case that proves the OpenLibrary fallback fires.

```
src/bookshelf/
  isbn.py        validation, checksums, ISBN-10 -> ISBN-13
  db.py          schema and versioned migrations
  providers/     google.py, openlibrary.py, shared BookRecord
  enrich.py      cache -> provider chain -> upsert
  queries.py     filter/sort/search, shared by CLI and API
  cli.py         subcommands
  server.py      FastAPI app
web/             the UI — plain HTML/CSS/JS, no build step
```

### Schema

`books` holds provider metadata and is rewritten freely on refetch. `user_books`,
`tags` and `book_tags` hold your data and are never touched by a fetch.
`lookup_cache` stores raw provider responses so re-imports and retries cost no
API quota. `import_runs` and `import_failures` keep a record of what went wrong
in a batch instead of dropping bad lines silently.
