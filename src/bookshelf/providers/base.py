"""Provider protocol and the normalised record every provider must produce."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = ["BookRecord", "Provider", "ProviderError", "RateLimited", "NotFound"]


class ProviderError(Exception):
    """A provider failed in a way worth reporting against the ISBN."""


class RateLimited(ProviderError):
    """Provider refused the request due to quota. Callers should back off or fall through."""


class NotFound(ProviderError):
    """Provider responded fine but has no record for this ISBN."""


@dataclass
class BookRecord:
    """Provider metadata, normalised. One shape regardless of where it came from."""

    isbn13: str
    isbn10: str | None = None
    title: str = ""
    subtitle: str | None = None
    authors: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    publisher: str | None = None
    published_date: str | None = None
    description: str | None = None
    page_count: int | None = None
    language: str | None = None
    thumbnail_url: str | None = None
    preview_link: str | None = None
    source: str = "manual"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        """Flatten to the column shape of the `books` table."""
        return {
            "isbn13": self.isbn13,
            "isbn10": self.isbn10,
            "title": self.title or "",
            "subtitle": self.subtitle,
            "authors": json.dumps(self.authors, ensure_ascii=False),
            "categories": json.dumps(self.categories, ensure_ascii=False),
            "publisher": self.publisher,
            "published_date": self.published_date,
            "description": self.description,
            "page_count": self.page_count,
            "language": self.language,
            "thumbnail_url": self.thumbnail_url,
            "preview_link": self.preview_link,
            "source": self.source,
            "raw_json": json.dumps(self.raw, ensure_ascii=False),
        }


class Provider(Protocol):
    name: str

    def fetch(self, isbn13: str) -> dict[str, Any]:
        """Return the raw provider payload, or raise NotFound / RateLimited / ProviderError."""

    def parse(self, payload: dict[str, Any], isbn13: str) -> BookRecord:
        """Normalise a raw payload into a BookRecord."""
