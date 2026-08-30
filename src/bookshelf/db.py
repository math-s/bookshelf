"""SQLite storage: connection handling and versioned, in-place migrations.

The schema deliberately splits provider-owned data (`books`) from user-owned data
(`user_books`, `tags`). Refetching metadata rewrites the former and never touches
the latter, so a re-import can't destroy a rating or a note.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = "data/bookshelf.db"

STATUSES = ("want", "reading", "read", "abandoned")

# Each migration is (version, SQL). Applied in order; every DB records the highest
# version it has reached, so a DB on a persistent disk upgrades in place on boot.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE books (
            isbn13         TEXT PRIMARY KEY,
            isbn10         TEXT,
            title          TEXT NOT NULL DEFAULT '',
            subtitle       TEXT,
            authors        TEXT NOT NULL DEFAULT '[]',   -- JSON array
            categories     TEXT NOT NULL DEFAULT '[]',   -- JSON array
            publisher      TEXT,
            published_date TEXT,
            description    TEXT,
            page_count     INTEGER,
            language       TEXT,
            thumbnail_url  TEXT,
            preview_link   TEXT,
            source         TEXT NOT NULL DEFAULT 'manual',
            raw_json       TEXT,
            fetched_at     TEXT,
            created_at     TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE user_books (
            isbn13      TEXT PRIMARY KEY REFERENCES books(isbn13) ON DELETE CASCADE,
            status      TEXT NOT NULL DEFAULT 'want'
                        CHECK (status IN ('want','reading','read','abandoned')),
            rating      INTEGER CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
            notes       TEXT,
            started_on  TEXT,
            finished_on TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE tags (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE
        );

        CREATE TABLE book_tags (
            isbn13 TEXT NOT NULL REFERENCES books(isbn13) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (isbn13, tag_id)
        );

        CREATE TABLE lookup_cache (
            isbn13        TEXT NOT NULL,
            provider      TEXT NOT NULL,
            response_json TEXT,
            ok            INTEGER NOT NULL DEFAULT 0,
            fetched_at    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (isbn13, provider)
        );

        CREATE TABLE import_runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at   TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at  TEXT,
            total        INTEGER NOT NULL DEFAULT 0,
            ok_count     INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE import_failures (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id    INTEGER NOT NULL REFERENCES import_runs(id) ON DELETE CASCADE,
            raw_input TEXT,
            isbn13    TEXT,
            reason    TEXT
        );

        CREATE INDEX idx_user_books_status ON user_books(status);
        CREATE INDEX idx_user_books_rating ON user_books(rating);
        CREATE INDEX idx_book_tags_tag     ON book_tags(tag_id);
        CREATE INDEX idx_books_title       ON books(title COLLATE NOCASE);

        -- External-content FTS index: the text lives in `books`, FTS only indexes it.
        CREATE VIRTUAL TABLE books_fts USING fts5(
            title, subtitle, authors, description, publisher,
            content='books', content_rowid='rowid'
        );

        CREATE TRIGGER books_ai AFTER INSERT ON books BEGIN
            INSERT INTO books_fts(rowid, title, subtitle, authors, description, publisher)
            VALUES (new.rowid, new.title, new.subtitle, new.authors, new.description, new.publisher);
        END;

        CREATE TRIGGER books_ad AFTER DELETE ON books BEGIN
            INSERT INTO books_fts(books_fts, rowid, title, subtitle, authors, description, publisher)
            VALUES ('delete', old.rowid, old.title, old.subtitle, old.authors, old.description, old.publisher);
        END;

        CREATE TRIGGER books_au AFTER UPDATE ON books BEGIN
            INSERT INTO books_fts(books_fts, rowid, title, subtitle, authors, description, publisher)
            VALUES ('delete', old.rowid, old.title, old.subtitle, old.authors, old.description, old.publisher);
            INSERT INTO books_fts(rowid, title, subtitle, authors, description, publisher)
            VALUES (new.rowid, new.title, new.subtitle, new.authors, new.description, new.publisher);
        END;
        """,
    ),
]


def db_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the DB location: explicit arg > BOOKSHELF_DB env > default."""
    return Path(explicit or os.environ.get("BOOKSHELF_DB") or DEFAULT_DB_PATH)


def connect(path: str | os.PathLike[str] | None = None, *, migrate: bool = True) -> sqlite3.Connection:
    """Open a connection with sane pragmas, creating and migrating as needed."""
    resolved = db_path(path)
    if resolved.parent and str(resolved.parent) not in ("", "."):
        resolved.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(resolved), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL keeps the UI readable while an import is writing.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    if migrate:
        apply_migrations(conn)
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return row["v"] or 0


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply any migrations newer than the DB's recorded version. Idempotent."""
    version = current_version(conn)
    for target, sql in MIGRATIONS:
        if target <= version:
            continue
        with conn:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (target,))
        version = target
    return version


@contextmanager
def session(path: str | os.PathLike[str] | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


# --- helpers shared by the CLI and the API -------------------------------------

def json_list(value: str | None) -> list[str]:
    """Decode a JSON array column, tolerating nulls and legacy junk."""
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError):
        return []
    return [str(v) for v in decoded] if isinstance(decoded, list) else []


def set_tags(conn: sqlite3.Connection, isbn13: str, names: list[str]) -> list[str]:
    """Replace a book's tag set. Tag names are trimmed, de-duped case-insensitively,
    and created on demand. Returns the resulting tag names."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for name in names:
        trimmed = (name or "").strip()
        if not trimmed or trimmed.casefold() in seen:
            continue
        seen.add(trimmed.casefold())
        cleaned.append(trimmed)

    conn.execute("DELETE FROM book_tags WHERE isbn13 = ?", (isbn13,))
    for name in cleaned:
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO book_tags (isbn13, tag_id) VALUES (?, ?)",
            (isbn13, row["id"]),
        )
    # Tags nothing points at any more would otherwise clutter the sidebar forever.
    conn.execute(
        "DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM book_tags)"
    )
    return cleaned


def tags_for(conn: sqlite3.Connection, isbn13: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT t.name FROM tags t
        JOIN book_tags bt ON bt.tag_id = t.id
        WHERE bt.isbn13 = ? ORDER BY t.name COLLATE NOCASE
        """,
        (isbn13,),
    ).fetchall()
    return [r["name"] for r in rows]
