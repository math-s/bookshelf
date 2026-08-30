"""Authors as a first-class relation: normalisation, grouping, and merging."""

import pytest

from bookshelf.db import (
    authors_for,
    connect,
    merge_authors,
    normalize_author_name,
    resolve_author,
    set_authors,
)
from bookshelf.enrich import upsert_book
from bookshelf.providers import GoogleBooksProvider, OpenLibraryProvider
from bookshelf.queries import BookFilter, books_grouped_by_author, list_authors, search

FOX = "9780140328721"
HOBBIT = "9780261102217"


class TestNormalisation:
    @pytest.mark.parametrize(
        "a,b",
        [
            ("J.R.R. Tolkien", "J. R. R. Tolkien"),   # initial spacing
            ("J. K. Rowling", "JK Rowling"),
            ("Gabriel García Márquez", "Gabriel Garcia Marquez"),  # accents
            ("Frank Herbert", "frank  herbert"),       # case and whitespace
            ("Le Guin, Ursula K.", "Le Guin  Ursula K"),
        ],
    )
    def test_variants_fold_together(self, a, b):
        assert normalize_author_name(a) == normalize_author_name(b)

    @pytest.mark.parametrize(
        "a,b",
        [
            ("Roald Dahl", "Roal'd Dal'"),   # transliteration: a merge, not a guess
            ("George Orwell", "George Eliot"),
            ("Kim Stanley Robinson", "Kim Robinson"),
        ],
    )
    def test_genuinely_different_names_stay_apart(self, a, b):
        assert normalize_author_name(a) != normalize_author_name(b)

    def test_empty_input_yields_no_key(self):
        assert normalize_author_name("") == ""
        assert normalize_author_name("   .,  ") == ""


class TestResolveAndLink:
    def test_same_person_spelled_two_ways_gets_one_row(self, conn):
        first = resolve_author(conn, "J.R.R. Tolkien")
        second = resolve_author(conn, "J. R. R. Tolkien")
        assert first == second
        assert conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0] == 1

    def test_display_name_keeps_the_first_spelling_seen(self, conn):
        resolve_author(conn, "J.R.R. Tolkien")
        resolve_author(conn, "J. R. R. Tolkien")
        assert conn.execute("SELECT name FROM authors").fetchone()[0] == "J.R.R. Tolkien"

    def test_blank_names_are_skipped(self, conn):
        assert resolve_author(conn, "  ") is None

    def test_set_authors_preserves_credited_order(self, conn, fixture):
        upsert_book(conn, GoogleBooksProvider().parse(fixture("google_with_subtitle.json"), HOBBIT))
        names = [a["name"] for a in authors_for(conn, HOBBIT)]
        assert names == ["J. R. R. Tolkien", "Christopher Tolkien"]

    def test_replacing_authors_drops_the_old_link(self, conn, fixture):
        upsert_book(conn, GoogleBooksProvider().parse(fixture("google_with_subtitle.json"), HOBBIT))
        set_authors(conn, HOBBIT, ["Someone Else"])
        assert [a["name"] for a in authors_for(conn, HOBBIT)] == ["Someone Else"]

    def test_orphaned_authors_are_pruned(self, conn, fixture):
        upsert_book(conn, GoogleBooksProvider().parse(fixture("google_with_subtitle.json"), HOBBIT))
        set_authors(conn, HOBBIT, ["Someone Else"])
        remaining = {r["name"] for r in conn.execute("SELECT name FROM authors")}
        assert remaining == {"Someone Else"}

    def test_duplicate_names_on_one_book_collapse(self, conn, fixture):
        upsert_book(conn, GoogleBooksProvider().parse(fixture("google_9780140328721.json"), FOX))
        set_authors(conn, FOX, ["Roald Dahl", "roald  dahl", "R. Dahl"])
        assert len(authors_for(conn, FOX)) == 2


class TestBackfill:
    def test_existing_books_are_linked_on_migration(self, tmp_path, fixture):
        """A database created before the author tables existed must come out linked."""
        path = tmp_path / "old.db"
        conn = connect(path)
        upsert_book(conn, GoogleBooksProvider().parse(fixture("google_9780140328721.json"), FOX))
        conn.commit()

        # Rewind to a genuine v1 database: the author tables did not exist at all.
        conn.execute("DROP TABLE book_authors")
        conn.execute("DROP TABLE author_aliases")
        conn.execute("DROP TABLE authors")
        conn.execute("DELETE FROM schema_version WHERE version >= 2")
        conn.commit()
        conn.close()

        migrated = connect(path)
        assert [a["name"] for a in authors_for(migrated, FOX)] == ["Roald Dahl"]
        migrated.close()


class TestMerge:
    @pytest.fixture
    def two_spellings(self, conn, fixture):
        upsert_book(conn, GoogleBooksProvider().parse(fixture("google_9780140328721.json"), FOX))
        set_authors(conn, FOX, ["Roald Dahl"])
        upsert_book(conn, OpenLibraryProvider().parse(fixture("openlibrary_9780261102217.json"), HOBBIT))
        set_authors(conn, HOBBIT, ["Roal'd Dal'"])
        conn.commit()
        ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM authors")}
        return ids

    def test_merge_moves_books_and_removes_the_source(self, conn, two_spellings):
        result = merge_authors(conn, two_spellings["Roal'd Dal'"], two_spellings["Roald Dahl"])
        assert result["books_moved"] == 1
        assert result["books_total"] == 2
        names = {r["name"] for r in conn.execute("SELECT name FROM authors")}
        assert names == {"Roald Dahl"}

    def test_merged_author_now_owns_both_books(self, conn, two_spellings):
        target = two_spellings["Roald Dahl"]
        merge_authors(conn, two_spellings["Roal'd Dal'"], target)
        found = search(conn, BookFilter(author_id=target))
        assert {b["isbn13"] for b in found["items"]} == {FOX, HOBBIT}

    def test_merge_survives_a_refetch_of_the_old_spelling(self, conn, two_spellings):
        """The alias is the point: a provider returning the old name must not
        resurrect the duplicate."""
        target = two_spellings["Roald Dahl"]
        merge_authors(conn, two_spellings["Roal'd Dal'"], target)
        set_authors(conn, HOBBIT, ["Roal'd Dal'"])       # as a refetch would
        assert [a["id"] for a in authors_for(conn, HOBBIT)] == [target]
        assert conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0] == 1

    def test_merging_into_self_is_rejected(self, conn, two_spellings):
        with pytest.raises(ValueError):
            merge_authors(conn, two_spellings["Roald Dahl"], two_spellings["Roald Dahl"])

    def test_unknown_author_is_rejected(self, conn, two_spellings):
        with pytest.raises(ValueError):
            merge_authors(conn, 9999, two_spellings["Roald Dahl"])

    def test_merging_authors_who_share_a_book_does_not_duplicate(self, conn, fixture):
        upsert_book(conn, GoogleBooksProvider().parse(fixture("google_9780140328721.json"), FOX))
        set_authors(conn, FOX, ["Roald Dahl", "Roal'd Dal'"])
        ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM authors")}
        result = merge_authors(conn, ids["Roal'd Dal'"], ids["Roald Dahl"])
        assert result["books_total"] == 1
        assert len(authors_for(conn, FOX)) == 1


class TestGrouping:
    @pytest.fixture
    def shelf(self, conn, fixture):
        upsert_book(conn, GoogleBooksProvider().parse(fixture("google_9780140328721.json"), FOX))
        upsert_book(conn, GoogleBooksProvider().parse(fixture("google_with_subtitle.json"), HOBBIT))
        conn.commit()
        return conn

    def test_groups_books_under_each_author(self, shelf):
        grouped = books_grouped_by_author(shelf, BookFilter())
        by_name = {g["name"]: g for g in grouped["groups"]}
        assert "Roald Dahl" in by_name
        assert [b["isbn13"] for b in by_name["Roald Dahl"]["books"]] == [FOX]

    def test_a_co_authored_book_appears_under_both_authors(self, shelf):
        grouped = books_grouped_by_author(shelf, BookFilter())
        by_name = {g["name"]: g for g in grouped["groups"]}
        assert [b["isbn13"] for b in by_name["J. R. R. Tolkien"]["books"]] == [HOBBIT]
        assert [b["isbn13"] for b in by_name["Christopher Tolkien"]["books"]] == [HOBBIT]

    def test_total_counts_distinct_books_not_credits(self, shelf):
        grouped = books_grouped_by_author(shelf, BookFilter())
        # Three author groups, but only two books.
        assert len(grouped["groups"]) == 3
        assert grouped["total"] == 2

    def test_grouping_respects_the_active_filter(self, shelf):
        shelf.execute("UPDATE user_books SET status='read' WHERE isbn13=?", (FOX,))
        shelf.commit()
        grouped = books_grouped_by_author(shelf, BookFilter(status=["read"]))
        assert [g["name"] for g in grouped["groups"]] == ["Roald Dahl"]

    def test_books_with_no_author_land_in_a_final_bucket(self, shelf):
        set_authors(shelf, FOX, [])
        shelf.commit()
        grouped = books_grouped_by_author(shelf, BookFilter())
        assert grouped["groups"][-1]["name"] == "Unknown author"
        assert grouped["groups"][-1]["id"] is None

    def test_list_authors_reports_counts_and_progress(self, shelf):
        shelf.execute("UPDATE user_books SET status='read', rating=5 WHERE isbn13=?", (FOX,))
        shelf.commit()
        dahl = next(a for a in list_authors(shelf) if a["name"] == "Roald Dahl")
        assert dahl["book_count"] == 1
        assert dahl["read_count"] == 1
        assert dahl["average_rating"] == 5.0

    def test_list_authors_can_be_filtered_by_name(self, shelf):
        assert [a["name"] for a in list_authors(shelf, "dahl")] == ["Roald Dahl"]


class TestMinBooks:
    @pytest.fixture
    def mixed_shelf(self, conn, fixture):
        upsert_book(conn, GoogleBooksProvider().parse(fixture("google_9780140328721.json"), FOX))
        upsert_book(conn, GoogleBooksProvider().parse(fixture("google_with_subtitle.json"), HOBBIT))
        # Give Dahl a second book so one author is prolific and the rest are not.
        conn.execute(
            "INSERT INTO books (isbn13, title, authors) VALUES ('9780140371550','The BFG','[\"Roald Dahl\"]')"
        )
        conn.execute("INSERT INTO user_books (isbn13) VALUES ('9780140371550')")
        set_authors(conn, "9780140371550", ["Roald Dahl"])
        conn.commit()
        return conn

    def test_hides_one_off_authors(self, mixed_shelf):
        grouped = books_grouped_by_author(mixed_shelf, BookFilter(), min_books=2)
        assert [g["name"] for g in grouped["groups"]] == ["Roald Dahl"]

    def test_total_reflects_only_the_books_shown(self, mixed_shelf):
        grouped = books_grouped_by_author(mixed_shelf, BookFilter(), min_books=2)
        assert grouped["total"] == 2               # the two Dahl books
        assert grouped["total_unfiltered"] == 3    # everything the filter matched

    def test_reports_how_many_authors_were_hidden(self, mixed_shelf):
        grouped = books_grouped_by_author(mixed_shelf, BookFilter(), min_books=2)
        assert grouped["hidden_authors"] == 2      # both Tolkiens

    def test_min_books_of_one_changes_nothing(self, mixed_shelf):
        assert len(books_grouped_by_author(mixed_shelf, BookFilter(), min_books=1)["groups"]) == 3

    def test_unknown_author_bucket_is_hidden_when_filtering(self, mixed_shelf):
        set_authors(mixed_shelf, FOX, [])
        mixed_shelf.commit()
        names = [g["name"] for g in books_grouped_by_author(mixed_shelf, BookFilter(), min_books=2)["groups"]]
        assert "Unknown author" not in names
