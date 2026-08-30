"""The ingest pipeline: ISBN -> cache or provider chain -> normalised row -> DB.

Two rules drive the design:
  * every provider response is cached, so re-imports and retries cost no quota;
  * upserting metadata never writes to `user_books`, so ratings and notes survive.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from .isbn import ParsedISBN, parse_many
from .providers import BookRecord, NotFound, Provider, ProviderError, RateLimited, build_chain

# Columns in `books` that a refetch is allowed to overwrite.
_BOOK_COLUMNS = (
    "isbn10", "title", "subtitle", "authors", "categories", "publisher",
    "published_date", "description", "page_count", "language",
    "thumbnail_url", "preview_link", "source", "raw_json",
)


@dataclass
class ImportResult:
    run_id: int | None = None
    total: int = 0
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)          # already present, not refreshed
    failures: list[tuple[str, str]] = field(default_factory=list)  # (raw/isbn, reason)

    @property
    def ok_count(self) -> int:
        return len(self.added) + len(self.updated) + len(self.skipped)

    @property
    def failed_count(self) -> int:
        return len(self.failures)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "total": self.total,
            "added": self.added,
            "updated": self.updated,
            "skipped": self.skipped,
            "failures": [{"input": raw, "reason": reason} for raw, reason in self.failures],
            "ok_count": self.ok_count,
            "failed_count": self.failed_count,
        }


def cache_get(conn: sqlite3.Connection, isbn13: str, provider: str) -> dict | None:
    row = conn.execute(
        "SELECT response_json FROM lookup_cache WHERE isbn13 = ? AND provider = ? AND ok = 1",
        (isbn13, provider),
    ).fetchone()
    if not row or not row["response_json"]:
        return None
    try:
        return json.loads(row["response_json"])
    except ValueError:
        return None


def cache_put(conn: sqlite3.Connection, isbn13: str, provider: str, payload: dict | None, ok: bool) -> None:
    conn.execute(
        """
        INSERT INTO lookup_cache (isbn13, provider, response_json, ok, fetched_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(isbn13, provider) DO UPDATE SET
            response_json = excluded.response_json,
            ok            = excluded.ok,
            fetched_at    = excluded.fetched_at
        """,
        (isbn13, provider, json.dumps(payload, ensure_ascii=False) if payload else None, 1 if ok else 0),
    )


def lookup(
    conn: sqlite3.Connection,
    isbn13: str,
    chain: Sequence[Provider],
    *,
    use_cache: bool = True,
    delay: float = 0.0,
    max_retries: int = 2,
    sleep: Callable[[float], None] = time.sleep,
) -> BookRecord:
    """Resolve one ISBN through the provider chain, raising ProviderError if all fail.

    A rate-limited provider is retried with exponential backoff before the chain
    falls through to the next one, so a brief 429 doesn't silently downgrade the
    source but a sustained one doesn't stall the whole import either.
    """
    reasons: list[str] = []

    for provider in chain:
        if use_cache:
            cached = cache_get(conn, isbn13, provider.name)
            if cached is not None:
                try:
                    return provider.parse(cached, isbn13)
                except ProviderError:
                    pass  # cached payload no longer parses; re-fetch below

        payload = None
        for attempt in range(max_retries + 1):
            try:
                payload = provider.fetch(isbn13)
                break
            except RateLimited as exc:
                if attempt < max_retries:
                    sleep(min(2.0 ** attempt, 8.0))
                    continue
                reasons.append(str(exc))
            except NotFound as exc:
                cache_put(conn, isbn13, provider.name, None, ok=False)
                reasons.append(str(exc))
                break
            except ProviderError as exc:
                reasons.append(str(exc))
                break

        if payload is None:
            continue

        try:
            record = provider.parse(payload, isbn13)
        except ProviderError as exc:
            reasons.append(str(exc))
            continue

        cache_put(conn, isbn13, provider.name, payload, ok=True)
        if delay:
            sleep(delay)
        return record

    raise ProviderError("; ".join(reasons) or f"no provider could resolve {isbn13}")


def upsert_book(conn: sqlite3.Connection, record: BookRecord) -> str:
    """Write provider metadata. Returns 'added' or 'updated'.

    Creates an empty `user_books` row for new books but never modifies an existing
    one — that table belongs to the user, not the provider.
    """
    row = record.to_row()
    existing = conn.execute(
        "SELECT 1 FROM books WHERE isbn13 = ?", (record.isbn13,)
    ).fetchone()

    if existing:
        assignments = ", ".join(f"{col} = :{col}" for col in _BOOK_COLUMNS)
        conn.execute(
            f"UPDATE books SET {assignments}, fetched_at = datetime('now'), "
            f"updated_at = datetime('now') WHERE isbn13 = :isbn13",
            row,
        )
        action = "updated"
    else:
        columns = ("isbn13",) + _BOOK_COLUMNS
        placeholders = ", ".join(f":{col}" for col in columns)
        conn.execute(
            f"INSERT INTO books ({', '.join(columns)}, fetched_at) "
            f"VALUES ({placeholders}, datetime('now'))",
            row,
        )
        action = "added"

    conn.execute("INSERT OR IGNORE INTO user_books (isbn13) VALUES (?)", (record.isbn13,))
    return action


def import_isbns(
    conn: sqlite3.Connection,
    text_or_isbns: str | Iterable[str],
    *,
    providers: list[str] | None = None,
    chain: Sequence[Provider] | None = None,
    refresh: bool = False,
    dry_run: bool = False,
    delay: float = 0.0,
    on_progress: Callable[[int, int, str, str], None] | None = None,
) -> ImportResult:
    """Import a blob of text or a list of ISBN strings.

    Books already present are skipped unless `refresh=True`. Unparseable lines and
    provider failures are recorded against the run rather than dropped.
    """
    if isinstance(text_or_isbns, str):
        raw_text = text_or_isbns
    else:
        raw_text = "\n".join(str(v) for v in text_or_isbns)

    parsed, bad_lines = parse_many(raw_text)
    resolved_chain = list(chain) if chain is not None else build_chain(providers)

    result = ImportResult(total=len(parsed) + len(bad_lines))
    result.failures.extend(bad_lines)

    if dry_run:
        for item in parsed:
            present = conn.execute(
                "SELECT 1 FROM books WHERE isbn13 = ?", (item.isbn13,)
            ).fetchone()
            if present and not refresh:
                result.skipped.append(item.isbn13)
            else:
                result.updated.append(item.isbn13) if present else result.added.append(item.isbn13)
        return result

    cur = conn.execute("INSERT INTO import_runs (total) VALUES (?)", (result.total,))
    result.run_id = cur.lastrowid
    conn.commit()

    for index, item in enumerate(parsed, start=1):
        action = _import_one(conn, item, resolved_chain, refresh=refresh, delay=delay, result=result)
        if on_progress:
            on_progress(index, len(parsed), item.isbn13, action)

    for raw, reason in result.failures:
        conn.execute(
            "INSERT INTO import_failures (run_id, raw_input, reason) VALUES (?, ?, ?)",
            (result.run_id, raw, reason),
        )
    conn.execute(
        "UPDATE import_runs SET finished_at = datetime('now'), ok_count = ?, failed_count = ? WHERE id = ?",
        (result.ok_count, result.failed_count, result.run_id),
    )
    conn.commit()
    return result


def _import_one(conn, item: ParsedISBN, chain, *, refresh: bool, delay: float, result: ImportResult) -> str:
    present = conn.execute("SELECT 1 FROM books WHERE isbn13 = ?", (item.isbn13,)).fetchone()
    if present and not refresh:
        result.skipped.append(item.isbn13)
        return "skipped"

    try:
        record = lookup(conn, item.isbn13, chain, use_cache=not refresh, delay=delay)
    except ProviderError as exc:
        result.failures.append((item.raw, str(exc)))
        conn.commit()
        return "failed"

    # The provider may not echo an ISBN-10; keep the one we derived from the input.
    if not record.isbn10 and item.isbn10:
        record.isbn10 = item.isbn10

    action = upsert_book(conn, record)
    (result.added if action == "added" else result.updated).append(item.isbn13)
    conn.commit()
    return action
