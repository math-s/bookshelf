"""Command line interface: bulk ingest, refetch, export, stats, serve."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from . import __version__
from .db import connect, db_path, json_list, tags_for
from .enrich import import_isbns, lookup, upsert_book
from .providers import PROVIDERS, ProviderError, build_chain
from .queries import BookFilter, search, stats


def _read_input(source: str) -> str:
    """Read ISBNs from a file, or from stdin when the argument is '-'."""
    if source == "-":
        return sys.stdin.read()
    path = Path(source)
    if not path.exists():
        raise SystemExit(f"error: no such file: {source}")
    return path.read_text()


def _provider_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    names = [n.strip().lower() for n in value.split(",") if n.strip()]
    for name in names:
        if name not in PROVIDERS:
            raise SystemExit(f"error: unknown provider {name!r}; choose from {', '.join(PROVIDERS)}")
    return names


def cmd_init(args) -> int:
    with connect(args.db) as conn:
        pass
    print(f"Initialised {db_path(args.db)}")
    return 0


def cmd_import(args) -> int:
    text = _read_input(args.source)
    conn = connect(args.db)
    try:
        chain = build_chain(_provider_list(args.provider))
        if not args.quiet:
            names = ", ".join(p.name for p in chain)
            print(f"Providers: {names}", file=sys.stderr)

        def progress(index, total, isbn13, action):
            if not args.quiet:
                print(f"[{index}/{total}] {isbn13} {action}", file=sys.stderr)

        result = import_isbns(
            conn,
            text,
            chain=chain,
            refresh=args.refresh,
            dry_run=args.dry_run,
            delay=args.delay,
            on_progress=None if args.dry_run else progress,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
        return 0 if not result.failures else 1

    prefix = "Would import" if args.dry_run else "Imported"
    print(
        f"{prefix}: {len(result.added)} added, {len(result.updated)} updated, "
        f"{len(result.skipped)} already present, {result.failed_count} failed"
    )
    if result.failures:
        print("\nFailures:")
        for raw, reason in result.failures:
            print(f"  {raw}: {reason}")
    return 0 if not result.failures else 1


def cmd_refetch(args) -> int:
    conn = connect(args.db)
    try:
        if args.missing_only:
            rows = conn.execute(
                "SELECT isbn13 FROM books WHERE title = '' OR title IS NULL OR authors = '[]'"
            ).fetchall()
        elif args.stale_days is not None:
            rows = conn.execute(
                "SELECT isbn13 FROM books WHERE fetched_at IS NULL "
                "OR julianday('now') - julianday(fetched_at) >= ?",
                (args.stale_days,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT isbn13 FROM books").fetchall()

        isbns = [r["isbn13"] for r in rows]
        if not isbns:
            print("Nothing to refetch.")
            return 0

        chain = build_chain(_provider_list(args.provider))
        ok = failed = 0
        for index, isbn13 in enumerate(isbns, start=1):
            try:
                record = lookup(conn, isbn13, chain, use_cache=False, delay=args.delay)
            except ProviderError as exc:
                failed += 1
                print(f"[{index}/{len(isbns)}] {isbn13} failed: {exc}", file=sys.stderr)
                continue
            upsert_book(conn, record)
            conn.commit()
            ok += 1
            if not args.quiet:
                print(f"[{index}/{len(isbns)}] {isbn13} refreshed from {record.source}", file=sys.stderr)
        print(f"Refetched {ok} book(s), {failed} failed. Personal metadata untouched.")
        return 0 if not failed else 1
    finally:
        conn.close()


def cmd_export(args) -> int:
    conn = connect(args.db)
    try:
        result = search(conn, BookFilter(limit=100000))
        items = result["items"]
    finally:
        conn.close()

    if args.format == "json":
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return 0

    columns = [
        "isbn13", "isbn10", "title", "subtitle", "authors", "publisher",
        "published_date", "page_count", "language", "categories",
        "status", "rating", "notes", "started_on", "finished_on", "tags", "source",
    ]
    writer = csv.DictWriter(sys.stdout, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        row = dict(item)
        for key in ("authors", "categories", "tags"):
            row[key] = "; ".join(row.get(key) or [])
        writer.writerow(row)
    return 0


def cmd_stats(args) -> int:
    conn = connect(args.db)
    try:
        data = stats(conn)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    print(f"Books:          {data['total_books']}")
    print(f"Tags:           {data['tag_count']}")
    print(f"Pages read:     {data['pages_read']}")
    print(f"Average rating: {data['average_rating'] if data['average_rating'] is not None else '-'}")
    if data["by_status"]:
        print("\nBy status:")
        for status, count in sorted(data["by_status"].items(), key=lambda kv: -kv[1]):
            print(f"  {status:<10} {count}")
    if data["by_source"]:
        print("\nBy source:")
        for source, count in data["by_source"].items():
            print(f"  {source:<12} {count}")
    return 0


def cmd_list(args) -> int:
    conn = connect(args.db)
    try:
        result = search(
            conn,
            BookFilter(
                q=args.query,
                status=args.status or [],
                tags=args.tag or [],
                author=args.author,
                rating_min=args.rating_min,
                sort=args.sort,
                order=args.order,
                limit=args.limit,
            ),
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(result["items"], indent=2, ensure_ascii=False))
        return 0

    if not result["items"]:
        print("No books matched.")
        return 0
    for item in result["items"]:
        authors = ", ".join(item["authors"]) or "unknown"
        rating = f" {'*' * item['rating']}" if item.get("rating") else ""
        tags = f"  [{', '.join(item['tags'])}]" if item["tags"] else ""
        print(f"{item['isbn13']}  {item['title'][:52]:<52} {authors[:28]:<28} {item['status']:<9}{rating}{tags}")
    print(f"\n{len(result['items'])} of {result['total']} book(s)")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .server import create_app

    app = create_app(args.db)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bookshelf",
        description="Ingest ISBNs, enrich them from book metadata providers, browse and filter.",
    )
    parser.add_argument("--version", action="version", version=f"bookshelf {__version__}")
    parser.add_argument("--db", help="SQLite path (default: $BOOKSHELF_DB or data/bookshelf.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create or migrate the database")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import", help="import ISBNs from a file or stdin")
    p.add_argument("source", help="path to a file of ISBNs, one per line, or '-' for stdin")
    p.add_argument("--provider", help="comma-separated provider order (default: google,openlibrary)")
    p.add_argument("--refresh", action="store_true", help="re-fetch books already in the library")
    p.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    p.add_argument("--delay", type=float, default=0.0, help="seconds to pause between lookups")
    p.add_argument("--json", action="store_true", help="emit the run summary as JSON")
    p.add_argument("--quiet", "-q", action="store_true", help="suppress per-book progress")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("refetch", help="refresh provider metadata for books already stored")
    p.add_argument("--stale-days", type=float, help="only books not fetched in this many days")
    p.add_argument("--missing-only", action="store_true", help="only books with no title or authors")
    p.add_argument("--provider", help="comma-separated provider order")
    p.add_argument("--delay", type=float, default=0.0)
    p.add_argument("--quiet", "-q", action="store_true")
    p.set_defaults(func=cmd_refetch)

    p = sub.add_parser("export", help="dump the library to stdout")
    p.add_argument("--format", choices=("json", "csv"), default="json")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("list", help="query the library from the terminal")
    p.add_argument("query", nargs="?", help="full-text search term")
    p.add_argument("--status", action="append", help="filter by status (repeatable)")
    p.add_argument("--tag", action="append", help="filter by tag (repeatable, ANDed)")
    p.add_argument("--author")
    p.add_argument("--rating-min", type=int)
    p.add_argument("--sort", default="title")
    p.add_argument("--order", default="asc", choices=("asc", "desc"))
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("stats", help="summarise the library")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("serve", help="run the web UI and JSON API")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
