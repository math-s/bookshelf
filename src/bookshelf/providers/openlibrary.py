"""OpenLibrary provider — the keyless fallback.

Used when Google Books has no key configured, is rate limited, or has no record.
Its coverage is good for ISBNs but its shape is quite different: authors and
publishers are objects, subjects run to dozens of entries, and there is no real
description field (the first excerpt is the closest equivalent).
"""

from __future__ import annotations

from typing import Any

import httpx

from .base import BookRecord, NotFound, ProviderError, RateLimited

ENDPOINT = "https://openlibrary.org/api/books"

# A popular title can carry 90+ subjects; keeping them all would drown the UI.
MAX_CATEGORIES = 8


class OpenLibraryProvider:
    name = "openlibrary"

    def __init__(self, client: httpx.Client | None = None, timeout: float = 15.0):
        self._client = client
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return True  # no credentials needed

    def fetch(self, isbn13: str) -> dict[str, Any]:
        params = {"bibkeys": f"ISBN:{isbn13}", "format": "json", "jscmd": "data"}
        client = self._client or httpx.Client(timeout=self.timeout)
        try:
            response = client.get(ENDPOINT, params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(f"openlibrary: request failed ({exc.__class__.__name__})") from exc
        finally:
            if self._client is None:
                client.close()

        if response.status_code == 429:
            raise RateLimited("openlibrary: rate limited")
        if response.status_code >= 400:
            raise ProviderError(f"openlibrary: HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("openlibrary: response was not JSON") from exc

        if not payload or f"ISBN:{isbn13}" not in payload:
            raise NotFound(f"openlibrary: no record for {isbn13}")
        return payload

    def parse(self, payload: dict[str, Any], isbn13: str) -> BookRecord:
        entry = payload.get(f"ISBN:{isbn13}")
        if not entry:
            raise NotFound(f"openlibrary: no record for {isbn13}")

        identifiers = entry.get("identifiers") or {}
        cover = entry.get("cover") or {}
        excerpts = entry.get("excerpts") or []
        description = None
        for excerpt in excerpts:
            if isinstance(excerpt, dict) and excerpt.get("text"):
                description = excerpt["text"]
                break

        page_count = entry.get("number_of_pages")

        return BookRecord(
            isbn13=isbn13,
            isbn10=_first(identifiers.get("isbn_10")),
            title=entry.get("title") or "",
            subtitle=entry.get("subtitle"),
            authors=_names(entry.get("authors")),
            categories=_names(entry.get("subjects"))[:MAX_CATEGORIES],
            publisher=_first(_names(entry.get("publishers"))),
            published_date=entry.get("publish_date"),
            description=description,
            page_count=page_count if isinstance(page_count, int) else None,
            language=None,  # jscmd=data does not report language
            thumbnail_url=cover.get("medium") or cover.get("large") or cover.get("small"),
            preview_link=entry.get("url"),
            source=self.name,
            raw=payload,
        )


def _names(items: Any) -> list[str]:
    """OpenLibrary returns [{'name': ...}] for authors/publishers/subjects."""
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for item in items:
        if isinstance(item, dict) and item.get("name"):
            out.append(str(item["name"]))
        elif isinstance(item, str):
            out.append(item)
    return out


def _first(value: Any) -> str | None:
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value) if value else None
