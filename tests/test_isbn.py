import pytest

from bookshelf.isbn import InvalidISBN, is_valid, parse, parse_many, to_isbn10, to_isbn13


class TestParseValid:
    def test_isbn13_plain(self):
        r = parse("9780140328721")
        assert r.isbn13 == "9780140328721"
        assert r.isbn10 == "0140328726"

    def test_isbn13_hyphenated(self):
        assert parse("978-0-14-032872-1").isbn13 == "9780140328721"

    def test_isbn10_converts_up(self):
        r = parse("0140328726")
        assert r.isbn13 == "9780140328721"
        assert r.isbn10 == "0140328726"

    def test_isbn10_with_x_check_digit(self):
        # "The Hobbit", 0-261-10221-4 is mod-11; use a real X-terminated ISBN-10.
        r = parse("080442957X")
        assert r.isbn13 == "9780804429573"

    def test_lowercase_x_accepted(self):
        assert parse("080442957x").isbn13 == parse("080442957X").isbn13

    def test_isbn_prefix_stripped(self):
        assert parse("ISBN: 978-0-14-032872-1").isbn13 == "9780140328721"
        assert parse("isbn13 9780140328721").isbn13 == "9780140328721"

    def test_979_prefix_has_no_isbn10(self):
        r = parse("9791234567896")
        assert r.isbn13 == "9791234567896"
        assert r.isbn10 is None

    def test_surrounding_whitespace(self):
        assert parse("  9780140328721\t").isbn13 == "9780140328721"


class TestParseInvalid:
    @pytest.mark.parametrize(
        "value,fragment",
        [
            ("", "no digits"),
            ("   ", "no digits"),
            ("not a book", "no digits"),
            ("9780140328722", "check digit"),   # last digit wrong
            ("0140328727", "check digit"),      # ISBN-10 check wrong
            ("12345", "expected 10 or 13"),
            ("97801403287211", "expected 10 or 13"),
            ("1234567890123", "978 or 979"),
            ("97801403X8721", "cannot contain 'X'"),
            ("01X0328726", "final check digit"),
        ],
    )
    def test_rejects(self, value, fragment):
        with pytest.raises(InvalidISBN) as exc:
            parse(value)
        assert fragment in str(exc.value)

    def test_is_valid_never_raises(self):
        assert is_valid("9780140328721") is True
        assert is_valid("garbage") is False


class TestRoundTrip:
    @pytest.mark.parametrize(
        "isbn10", ["0140328726", "080442957X", "0261102214", "0439023483"]
    )
    def test_10_to_13_and_back(self, isbn10):
        assert to_isbn10(to_isbn13(isbn10)) == isbn10

    def test_to_isbn10_returns_none_for_979(self):
        assert to_isbn10("9791234567896") is None


class TestParseMany:
    def test_splits_good_from_bad(self):
        parsed, failures = parse_many(
            "9780140328721\nnonsense\n0140328726\n\n# a comment\n9780804429573\n"
        )
        # 0140328726 is the same book as the first line, so it collapses.
        assert [p.isbn13 for p in parsed] == ["9780140328721", "9780804429573"]
        assert len(failures) == 1
        assert failures[0][0] == "nonsense"

    def test_blank_and_comment_lines_are_not_failures(self):
        parsed, failures = parse_many("\n\n# header\n   \n")
        assert parsed == []
        assert failures == []

    def test_handles_empty_input(self):
        assert parse_many("") == ([], [])
