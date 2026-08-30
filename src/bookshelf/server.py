"""FastAPI app: serves the static UI and a small JSON API for reads and edits."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .db import STATUSES, connect, set_tags, tags_for
from .enrich import import_isbns
from .providers import build_chain
from .queries import BASE_SELECT, BookFilter, row_to_book, search, stats

def _find_web_dir() -> Path | None:
    """Locate the static UI.

    BOOKSHELF_WEB_DIR wins, then the repo layout (running from a checkout), then a
    copy sitting beside the package (how the Docker image installs it).
    """
    candidates = [
        Path(os.environ["BOOKSHELF_WEB_DIR"]) if os.environ.get("BOOKSHELF_WEB_DIR") else None,
        Path(__file__).resolve().parents[2] / "web",
        Path(__file__).resolve().parent / "web",
        Path.cwd() / "web",
    ]
    for candidate in candidates:
        if candidate and (candidate / "index.html").is_file():
            return candidate
    return None


WEB_DIR = _find_web_dir()


class BookPatch(BaseModel):
    """Partial update of the user-owned fields. Unset fields are left alone."""

    status: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    notes: str | None = None
    started_on: str | None = None
    finished_on: str | None = None

    @field_validator("status")
    @classmethod
    def _known_status(cls, v):
        if v is not None and v not in STATUSES:
            raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
        return v


class TagsBody(BaseModel):
    tags: list[str] = Field(default_factory=list)


class ImportBody(BaseModel):
    isbns: list[str] | None = None
    text: str | None = None
    refresh: bool = False


def _cors_origins() -> list[str]:
    """Origins allowed to call the API from a browser.

    Needed when the UI is hosted separately (GitHub Pages) from the API (Fly).
    Comma-separated in BOOKSHELF_CORS_ORIGINS, e.g.
        https://you.github.io,http://localhost:8000
    """
    raw = os.environ.get("BOOKSHELF_CORS_ORIGINS", "")
    return [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]


def create_app(db: str | os.PathLike[str] | None = None) -> FastAPI:
    app = FastAPI(title="Bookshelf", version="0.1.0", docs_url="/api/docs", redoc_url=None)
    token = os.environ.get("BOOKSHELF_TOKEN") or None

    origins = _cors_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            # The token header is custom, so it must be allowed explicitly or the
            # browser's preflight will reject every authenticated request.
            allow_headers=["Content-Type", "X-Bookshelf-Token"],
            max_age=3600,
        )

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = connect(db)
        try:
            yield conn
        finally:
            conn.close()

    def require_token(x_bookshelf_token: str | None = Header(default=None)) -> None:
        """No-op when BOOKSHELF_TOKEN is unset (local use); enforced when it is set."""
        if token and x_bookshelf_token != token:
            raise HTTPException(status_code=401, detail="invalid or missing X-Bookshelf-Token")

    guarded = [Depends(require_token)]

    # --- reads ---------------------------------------------------------------

    @app.get("/api/books", dependencies=guarded)
    def list_books(
        conn: sqlite3.Connection = Depends(get_conn),
        q: str | None = None,
        status: list[str] = Query(default=[]),
        tag: list[str] = Query(default=[]),
        author: str | None = None,
        language: str | None = None,
        rating_min: int | None = None,
        untagged: bool = False,
        sort: str = "title",
        order: str = "asc",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return search(
            conn,
            BookFilter(
                q=q, status=status, tags=tag, author=author, language=language,
                rating_min=rating_min, untagged=untagged, sort=sort, order=order,
                limit=limit, offset=offset,
            ),
        )

    @app.get("/api/books/{isbn13}", dependencies=guarded)
    def get_book(isbn13: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        return _fetch_book_or_404(conn, isbn13)

    @app.get("/api/tags", dependencies=guarded)
    def list_tags(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        rows = conn.execute(
            "SELECT t.name, COUNT(bt.isbn13) AS count FROM tags t "
            "LEFT JOIN book_tags bt ON bt.tag_id = t.id "
            "GROUP BY t.id ORDER BY count DESC, t.name COLLATE NOCASE"
        ).fetchall()
        return {"tags": [dict(r) for r in rows]}

    @app.get("/api/stats", dependencies=guarded)
    def get_stats(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        return stats(conn)

    # --- writes --------------------------------------------------------------

    @app.patch("/api/books/{isbn13}", dependencies=guarded)
    def patch_book(
        isbn13: str, patch: BookPatch, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict[str, Any]:
        _fetch_book_or_404(conn, isbn13)
        fields = patch.model_dump(exclude_unset=True)
        if fields:
            conn.execute("INSERT OR IGNORE INTO user_books (isbn13) VALUES (?)", (isbn13,))
            assignments = ", ".join(f"{k} = :{k}" for k in fields)
            conn.execute(
                f"UPDATE user_books SET {assignments}, updated_at = datetime('now') "
                f"WHERE isbn13 = :isbn13",
                {**fields, "isbn13": isbn13},
            )
            conn.commit()
        return _fetch_book_or_404(conn, isbn13)

    @app.put("/api/books/{isbn13}/tags", dependencies=guarded)
    def put_tags(
        isbn13: str, body: TagsBody, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict[str, Any]:
        _fetch_book_or_404(conn, isbn13)
        set_tags(conn, isbn13, body.tags)
        conn.commit()
        return {"isbn13": isbn13, "tags": tags_for(conn, isbn13)}

    @app.delete("/api/books/{isbn13}", dependencies=guarded)
    def delete_book(isbn13: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        _fetch_book_or_404(conn, isbn13)
        conn.execute("DELETE FROM books WHERE isbn13 = ?", (isbn13,))
        # Drop tags that were only attached to this book.
        conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM book_tags)")
        conn.commit()
        return {"deleted": isbn13}

    @app.post("/api/import", dependencies=guarded)
    def post_import(body: ImportBody, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        source = body.text if body.text is not None else (body.isbns or [])
        if not source:
            raise HTTPException(status_code=400, detail="provide 'text' or 'isbns'")
        result = import_isbns(conn, source, chain=build_chain(), refresh=body.refresh)
        return result.as_dict()

    # --- static UI -----------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if WEB_DIR is not None:
        # Mounted at the root, and last, so the API routes above still match first.
        # Serving the UI from / means its asset paths ("./styles.css") are identical
        # whether Fly serves it or GitHub Pages does.
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app


def _fetch_book_or_404(conn: sqlite3.Connection, isbn13: str) -> dict[str, Any]:
    row = conn.execute(f"{BASE_SELECT} WHERE b.isbn13 = ?", (isbn13,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"no book with ISBN {isbn13}")
    return row_to_book(row)


app = create_app()
