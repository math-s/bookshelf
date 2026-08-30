"""Metadata providers, tried in order until one yields a record."""

from __future__ import annotations

from .base import BookRecord, NotFound, Provider, ProviderError, RateLimited
from .google import GoogleBooksProvider
from .openlibrary import OpenLibraryProvider

__all__ = [
    "BookRecord",
    "Provider",
    "ProviderError",
    "RateLimited",
    "NotFound",
    "GoogleBooksProvider",
    "OpenLibraryProvider",
    "PROVIDERS",
    "build_chain",
]

PROVIDERS = {
    "google": GoogleBooksProvider,
    "openlibrary": OpenLibraryProvider,
}

DEFAULT_ORDER = ("google", "openlibrary")


def build_chain(names: list[str] | tuple[str, ...] | None = None, **kwargs) -> list[Provider]:
    """Instantiate providers in the requested order.

    Defaults to Google first (richest data) then OpenLibrary (keyless fallback).
    """
    selected = tuple(names) if names else DEFAULT_ORDER
    chain: list[Provider] = []
    for name in selected:
        key = name.strip().lower()
        if key not in PROVIDERS:
            raise ValueError(f"unknown provider {name!r}; choose from {', '.join(PROVIDERS)}")
        chain.append(PROVIDERS[key]())
    return chain
