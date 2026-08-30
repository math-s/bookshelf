import json
from pathlib import Path

import pytest

from bookshelf.db import connect

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def fixture():
    return load_fixture


@pytest.fixture
def conn(tmp_path):
    """A migrated, isolated database per test."""
    connection = connect(tmp_path / "test.db")
    yield connection
    connection.close()


class FakeProvider:
    """A provider that replays scripted responses instead of hitting the network."""

    def __init__(self, name, responses, real=None):
        self.name = name
        self._responses = list(responses)
        self.calls = []
        # Reuse the real parser so tests exercise production parsing code.
        self._real = real

    def fetch(self, isbn13):
        self.calls.append(isbn13)
        if not self._responses:
            raise AssertionError(f"{self.name}: no scripted response left for {isbn13}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def parse(self, payload, isbn13):
        return self._real.parse(payload, isbn13)


@pytest.fixture
def fake_provider():
    return FakeProvider
