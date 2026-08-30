import pytest

from bookshelf.db import json_list
from bookshelf.enrich import cache_get, import_isbns, lookup, upsert_book
from bookshelf.providers import GoogleBooksProvider, OpenLibraryProvider
from bookshelf.providers.base import NotFound, ProviderError, RateLimited

ISBN = "9780140328721"


@pytest.fixture
def google_chain(fake_provider, fixture):
    def build(*responses):
        return [fake_provider("google", responses, real=GoogleBooksProvider(api_key="k"))]
    return build


@pytest.fixture
def full_chain(fake_provider, fixture):
    def build(google_responses, ol_responses):
        return [
            fake_provider("google", google_responses, real=GoogleBooksProvider(api_key="k")),
            fake_provider("openlibrary", ol_responses, real=OpenLibraryProvider()),
        ]
    return build


class TestLookupFallback:
    def test_google_rate_limited_falls_through_to_openlibrary(self, conn, full_chain, fixture):
        """The finding that shaped this design: a 429 must not stop the import."""
        chain = full_chain(
            [RateLimited("429"), RateLimited("429"), RateLimited("429")],
            [fixture("openlibrary_9780140328721.json")],
        )
        record = lookup(conn, ISBN, chain, delay=0, sleep=lambda _: None)
        assert record.source == "openlibrary"
        assert record.title == "Fantastic Mr. Fox"

    def test_retries_before_falling_through(self, conn, full_chain, fixture):
        chain = full_chain(
            [RateLimited("429"), RateLimited("429"), fixture("google_9780140328721.json")],
            [],
        )
        record = lookup(conn, ISBN, chain, sleep=lambda _: None)
        # Third attempt succeeded, so Google stays the source and OL is never called.
        assert record.source == "google"
        assert chain[1].calls == []

    def test_notfound_in_google_falls_through(self, conn, full_chain, fixture):
        chain = full_chain([NotFound("nope")], [fixture("openlibrary_9780140328721.json")])
        assert lookup(conn, ISBN, chain, sleep=lambda _: None).source == "openlibrary"

    def test_all_providers_failing_raises_with_reasons(self, conn, full_chain):
        chain = full_chain([NotFound("g-missing")], [NotFound("ol-missing")])
        with pytest.raises(ProviderError) as exc:
            lookup(conn, ISBN, chain, sleep=lambda _: None)
        assert "g-missing" in str(exc.value)
        assert "ol-missing" in str(exc.value)


class TestCache:
    def test_successful_lookup_is_cached(self, conn, google_chain, fixture):
        chain = google_chain(fixture("google_9780140328721.json"))
        lookup(conn, ISBN, chain, sleep=lambda _: None)
        assert cache_get(conn, ISBN, "google") is not None

    def test_second_lookup_does_not_refetch(self, conn, google_chain, fixture):
        chain = google_chain(fixture("google_9780140328721.json"))
        lookup(conn, ISBN, chain, sleep=lambda _: None)
        # Only one scripted response exists; a second network call would raise.
        record = lookup(conn, ISBN, chain, sleep=lambda _: None)
        assert record.title == "Fantastic Mr. Fox"
        assert chain[0].calls == [ISBN]

    def test_use_cache_false_forces_refetch(self, conn, google_chain, fixture):
        chain = google_chain(
            fixture("google_9780140328721.json"), fixture("google_9780140328721.json")
        )
        lookup(conn, ISBN, chain, sleep=lambda _: None)
        lookup(conn, ISBN, chain, use_cache=False, sleep=lambda _: None)
        assert chain[0].calls == [ISBN, ISBN]


class TestUpsertPreservesUserData:
    def test_refetch_does_not_clobber_rating_or_notes(self, conn, fixture):
        google = GoogleBooksProvider(api_key="k")
        record = google.parse(fixture("google_9780140328721.json"), ISBN)
        assert upsert_book(conn, record) == "added"

        conn.execute(
            "UPDATE user_books SET status='read', rating=5, notes='loved it' WHERE isbn13=?",
            (ISBN,),
        )
        conn.commit()

        # Simulate a refetch that returns different metadata.
        record.title = "Fantastic Mr Fox (Revised)"
        assert upsert_book(conn, record) == "updated"

        row = conn.execute("SELECT * FROM user_books WHERE isbn13=?", (ISBN,)).fetchone()
        assert (row["status"], row["rating"], row["notes"]) == ("read", 5, "loved it")
        assert conn.execute("SELECT title FROM books WHERE isbn13=?", (ISBN,)).fetchone()[0] \
            == "Fantastic Mr Fox (Revised)"

    def test_creates_empty_user_row_for_new_book(self, conn, fixture):
        record = GoogleBooksProvider().parse(fixture("google_9780140328721.json"), ISBN)
        upsert_book(conn, record)
        row = conn.execute("SELECT * FROM user_books WHERE isbn13=?", (ISBN,)).fetchone()
        assert row["status"] == "want"
        assert row["rating"] is None

    def test_authors_round_trip_through_json_column(self, conn, fixture):
        record = GoogleBooksProvider().parse(fixture("google_with_subtitle.json"), "9780261102217")
        upsert_book(conn, record)
        stored = conn.execute(
            "SELECT authors FROM books WHERE isbn13=?", ("9780261102217",)
        ).fetchone()["authors"]
        assert json_list(stored) == ["J. R. R. Tolkien", "Christopher Tolkien"]


class TestImportIsbns:
    def test_records_bad_lines_as_failures(self, conn, full_chain, fixture):
        chain = full_chain([fixture("google_9780140328721.json")], [])
        result = import_isbns(conn, f"{ISBN}\nnot-an-isbn\n", chain=chain)
        assert result.added == [ISBN]
        assert result.failed_count == 1
        assert result.failures[0][0] == "not-an-isbn"

    def test_failures_are_persisted_against_the_run(self, conn, full_chain, fixture):
        chain = full_chain([fixture("google_9780140328721.json")], [])
        result = import_isbns(conn, f"{ISBN}\nbogus\n", chain=chain)
        rows = conn.execute(
            "SELECT raw_input, reason FROM import_failures WHERE run_id=?", (result.run_id,)
        ).fetchall()
        assert [r["raw_input"] for r in rows] == ["bogus"]

    def test_existing_books_are_skipped_by_default(self, conn, full_chain, fixture):
        chain = full_chain([fixture("google_9780140328721.json")], [])
        import_isbns(conn, ISBN, chain=chain)
        again = import_isbns(conn, ISBN, chain=chain)
        assert again.skipped == [ISBN]
        assert again.added == []

    def test_dry_run_writes_nothing(self, conn, full_chain, fixture):
        chain = full_chain([], [])
        result = import_isbns(conn, ISBN, chain=chain, dry_run=True)
        assert result.added == [ISBN]
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0] == 0

    def test_accepts_a_list_of_isbns(self, conn, full_chain, fixture):
        chain = full_chain(
            [fixture("google_9780140328721.json"), fixture("google_with_subtitle.json")], []
        )
        result = import_isbns(conn, [ISBN, "9780261102217"], chain=chain)
        assert sorted(result.added) == ["9780140328721", "9780261102217"]

    def test_duplicate_input_collapses(self, conn, full_chain, fixture):
        chain = full_chain([fixture("google_9780140328721.json")], [])
        # ISBN-10 and ISBN-13 for the same book, plus a repeat.
        result = import_isbns(conn, f"{ISBN}\n0140328726\n{ISBN}\n", chain=chain)
        assert result.added == [ISBN]
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1

    def test_provider_failure_is_reported_not_raised(self, conn, full_chain):
        chain = full_chain([NotFound("g")], [NotFound("ol")])
        result = import_isbns(conn, ISBN, chain=chain)
        assert result.added == []
        assert result.failed_count == 1
