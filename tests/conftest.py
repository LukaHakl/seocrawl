"""Shared fixtures: HTML documents loaded from ``tests/fixtures``."""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from seocrawl.checks import parse_html

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> BeautifulSoup:
    """Parse the named fixture file from ``tests/fixtures``."""
    return parse_html((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def good_page() -> BeautifulSoup:
    """A clean page: every head tag present and correctly sized."""
    return load("good_page.html")


@pytest.fixture
def bad_page() -> BeautifulSoup:
    """A page failing most head/heading/image/link checks at once."""
    return load("bad_page.html")


@pytest.fixture
def product_ok() -> BeautifulSoup:
    """A Shopify PDP where every price signal agrees."""
    return load("product_price_ok.html")


@pytest.fixture
def product_mismatch() -> BeautifulSoup:
    """A Shopify PDP where og:price says 179 and the real price is 169."""
    return load("product_price_mismatch.html")


@pytest.fixture
def product_merged_reviews() -> BeautifulSoup:
    """A PDP whose schema claims a catalogue-wide review count."""
    return load("product_merged_reviews.html")


@pytest.fixture
def hreflang_page() -> BeautifulSoup:
    """A page declaring a three-locale hreflang cluster with x-default."""
    return load("hreflang.html")


@pytest.fixture
def product_group() -> BeautifulSoup:
    """A Shopify ProductGroup PDP: variant offers with strikethrough prices."""
    return load("product_group_variants.html")
