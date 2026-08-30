"""Google Books provider.

Note: the anonymous quota for this API is often zero, in which case every request
comes back 429 and the caller falls through to OpenLibrary. Set GOOGLE_BOOKS_API_KEY.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .base import BookRecord, NotFound, ProviderError, RateLimited

ENDPOINT = "https://www.googleapis.com/books/v1/volumes"


class GoogleBooksProvider:
    name = "google"

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None, timeout: float = 15.0):
        self.api_key = api_key if api_key is not None else os.environ.get("GOOGLE_BOOKS_API_KEY") or None
        self._client = client
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        """Whether a key is present. Without one the API usually 429s immediately."""
        return bool(self.api_key)

    def fetch(self, isbn13: str) -> dict[str, Any]:
        params: dict[str, str] = {"q": f"isbn:{isbn13}"}
        if self.api_key:
            params["key"] = self.api_key

        client = self._client or httpx.Client(timeout=self.timeout)
        try:
            response = client.get(ENDPOINT, params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(f"google: request failed ({exc.__class__.__name__})") from exc
        finally:
            if self._client is None:
                client.close()

        if response.status_code == 429:
            raise RateLimited("google: quota exceeded (set GOOGLE_BOOKS_API_KEY or wait)")
        if response.status_code in (401, 403):
            raise ProviderError(f"google: rejected the API key (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise ProviderError(f"google: HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("google: response was not JSON") from exc

        if not payload.get("items"):
            raise NotFound(f"google: no volume for {isbn13}")
        return payload

    def parse(self, payload: dict[str, Any], isbn13: str) -> BookRecord:
        items = payload.get("items") or []
        if not items:
            raise NotFound(f"google: no volume for {isbn13}")
        info = items[0].get("volumeInfo") or {}

        identifiers = {
            entry.get("type"): entry.get("identifier")
            for entry in info.get("industryIdentifiers") or []
            if isinstance(entry, dict)
        }
        images = info.get("imageLinks") or {}
        # Prefer the larger of the two thumbnails Google returns.
        thumbnail = images.get("thumbnail") or images.get("smallThumbnail")

        return BookRecord(
            isbn13=isbn13,
            isbn10=identifiers.get("ISBN_10"),
            title=info.get("title") or "",
            subtitle=info.get("subtitle"),
            authors=[str(a) for a in info.get("authors") or []],
            categories=[str(c) for c in info.get("categories") or []],
            publisher=info.get("publisher"),
            published_date=info.get("publishedDate"),
            description=info.get("description"),
            page_count=info.get("pageCount") if isinstance(info.get("pageCount"), int) else None,
            language=info.get("language"),
            # Google serves these over http; upgrade so they aren't blocked as mixed content.
            thumbnail_url=_https(thumbnail),
            preview_link=_https(info.get("previewLink") or info.get("infoLink")),
            source=self.name,
            raw=payload,
        )


def _https(url: str | None) -> str | None:
    if not url:
        return None
    return url.replace("http://", "https://", 1) if url.startswith("http://") else url
