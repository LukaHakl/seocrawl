"""Tests for image role classification and the heading-furniture check."""

from __future__ import annotations

import pytest

from seocrawl.checks import (
    LARGE_IMAGE_PX,
    THUMBNAIL_MAX_PX,
    check_heading_furniture,
    check_image_loading,
    is_furniture_heading,
    parse_html,
    parse_sizes,
    srcset_max_width,
)

from .conftest import load

URL = "https://example-shop.com/products/traditional-bifold"


# --- sizes parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    "value, px, relative",
    [
        ("100vw", 1280.0, True),
        ("50vw", 640.0, True),
        ("64px", 64.0, False),
        ("(max-width: 768px) 100vw, 50vw", 1280.0, True),   # largest clause wins
        ("(max-width: 600px) 120px, 400px", 400.0, False),
        ("4rem", 64.0, False),
        ("", None, False),
        (None, None, False),
        ("auto", None, False),
    ],
)
def test_parse_sizes(value, px, relative):
    assert parse_sizes(value) == (px, relative)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("/a-800.jpg 800w, /a-1600.jpg 1600w", 1600),
        ("/a.jpg 2x", None),
        (None, None),
    ],
)
def test_srcset_max_width(value, expected):
    assert srcset_max_width(value) == expected


# --- role classification ----------------------------------------------------


def test_hero_is_the_first_large_image():
    result = check_image_loading(load("hero_lazy.html"), URL)
    roles = [image.role for image in result.images]
    assert roles == ["hero", "gallery", "thumbnail", "thumbnail"]


def test_thumbnails_are_classified_by_rendered_size():
    result = check_image_loading(load("hero_lazy.html"), URL)
    thumbs = [i for i in result.images if i.role == "thumbnail"]
    assert thumbs and all(i.rendered_px <= THUMBNAIL_MAX_PX for i in thumbs)


def test_logo_outside_main_is_not_the_hero():
    """The header logo comes first in the document but is not content."""
    result = check_image_loading(load("hero_lazy.html"), URL)
    assert result.hero is not None
    assert "logo" not in result.hero.src


def test_viewport_relative_image_counts_as_large():
    soup = parse_html('<main><img src="/a.jpg" sizes="50vw"><p>%s</p></main>' % ("x " * 60))
    hero = check_image_loading(soup, URL).hero
    assert hero is not None and hero.viewport_relative


def test_large_fixed_size_counts_as_large():
    soup = parse_html('<main><img src="/a.jpg" sizes="%dpx"><p>%s</p></main>'
                      % (LARGE_IMAGE_PX + 50, "x " * 60))
    assert check_image_loading(soup, URL).hero is not None


def test_wide_srcset_promotes_an_image_without_sizes():
    soup = parse_html('<main><img src="/a.jpg" srcset="/a-1600.jpg 1600w">'
                      '<p>%s</p></main>' % ("x " * 60))
    assert check_image_loading(soup, URL).hero is not None


def test_small_image_without_sizes_is_not_a_hero():
    soup = parse_html('<main><img src="/icon.png" width="32"><p>%s</p></main>' % ("x " * 60))
    result = check_image_loading(soup, URL)
    assert result.hero is None and result.images[0].role == "thumbnail"


def test_page_with_no_images_is_handled():
    result = check_image_loading(parse_html("<main><p>Text only.</p></main>"), URL)
    assert result.hero is None and result.images == ()


# --- the signature check: lazy hero ----------------------------------------


def test_lazy_hero_is_detected():
    """Same attribute, different verdict: lazy on a hero is the defect."""
    result = check_image_loading(load("hero_lazy.html"), URL)
    assert result.hero_lazy
    assert not result.fetchpriority_present


def test_lazy_thumbnail_is_not_a_problem():
    """The 64px thumbnails in the same fixture are lazy and correctly so."""
    result = check_image_loading(load("hero_lazy.html"), URL)
    thumbs = [i for i in result.images if i.role == "thumbnail"]
    assert all(i.lazy for i in thumbs)
    assert result.eager_belowfold_count == 0


def test_eager_hero_with_fetchpriority_is_clean():
    result = check_image_loading(load("hero_ok.html"), URL)
    assert not result.hero_lazy
    assert result.fetchpriority_present
    assert result.eager_belowfold_count == 0


def test_eager_belowfold_images_are_counted():
    result = check_image_loading(load("hero_eager_belowfold.html"), URL)
    assert not result.hero_lazy
    assert result.eager_belowfold_count == 3      # gallery shot + two thumbnails


def test_large_image_without_srcset_is_flagged():
    result = check_image_loading(load("hero_no_srcset.html"), URL)
    assert len(result.large_without_srcset) == 1
    assert "bifold-1600" in result.large_without_srcset[0]


def test_thumbnail_without_srcset_is_not_flagged():
    """Only hero and gallery images are worth a responsive set."""
    result = check_image_loading(load("hero_ok.html"), URL)
    assert result.large_without_srcset == ()


# --- heading furniture ------------------------------------------------------


@pytest.mark.parametrize(
    "text, furniture",
    [
        ("Featured Collections", True),
        ("featured collections", True),
        ("You may also like", True),
        ("Customer Reviews", True),
        ("Related Products", True),
        ("You may also like these", True),      # prefix match
        ("Description", False),
        ("Warranty", False),
        ("Why full-grain leather", False),
    ],
)
def test_is_furniture_heading(text, furniture):
    assert is_furniture_heading(text) is furniture


def test_furniture_only_page_is_flagged():
    """H2s are 'Featured Collections' and 'You may also like'; nothing else."""
    result = check_heading_furniture(load("furniture_headings.html"))
    assert result.unmarked_content
    assert result.furniture == ("Featured Collections", "You may also like")
    assert result.content_headings == ()
    assert result.furniture_ratio == 1.0


def test_rich_heading_structure_is_never_flagged():
    """Five real H2/H3s plus one furniture heading is a well-structured page."""
    result = check_heading_furniture(load("rich_headings.html"))
    assert not result.unmarked_content
    assert len(result.content_headings) >= 5


def test_page_with_no_subheadings_is_not_flagged():
    """Nothing to judge: this is the missing-H1/thin-content check's business."""
    soup = parse_html("<main><h1>Title</h1><p>%s</p></main>" % ("word " * 80))
    assert not check_heading_furniture(soup).unmarked_content


def test_half_furniture_with_few_headings_is_flagged():
    soup = parse_html("<main><h1>P</h1><h2>Details</h2><h2>You may also like</h2>"
                      "<p>%s</p></main>" % ("word " * 80))
    result = check_heading_furniture(soup)
    assert result.unmarked_content and result.furniture_ratio == 0.5


def test_mostly_real_headings_are_not_flagged():
    soup = parse_html("<main><h1>P</h1><h2>Details</h2><h2>Warranty</h2>"
                      "<h2>You may also like</h2><p>%s</p></main>" % ("word " * 80))
    result = check_heading_furniture(soup)
    assert not result.unmarked_content and result.furniture_ratio < 0.5


def test_furniture_list_is_configurable():
    soup = parse_html("<main><h1>P</h1><h2>Nasze polecane</h2><p>%s</p></main>"
                      % ("word " * 80))
    assert not check_heading_furniture(soup).unmarked_content
    assert check_heading_furniture(soup, ["nasze polecane"]).unmarked_content
