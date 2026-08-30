"""ISBN parsing, validation and normalisation.

Everything in the system is keyed by ISBN-13. ISBN-10 inputs are converted up so
that the same physical book imported from two different sources lands on one row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["InvalidISBN", "ParsedISBN", "normalize", "parse", "parse_many", "is_valid"]

# Everything that is not a digit or a check-digit 'X' is noise: hyphens, spaces,
# non-breaking spaces, and the "ISBN:" prefixes that show up in pasted lists.
_STRIP = re.compile(r"[^0-9Xx]")
_PREFIX = re.compile(r"^\s*(isbn(?:-?1[03])?)\s*[:.]?\s*", re.IGNORECASE)


class InvalidISBN(ValueError):
    """Raised when a string cannot be read as a valid ISBN-10 or ISBN-13."""


@dataclass(frozen=True)
class ParsedISBN:
    """A validated ISBN, canonicalised to 13 digits."""

    isbn13: str
    isbn10: str | None
    raw: str


def normalize(value: str) -> str:
    """Strip formatting from an ISBN-ish string. Does not validate."""
    return _STRIP.sub("", _PREFIX.sub("", value or "")).upper()


def _check_digit_10(body: str) -> str:
    """Compute the ISBN-10 check digit (mod 11) for the first 9 digits."""
    total = sum((10 - i) * int(ch) for i, ch in enumerate(body[:9]))
    remainder = (11 - (total % 11)) % 11
    return "X" if remainder == 10 else str(remainder)


def _check_digit_13(body: str) -> str:
    """Compute the ISBN-13 check digit (mod 10) for the first 12 digits."""
    total = sum((1 if i % 2 == 0 else 3) * int(ch) for i, ch in enumerate(body[:12]))
    return str((10 - (total % 10)) % 10)


def to_isbn13(isbn10: str) -> str:
    """Convert a valid ISBN-10 to its ISBN-13 equivalent (978 prefix)."""
    body = "978" + isbn10[:9]
    return body + _check_digit_13(body)


def to_isbn10(isbn13: str) -> str | None:
    """Convert a 978-prefixed ISBN-13 back to ISBN-10. Returns None for 979 ranges,
    which have no ISBN-10 equivalent."""
    if not isbn13.startswith("978"):
        return None
    body = isbn13[3:12]
    return body + _check_digit_10(body)


def parse(value: str) -> ParsedISBN:
    """Parse and validate an ISBN string, canonicalising to ISBN-13.

    Raises InvalidISBN with a human-readable reason, which the importer records
    against the offending line rather than dropping it silently.
    """
    raw = (value or "").strip()
    digits = normalize(raw)

    if not digits:
        raise InvalidISBN("no digits found")

    if len(digits) == 10:
        # 'X' is only ever legal as the final check digit.
        if "X" in digits[:9]:
            raise InvalidISBN("'X' is only valid as the final check digit")
        if digits[9] != _check_digit_10(digits):
            raise InvalidISBN(f"ISBN-10 check digit failed (expected {_check_digit_10(digits)})")
        return ParsedISBN(isbn13=to_isbn13(digits), isbn10=digits, raw=raw)

    if len(digits) == 13:
        if "X" in digits:
            raise InvalidISBN("ISBN-13 cannot contain 'X'")
        if not digits.startswith(("978", "979")):
            raise InvalidISBN("ISBN-13 must start with 978 or 979")
        if digits[12] != _check_digit_13(digits):
            raise InvalidISBN(f"ISBN-13 check digit failed (expected {_check_digit_13(digits)})")
        return ParsedISBN(isbn13=digits, isbn10=to_isbn10(digits), raw=raw)

    raise InvalidISBN(f"expected 10 or 13 digits, got {len(digits)}")


def is_valid(value: str) -> bool:
    try:
        parse(value)
        return True
    except InvalidISBN:
        return False


def parse_many(text: str) -> tuple[list[ParsedISBN], list[tuple[str, str]]]:
    """Parse a blob of text (one ISBN per line) into (parsed, failures).

    Failures are (raw_line, reason) pairs. Blank lines and '#' comments are skipped
    entirely rather than reported as errors. Duplicates within the input collapse
    to the first occurrence, so pasting an overlapping list twice is harmless.
    """
    parsed: list[ParsedISBN] = []
    failures: list[tuple[str, str]] = []
    seen: set[str] = set()

    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            item = parse(stripped)
        except InvalidISBN as exc:
            failures.append((stripped, str(exc)))
            continue
        if item.isbn13 in seen:
            continue
        seen.add(item.isbn13)
        parsed.append(item)

    return parsed, failures
