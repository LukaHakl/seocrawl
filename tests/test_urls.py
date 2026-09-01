"""Unit tests for URL normalization and hygiene."""

from __future__ import annotations

import pytest

from seocrawl.urls import (
    check_url_hygiene,
    dedupe_key,
    is_crawlable,
    normalize_url,
    path_segments,
    registrable_host,
    same_site,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://Example.com/Path#frag", "https://example.com/Path"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com//a//b", "https://example.com/a/b"),
        ("https://example.com/a?utm_source=x&gclid=y", "https://example.com/a"),
        ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_normalize_resolves_relative_against_base():
    # base ends in a directory, so ".." climbs from /a/b/ to /a/.
    assert normalize_url("../c", base="https://example.com/a/b/") == "https://example.com/a/c"
    assert normalize_url("../c", base="https://example.com/a/b") == "https://example.com/c"


def test_normalize_keeps_meaningful_query():
    assert normalize_url("https://x.com/s?variant=42") == "https://x.com/s?variant=42"


def test_registrable_host_strips_www():
    assert registrable_host("https://www.example.com/a") == "example.com"


@pytest.mark.parametrize(
    "a, b",
    [
        ("https://example.com/a", "http://example.com/b"),
        ("https://www.example.com/a", "https://example.com/a"),
    ],
)
def test_same_site(a, b):
    assert same_site(a, b)


def test_same_site_rejects_other_domains():
    assert not same_site("https://example.com/a", "https://example.net/a")


def test_dedupe_key_folds_scheme_and_www():
    assert dedupe_key("http://www.example.com/a") == dedupe_key("https://example.com/a")


@pytest.mark.parametrize(
    "url, crawlable",
    [
        ("https://example.com/page", True),
        ("https://example.com/a.html", True),
        ("https://example.com/img.jpg", False),
        ("https://example.com/doc.pdf", False),
        ("https://example.com/app.js", False),
        ("mailto:hi@example.com", False),
        ("javascript:void(0)", False),
    ],
)
def test_is_crawlable(url, crawlable):
    assert is_crawlable(url) is crawlable


def test_path_segments():
    assert path_segments("https://x.com/a/b/c/") == ["a", "b", "c"]


def test_hygiene_clean_url():
    assert check_url_hygiene("https://x.com/products/bifold").ok


@pytest.mark.parametrize(
    "url, flag",
    [
        ("https://x.com/products/bifold-copy", "copy-suffix"),
        ("https://x.com/Products/Bifold", "uppercase"),
        ("https://x.com/a/b/c/d/e", "deep-path"),
        ("https://x.com/my_page", "underscores"),
    ],
)
def test_hygiene_flags(url, flag):
    assert flag in check_url_hygiene(url).flags


def test_hygiene_query_params_only_flagged_without_canonical():
    assert "query-params-no-canonical" in check_url_hygiene(
        "https://x.com/s?variant=1", has_canonical=False
    ).flags
    assert "query-params-no-canonical" not in check_url_hygiene(
        "https://x.com/s?variant=1", has_canonical=True
    ).flags
