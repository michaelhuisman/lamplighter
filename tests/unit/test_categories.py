"""The category filter value from a query parameter."""

import pytest

from app.services.categories import UNCATEGORIZED, parse


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("", None), ("12", 12), ("none", UNCATEGORIZED), ("abc", None), ("-3", None)],
)
def test_parse(raw: str | None, expected: object) -> None:
    assert parse(raw) == expected
