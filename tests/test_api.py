import pytest
from fastapi.testclient import TestClient

from bookshelf.db import connect
from bookshelf.enrich import upsert_book
from bookshelf.providers import GoogleBooksProvider, OpenLibraryProvider
from bookshelf.server import create_app

FOX = "9780140328721"
HOBBIT = "9780261102217"


@pytest.fixture
def db_file(tmp_path, fixture):
    """A DB seeded with two books from committed fixtures."""
    path = tmp_path / "api.db"
    conn = connect(path)
    upsert_book(conn, GoogleBooksProvider().parse(fixture("google_9780140328721.json"), FOX))
    upsert_book(conn, OpenLibraryProvider().parse(fixture("openlibrary_9780261102217.json"), HOBBIT))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def client(db_file):
    return TestClient(create_app(db_file))


class TestListAndFilter:
    def test_lists_all_books(self, client):
        body = client.get("/api/books").json()
        assert body["total"] == 2
        assert {b["isbn13"] for b in body["items"]} == {FOX, HOBBIT}

    def test_returns_facets(self, client):
        facets = client.get("/api/books").json()["facets"]
        assert facets["statuses"] == {"want": 2}
        assert any(a["name"] == "Roald Dahl" for a in facets["authors"])

    def test_authors_and_tags_are_lists(self, client):
        item = client.get(f"/api/books/{FOX}").json()
        assert item["authors"] == ["Roald Dahl"]
        assert item["tags"] == []

    def test_full_text_search(self, client):
        body = client.get("/api/books", params={"q": "hobbit"}).json()
        assert [b["isbn13"] for b in body["items"]] == [HOBBIT]

    def test_search_is_prefix_matched(self, client):
        assert client.get("/api/books", params={"q": "fantas"}).json()["total"] == 1

    def test_search_with_punctuation_does_not_500(self, client):
        # Bare FTS5 would treat these as operators and raise a syntax error.
        for term in ['"', "-fox", "a*b", "fox:", "^", "AND", ")("]:
            assert client.get("/api/books", params={"q": term}).status_code == 200

    def test_filter_by_status(self, client):
        client.patch(f"/api/books/{FOX}", json={"status": "read"})
        body = client.get("/api/books", params={"status": "read"}).json()
        assert [b["isbn13"] for b in body["items"]] == [FOX]

    def test_filter_by_author_substring(self, client):
        body = client.get("/api/books", params={"author": "tolkien"}).json()
        assert [b["isbn13"] for b in body["items"]] == [HOBBIT]

    def test_filter_by_rating_min(self, client):
        client.patch(f"/api/books/{FOX}", json={"rating": 4})
        assert client.get("/api/books", params={"rating_min": 4}).json()["total"] == 1
        assert client.get("/api/books", params={"rating_min": 5}).json()["total"] == 0

    def test_tags_are_anded_not_ored(self, client):
        client.put(f"/api/books/{FOX}/tags", json={"tags": ["owned", "kids"]})
        client.put(f"/api/books/{HOBBIT}/tags", json={"tags": ["owned"]})
        both = client.get("/api/books", params=[("tag", "owned"), ("tag", "kids")]).json()
        assert [b["isbn13"] for b in both["items"]] == [FOX]
        one = client.get("/api/books", params={"tag": "owned"}).json()
        assert one["total"] == 2

    def test_untagged_filter(self, client):
        client.put(f"/api/books/{FOX}/tags", json={"tags": ["owned"]})
        body = client.get("/api/books", params={"untagged": "true"}).json()
        assert [b["isbn13"] for b in body["items"]] == [HOBBIT]

    def test_sorting_and_order(self, client):
        asc = client.get("/api/books", params={"sort": "title", "order": "asc"}).json()
        desc = client.get("/api/books", params={"sort": "title", "order": "desc"}).json()
        assert [b["title"] for b in asc["items"]] == list(reversed([b["title"] for b in desc["items"]]))

    def test_unrated_books_sort_last(self, client):
        client.patch(f"/api/books/{HOBBIT}", json={"rating": 3})
        items = client.get("/api/books", params={"sort": "rating", "order": "desc"}).json()["items"]
        assert items[0]["isbn13"] == HOBBIT
        assert items[-1]["rating"] is None

    def test_pagination(self, client):
        first = client.get("/api/books", params={"limit": 1, "offset": 0}).json()
        second = client.get("/api/books", params={"limit": 1, "offset": 1}).json()
        assert first["total"] == second["total"] == 2
        assert first["items"][0]["isbn13"] != second["items"][0]["isbn13"]

    def test_unknown_sort_falls_back_instead_of_erroring(self, client):
        assert client.get("/api/books", params={"sort": "'; DROP TABLE books--"}).status_code == 200


class TestWrites:
    def test_patch_updates_status_and_rating(self, client):
        body = client.patch(f"/api/books/{FOX}", json={"status": "read", "rating": 5}).json()
        assert body["status"] == "read"
        assert body["rating"] == 5

    def test_patch_is_partial(self, client):
        client.patch(f"/api/books/{FOX}", json={"rating": 4, "notes": "keep me"})
        body = client.patch(f"/api/books/{FOX}", json={"status": "reading"}).json()
        assert body["rating"] == 4
        assert body["notes"] == "keep me"

    def test_patch_can_clear_a_field(self, client):
        client.patch(f"/api/books/{FOX}", json={"rating": 4})
        assert client.patch(f"/api/books/{FOX}", json={"rating": None}).json()["rating"] is None

    def test_rejects_bad_status(self, client):
        assert client.patch(f"/api/books/{FOX}", json={"status": "sideways"}).status_code == 422

    def test_rejects_out_of_range_rating(self, client):
        assert client.patch(f"/api/books/{FOX}", json={"rating": 9}).status_code == 422
        assert client.patch(f"/api/books/{FOX}", json={"rating": 0}).status_code == 422

    def test_patch_unknown_book_404s(self, client):
        assert client.patch("/api/books/9999999999999", json={"rating": 1}).status_code == 404

    def test_put_tags_replaces_and_dedupes(self, client):
        body = client.put(f"/api/books/{FOX}/tags", json={"tags": ["Sci-Fi", " sci-fi ", "Owned"]}).json()
        # " sci-fi " collapses into "Sci-Fi"; the endpoint returns tags sorted for
        # stable display rather than in submission order.
        assert body["tags"] == ["Owned", "Sci-Fi"]
        replaced = client.put(f"/api/books/{FOX}/tags", json={"tags": ["Kids"]}).json()
        assert replaced["tags"] == ["Kids"]

    def test_orphaned_tags_are_cleaned_up(self, client):
        client.put(f"/api/books/{FOX}/tags", json={"tags": ["temporary"]})
        client.put(f"/api/books/{FOX}/tags", json={"tags": []})
        assert client.get("/api/tags").json()["tags"] == []

    def test_delete_removes_book_and_user_row(self, client):
        assert client.delete(f"/api/books/{FOX}").status_code == 200
        assert client.get(f"/api/books/{FOX}").status_code == 404
        assert client.get("/api/books").json()["total"] == 1

    def test_delete_unknown_404s(self, client):
        assert client.delete("/api/books/9999999999999").status_code == 404


class TestImportEndpoint:
    def test_rejects_empty_body(self, client):
        assert client.post("/api/import", json={}).status_code == 400

    def test_reports_invalid_isbns(self, client, monkeypatch):
        # Books already present are skipped without any network call.
        body = client.post("/api/import", json={"text": f"{FOX}\nrubbish"}).json()
        assert body["skipped"] == [FOX]
        assert body["failed_count"] == 1
        assert body["failures"][0]["input"] == "rubbish"


class TestStatsAndHealth:
    def test_healthz(self, client):
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_stats(self, client):
        client.patch(f"/api/books/{FOX}", json={"status": "read", "rating": 5})
        body = client.get("/api/stats").json()
        assert body["total_books"] == 2
        assert body["by_status"]["read"] == 1
        assert body["pages_read"] == 96
        assert body["average_rating"] == 5.0
        assert body["by_source"] == {"google": 1, "openlibrary": 1}


class TestTokenAuth:
    def test_no_token_configured_means_open(self, client):
        assert client.get("/api/books").status_code == 200

    def test_token_required_when_configured(self, db_file, monkeypatch):
        monkeypatch.setenv("BOOKSHELF_TOKEN", "s3cret")
        guarded = TestClient(create_app(db_file))
        assert guarded.get("/api/books").status_code == 401
        assert guarded.patch(f"/api/books/{FOX}", json={"rating": 1}).status_code == 401
        ok = guarded.get("/api/books", headers={"X-Bookshelf-Token": "s3cret"})
        assert ok.status_code == 200

    def test_wrong_token_rejected(self, db_file, monkeypatch):
        monkeypatch.setenv("BOOKSHELF_TOKEN", "s3cret")
        guarded = TestClient(create_app(db_file))
        assert guarded.get("/api/books", headers={"X-Bookshelf-Token": "nope"}).status_code == 401

    def test_healthz_stays_open_for_probes(self, db_file, monkeypatch):
        monkeypatch.setenv("BOOKSHELF_TOKEN", "s3cret")
        assert TestClient(create_app(db_file)).get("/healthz").status_code == 200


class TestCors:
    """The UI is hosted on GitHub Pages while the API runs on Fly, so browser
    requests are cross-origin and depend on these headers being right."""

    def test_no_cors_headers_when_unconfigured(self, db_file):
        client = TestClient(create_app(db_file))
        r = client.get("/api/books", headers={"Origin": "https://math-s.github.io"})
        assert "access-control-allow-origin" not in r.headers

    def test_configured_origin_is_allowed(self, db_file, monkeypatch):
        monkeypatch.setenv("BOOKSHELF_CORS_ORIGINS", "https://math-s.github.io")
        client = TestClient(create_app(db_file))
        r = client.get("/api/books", headers={"Origin": "https://math-s.github.io"})
        assert r.headers["access-control-allow-origin"] == "https://math-s.github.io"

    def test_other_origins_are_refused(self, db_file, monkeypatch):
        monkeypatch.setenv("BOOKSHELF_CORS_ORIGINS", "https://math-s.github.io")
        client = TestClient(create_app(db_file))
        r = client.get("/api/books", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in r.headers

    def test_preflight_allows_the_token_header(self, db_file, monkeypatch):
        # Without this the browser blocks every authenticated cross-origin call.
        monkeypatch.setenv("BOOKSHELF_CORS_ORIGINS", "https://math-s.github.io")
        client = TestClient(create_app(db_file))
        r = client.options(
            "/api/books",
            headers={
                "Origin": "https://math-s.github.io",
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "x-bookshelf-token",
            },
        )
        assert r.status_code == 200
        assert "X-Bookshelf-Token" in r.headers["access-control-allow-headers"]

    def test_trailing_slashes_are_tolerated(self, db_file, monkeypatch):
        monkeypatch.setenv("BOOKSHELF_CORS_ORIGINS", "https://math-s.github.io/, http://localhost:8000")
        client = TestClient(create_app(db_file))
        r = client.get("/api/books", headers={"Origin": "https://math-s.github.io"})
        assert r.headers["access-control-allow-origin"] == "https://math-s.github.io"
