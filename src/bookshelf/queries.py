"""Filter/sort/search over the joined book + user view.

Lives in one place so the API and the CLI can't drift apart on what a filter means.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .db import json_list

SORT_COLUMNS = {
    "title": "b.title COLLATE NOCASE",
    "author": "json_extract(b.authors, '$[0]') COLLATE NOCASE",
    "added": "b.created_at",
    "updated": "u.updated_at",
    "rating": "u.rating",
    "published": "b.published_date",
    "pages": "b.page_count",
}

DEFAULT_SORT = "title"
MAX_LIMIT = 500


@dataclass
class BookFilter:
    q: str | None = None
    status: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    author: str | None = None
    language: str | None = None
    rating_min: int | None = None
    untagged: bool = False
    sort: str = DEFAULT_SORT
    order: str = "asc"
    limit: int = 100
    offset: int = 0

    def normalised(self) -> "BookFilter":
        self.sort = self.sort if self.sort in SORT_COLUMNS else DEFAULT_SORT
        self.order = "desc" if str(self.order).lower() == "desc" else "asc"
        self.limit = max(1, min(int(self.limit or 100), MAX_LIMIT))
        self.offset = max(0, int(self.offset or 0))
        if self.rating_min is not None:
            self.rating_min = max(1, min(int(self.rating_min), 5))
        return self


def _where(f: BookFilter) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if f.q:
        # FTS5 handles the ranking; escape the term so punctuation can't break the query.
        clauses.append(
            "b.rowid IN (SELECT rowid FROM books_fts WHERE books_fts MATCH ?)"
        )
        params.append(_fts_query(f.q))

    if f.status:
        clauses.append(f"u.status IN ({', '.join('?' * len(f.status))})")
        params.extend(f.status)

    if f.rating_min is not None:
        clauses.append("u.rating >= ?")
        params.append(f.rating_min)

    if f.author:
        # authors is a JSON array; match any element case-insensitively.
        clauses.append(
            "EXISTS (SELECT 1 FROM json_each(b.authors) WHERE json_each.value LIKE ?)"
        )
        params.append(f"%{f.author}%")

    if f.language:
        clauses.append("b.language = ?")
        params.append(f.language)

    if f.untagged:
        clauses.append("NOT EXISTS (SELECT 1 FROM book_tags bt WHERE bt.isbn13 = b.isbn13)")

    for tag in f.tags:
        # Repeated tags AND together: "sci-fi" + "owned" means both, not either.
        clauses.append(
            """
            EXISTS (SELECT 1 FROM book_tags bt JOIN tags t ON t.id = bt.tag_id
                    WHERE bt.isbn13 = b.isbn13 AND t.name = ? COLLATE NOCASE)
            """
        )
        params.append(tag)

    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def _fts_query(text: str) -> str:
    """Turn user input into a safe FTS5 prefix query.

    Every token is quoted so characters FTS treats as operators (-, ", *, :, ^)
    can't produce a syntax error on something the user typed in a search box.
    """
    tokens = [t for t in "".join(c if c.isalnum() else " " for c in text).split() if t]
    if not tokens:
        return '""'
    return " AND ".join(f'"{t}"*' for t in tokens)


BASE_SELECT = """
SELECT b.isbn13, b.isbn10, b.title, b.subtitle, b.authors, b.categories,
       b.publisher, b.published_date, b.description, b.page_count, b.language,
       b.thumbnail_url, b.preview_link, b.source, b.created_at, b.fetched_at,
       u.status, u.rating, u.notes, u.started_on, u.finished_on, u.updated_at,
       (SELECT group_concat(t.name, char(31)) FROM tags t
         JOIN book_tags bt ON bt.tag_id = t.id
        WHERE bt.isbn13 = b.isbn13) AS tag_names
  FROM books b
  LEFT JOIN user_books u ON u.isbn13 = b.isbn13
"""


def search(conn: sqlite3.Connection, f: BookFilter) -> dict[str, Any]:
    """Run the filter, returning items, total count, and sidebar facets."""
    f = f.normalised()
    where, params = _where(f)

    total = conn.execute(
        f"SELECT COUNT(*) FROM books b LEFT JOIN user_books u ON u.isbn13 = b.isbn13{where}",
        params,
    ).fetchone()[0]

    # NULLs (unrated, undated) always sort last rather than bunching at the top.
    order_sql = f"{SORT_COLUMNS[f.sort]} IS NULL, {SORT_COLUMNS[f.sort]} {f.order.upper()}"
    rows = conn.execute(
        f"{BASE_SELECT}{where} ORDER BY {order_sql}, b.title COLLATE NOCASE LIMIT ? OFFSET ?",
        [*params, f.limit, f.offset],
    ).fetchall()

    return {
        "items": [row_to_book(r) for r in rows],
        "total": total,
        "limit": f.limit,
        "offset": f.offset,
        "facets": facets(conn),
    }


def row_to_book(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["authors"] = json_list(data.get("authors"))
    data["categories"] = json_list(data.get("categories"))
    # Unit separator: safe against commas inside tag names.
    raw_tags = data.pop("tag_names", None)
    data["tags"] = sorted(raw_tags.split("\x1f"), key=str.casefold) if raw_tags else []
    data["status"] = data.get("status") or "want"
    return data


def facets(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts for the sidebar, over the whole library."""
    statuses = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT COALESCE(status,'want') AS status, COUNT(*) AS n "
            "FROM books b LEFT JOIN user_books u ON u.isbn13=b.isbn13 GROUP BY 1"
        )
    }
    tags = [
        {"name": r["name"], "count": r["n"]}
        for r in conn.execute(
            "SELECT t.name, COUNT(*) AS n FROM tags t JOIN book_tags bt ON bt.tag_id=t.id "
            "GROUP BY t.id ORDER BY n DESC, t.name COLLATE NOCASE"
        )
    ]
    authors = [
        {"name": r["name"], "count": r["n"]}
        for r in conn.execute(
            "SELECT json_each.value AS name, COUNT(*) AS n FROM books b, json_each(b.authors) "
            "GROUP BY 1 ORDER BY n DESC, name COLLATE NOCASE LIMIT 50"
        )
    ]
    languages = [
        r["language"]
        for r in conn.execute(
            "SELECT DISTINCT language FROM books WHERE language IS NOT NULL ORDER BY 1"
        )
    ]
    return {"statuses": statuses, "tags": tags, "authors": authors, "languages": languages}


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    pages = conn.execute(
        "SELECT COALESCE(SUM(b.page_count),0) FROM books b JOIN user_books u ON u.isbn13=b.isbn13 "
        "WHERE u.status='read'"
    ).fetchone()[0]
    avg = conn.execute("SELECT ROUND(AVG(rating),2) FROM user_books WHERE rating IS NOT NULL").fetchone()[0]
    by_source = {
        r["source"]: r["n"]
        for r in conn.execute("SELECT source, COUNT(*) AS n FROM books GROUP BY 1 ORDER BY n DESC")
    }
    return {
        "total_books": total,
        "pages_read": pages,
        "average_rating": avg,
        "by_status": facets(conn)["statuses"],
        "by_source": by_source,
        "tag_count": conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0],
    }
