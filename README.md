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
| `bookshelf list [query]` | Query from the terminal. `--status`, `--tag`, `--author`, `--rating-min`, `--by-author` |
| `bookshelf authors` | List authors with book counts and reading progress; `--merge FROM INTO` |
| `bookshelf export` | Dump everything. `--format json\|csv` |
| `bookshelf stats` | Totals, pages read, average rating, breakdowns |
| `bookshelf serve` | Run the API and UI |

Repeated `--tag` flags are ANDed: `--tag owned --tag sci-fi` means both.

## Authors

Authors are a real table, not a JSON blob on the book, so you can group a shelf
by person:

```bash
bookshelf list --by-author                 # every author, most prolific first
bookshelf list --by-author --min-books 2   # skip the one-off authors
bookshelf authors                          # counts, how many you've read, avg rating
```

In the UI the **By author** view does the same thing, and any author name — in
the sidebar, a group heading, or a book's detail drawer — filters the library
down to that person.

### Why names need normalising

Providers spell the same person differently between editions. Names are reduced
to a grouping key that folds case, accents, punctuation and initial spacing, so
`J.R.R. Tolkien` and `J. R. R. Tolkien` are one author rather than two.

That deliberately stops short of guessing. A transliteration like `Roal'd Dal'`
next to `Roald Dahl` is one person but two unrelated strings — no safe rule
merges those, so you say so explicitly:

```bash
bookshelf authors --merge "Roal'd Dal'" "Roald Dahl"
```

The merge records an alias, so the **next refetch re-links the old spelling to
the canonical author** instead of recreating the duplicate.

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

### Order matters

CORS needs the Pages origin and Pages needs the Fly URL, which looks circular.
It isn't: your Pages origin is predictable from your username, so set it during
the Fly step, before Pages exists.

### 1. API on Fly.io

```bash
fly launch --no-deploy --copy-config
```

`fly.toml` ships with `app = "bookshelf"`. App names are global across Fly, so
that one is almost certainly taken and `fly launch` will prompt for another —
whatever you pick becomes your URL. `primary_region` is `gru` (São Paulo);
change it if that isn't near you.

**Create the volume before the first deploy.** SQLite lives on it, and without
it every release starts from an empty library:

```bash
fly volumes create bookshelf_data --size 1 --region <your-region>
```

The name must stay `bookshelf_data` to match the `[mounts]` block.

```bash
fly secrets set \
  GOOGLE_BOOKS_API_KEY=... \
  BOOKSHELF_TOKEN=$(openssl rand -hex 24) \
  BOOKSHELF_CORS_ORIGINS=https://<your-user>.github.io,http://localhost:8000

fly deploy
curl https://<your-app>.fly.dev/healthz     # {"status":"ok"}
```

Keep the app on a single machine — one SQLite file cannot be shared across
several.

### 2. UI on GitHub Pages

1. **Settings → Pages → Source: GitHub Actions.** Do this first. Until Pages is
   enabled the workflow fails at `actions/configure-pages`, which is the most
   likely reason a run went red before you ever touched the code.
2. **Settings → Secrets and variables → Actions → Variables** → add
   `BOOKSHELF_API_BASE` = `https://<your-app>.fly.dev`. The workflow bakes it
   into `config.js` at deploy time so visitors need no setup.
3. Re-run the workflow (Actions → the run → Re-run jobs), or push to `web/**`.

The UI lands at `https://<your-user>.github.io/<repo>/`.

Without the variable the page still works — open ⚙ Settings and enter the API
URL and token by hand; both stay in that browser only.

### 3. First use

Open the UI, click ⚙, and paste your `BOOKSHELF_TOKEN`. The API rejects
everything without it.

### Two things to get right

**CORS is an origin, not a URL.** `BOOKSHELF_CORS_ORIGINS` wants
`https://user.github.io` — scheme and host, no `/repo` path, no trailing slash.
Get it wrong and the browser blocks every request with no useful error.

**Set `BOOKSHELF_TOKEN`.** A public Pages frontend talking to a public Fly API
is reachable by anyone who finds the URL, and the API can write and delete.

Be clear-eyed about what the token buys: it is entered in the browser and kept
in `localStorage`, so it stops a passer-by, not someone using a browser you left
unlocked. For anything stronger, keep the API off the public internet (Tailscale,
or Fly private networking) rather than relying on the header.

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
pytest              # 134 tests, none touch the network
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

`authors`, `book_authors` and `author_aliases` are derived from the provider's
author list on every upsert — `books.authors` stays as the raw payload, feeding
the FTS index and acting as the source the relation is rebuilt from. Migrations
are versioned and applied in place, so an existing database gains the author
tables and backfills itself on the next `bookshelf init` or server boot.
`lookup_cache` stores raw provider responses so re-imports and retries cost no
API quota. `import_runs` and `import_failures` keep a record of what went wrong
in a batch instead of dropping bad lines silently.
