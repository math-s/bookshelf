import pytest

from bookshelf.providers import GoogleBooksProvider, OpenLibraryProvider
from bookshelf.providers.base import NotFound


class TestGoogleParse:
    def test_parses_core_fields(self, fixture):
        record = GoogleBooksProvider(api_key="x").parse(
            fixture("google_9780140328721.json"), "9780140328721"
        )
        assert record.title == "Fantastic Mr. Fox"
        assert record.authors == ["Roald Dahl"]
        assert record.isbn10 == "0140328726"
        assert record.publisher == "Puffin"
        assert record.published_date == "1988-10-01"
        assert record.page_count == 96
        assert record.language == "en"
        assert record.categories == ["Juvenile Fiction"]
        assert record.source == "google"

    def test_upgrades_http_thumbnails_to_https(self, fixture):
        record = GoogleBooksProvider(api_key="x").parse(
            fixture("google_9780140328721.json"), "9780140328721"
        )
        # Mixed content would be blocked when the UI is served over HTTPS.
        assert record.thumbnail_url.startswith("https://")
        assert record.preview_link.startswith("https://")

    def test_prefers_large_thumbnail(self, fixture):
        record = GoogleBooksProvider(api_key="x").parse(
            fixture("google_9780140328721.json"), "9780140328721"
        )
        assert "zoom=1" in record.thumbnail_url

    def test_subtitle_and_multiple_authors(self, fixture):
        record = GoogleBooksProvider().parse(fixture("google_with_subtitle.json"), "9780261102217")
        assert record.subtitle == "or There and Back Again"
        assert record.authors == ["J. R. R. Tolkien", "Christopher Tolkien"]

    def test_empty_result_raises_notfound(self, fixture):
        with pytest.raises(NotFound):
            GoogleBooksProvider().parse(fixture("google_notfound.json"), "9780000000002")

    def test_configured_reflects_key_presence(self):
        assert GoogleBooksProvider(api_key="abc").configured is True
        assert GoogleBooksProvider(api_key="").configured is False


class TestOpenLibraryParse:
    def test_parses_core_fields(self, fixture):
        record = OpenLibraryProvider().parse(
            fixture("openlibrary_9780140328721.json"), "9780140328721"
        )
        assert record.title == "Fantastic Mr. Fox"
        assert "Roald Dahl" in record.authors
        assert record.publisher == "Puffin"
        assert record.page_count == 96
        assert record.source == "openlibrary"
        assert record.thumbnail_url.startswith("https://covers.openlibrary.org/")

    def test_unwraps_name_objects(self, fixture):
        record = OpenLibraryProvider().parse(
            fixture("openlibrary_9780261102217.json"), "9780261102217"
        )
        assert record.authors == ["J.R.R. Tolkien"]
        assert record.publisher == "HarperCollins"
        assert record.subtitle == "or There and Back Again"

    def test_caps_runaway_subject_lists(self, fixture):
        # This record carries 90+ subjects; unbounded they would swamp the UI.
        record = OpenLibraryProvider().parse(
            fixture("openlibrary_9780261102217.json"), "9780261102217"
        )
        assert 0 < len(record.categories) <= 8
        assert all(isinstance(c, str) for c in record.categories)

    def test_missing_key_raises_notfound(self, fixture):
        with pytest.raises(NotFound):
            OpenLibraryProvider().parse(fixture("openlibrary_notfound.json"), "9780140328721")


class TestToRow:
    def test_serialises_lists_as_json(self, fixture):
        row = GoogleBooksProvider().parse(
            fixture("google_9780140328721.json"), "9780140328721"
        ).to_row()
        assert row["authors"] == '["Roald Dahl"]'
        assert row["categories"] == '["Juvenile Fiction"]'
        assert row["isbn13"] == "9780140328721"
