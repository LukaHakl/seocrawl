"""Per-page SEO checks.

Every check is a small pure function taking a parsed ``BeautifulSoup`` document
(plus, where needed, the page URL or response headers) and returning a frozen
dataclass. Nothing here does I/O, so each one is directly unit-testable against
an HTML fixture.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from bs4 import BeautifulSoup, Tag

from .urls import dedupe_key, is_crawlable, normalize_url, same_site

# --- thresholds -------------------------------------------------------------
# Pixel-width limits vary by font, so we use the character counts that map to
# Google's ~600px title / ~960px description SERP truncation in practice.
TITLE_MIN, TITLE_MAX = 30, 60
META_DESC_MIN, META_DESC_MAX = 70, 155
THIN_CONTENT_WORDS = 250
OVERSIZED_IMAGE_BYTES = 300 * 1024
#: Max SimHash bit distance still counted as a near-duplicate (~87% similar).
#: Calibrated on Shopify PDPs: identical-but-for-a-word pages score 0-7, while
#: genuinely different products sharing one theme template score 13+.
NEAR_DUPLICATE_DISTANCE = 8


def parse_html(html: str) -> BeautifulSoup:
    """Parse ``html`` with lxml, falling back to the stdlib parser."""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover - only when lxml is unavailable
        return BeautifulSoup(html, "html.parser")


def _meta(soup: BeautifulSoup, *, name: str | None = None, prop: str | None = None) -> str | None:
    """Return the ``content`` of the first matching ``<meta>`` tag."""
    attrs: dict[str, Any] = {}
    if name:
        attrs["name"] = re.compile(r"^" + re.escape(name) + r"$", re.I)
    if prop:
        attrs["property"] = re.compile(r"^" + re.escape(prop) + r"$", re.I)
    tag = soup.find("meta", attrs=attrs)
    if isinstance(tag, Tag):
        content = tag.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


# --- head -------------------------------------------------------------------


@dataclass(frozen=True)
class TitleCheck:
    """``<title>`` presence and length."""

    text: str | None
    length: int
    missing: bool
    too_short: bool
    too_long: bool
    duplicated_in_h1: bool = False


def check_title(soup: BeautifulSoup) -> TitleCheck:
    """Extract the document title and judge its length against SERP limits."""
    tag = soup.find("title")
    text = tag.get_text(strip=True) if tag else None
    if not text:
        return TitleCheck(None, 0, missing=True, too_short=False, too_long=False)
    h1 = soup.find("h1")
    return TitleCheck(
        text=text,
        length=len(text),
        missing=False,
        too_short=len(text) < TITLE_MIN,
        too_long=len(text) > TITLE_MAX,
        duplicated_in_h1=bool(h1) and h1.get_text(strip=True) == text,
    )


@dataclass(frozen=True)
class MetaDescriptionCheck:
    """``<meta name="description">`` presence and length."""

    text: str | None
    length: int
    missing: bool
    too_short: bool
    too_long: bool


def check_meta_description(soup: BeautifulSoup) -> MetaDescriptionCheck:
    """Extract the meta description and judge its length."""
    text = _meta(soup, name="description")
    if not text:
        return MetaDescriptionCheck(None, 0, missing=True, too_short=False, too_long=False)
    return MetaDescriptionCheck(
        text=text,
        length=len(text),
        missing=False,
        too_short=len(text) < META_DESC_MIN,
        too_long=len(text) > META_DESC_MAX,
    )


@dataclass(frozen=True)
class CanonicalCheck:
    """``<link rel="canonical">`` state relative to the page's own URL."""

    href: str | None
    present: bool
    is_self: bool
    is_cross_domain: bool
    multiple: bool = False


def check_canonical(soup: BeautifulSoup, page_url: str) -> CanonicalCheck:
    """Resolve the canonical link and compare it against ``page_url``."""
    tags = [t for t in soup.find_all("link") if "canonical" in [r.lower() for r in (t.get("rel") or [])]]
    if not tags:
        return CanonicalCheck(None, present=False, is_self=False, is_cross_domain=False)

    href = tags[0].get("href")
    if not isinstance(href, str) or not href.strip():
        return CanonicalCheck(None, present=False, is_self=False, is_cross_domain=False,
                              multiple=len(tags) > 1)

    resolved = normalize_url(href, base=page_url)
    return CanonicalCheck(
        href=resolved,
        present=True,
        is_self=dedupe_key(resolved) == dedupe_key(page_url),
        is_cross_domain=not same_site(resolved, page_url),
        multiple=len(tags) > 1,
    )


@dataclass(frozen=True)
class RobotsCheck:
    """Indexing directives from both the meta tag and the response header."""

    meta_content: str | None
    header_content: str | None
    noindex: bool
    nofollow: bool
    source: str | None  # "meta", "header", "meta+header" or None


def check_robots_directives(
    soup: BeautifulSoup, headers: Mapping[str, str] | None = None
) -> RobotsCheck:
    """Merge ``<meta name="robots">`` with the ``X-Robots-Tag`` response header.

    The header variant is easy to forget and silently deindexes pages, so both
    sources are read and either one can set the flags.
    """
    meta_content = _meta(soup, name="robots") or _meta(soup, name="googlebot")

    header_content = None
    if headers:
        for key, value in headers.items():
            if key.lower() == "x-robots-tag" and value.strip():
                header_content = value.strip()
                break

    def has(directive: str) -> bool:
        return any(
            directive in re.split(r"[,\s]+", (src or "").lower())
            for src in (meta_content, header_content)
        )

    sources = [n for n, v in (("meta", meta_content), ("header", header_content)) if v]
    return RobotsCheck(
        meta_content=meta_content,
        header_content=header_content,
        noindex=has("noindex") or has("none"),
        nofollow=has("nofollow") or has("none"),
        source="+".join(sources) or None,
    )


@dataclass(frozen=True)
class SocialCheck:
    """Open Graph / Twitter card coverage and the mobile viewport tag."""

    has_viewport: bool
    viewport: str | None
    og_title: str | None
    og_image: str | None
    og_description: str | None
    twitter_card: str | None

    @property
    def missing(self) -> tuple[str, ...]:
        """Names of the social tags that are absent."""
        return tuple(
            name
            for name, value in (
                ("viewport", self.viewport),
                ("og:title", self.og_title),
                ("og:image", self.og_image),
                ("twitter:card", self.twitter_card),
            )
            if not value
        )


def check_social(soup: BeautifulSoup) -> SocialCheck:
    """Collect viewport, Open Graph and Twitter card tags."""
    viewport_tag = soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)})
    viewport = viewport_tag.get("content") if isinstance(viewport_tag, Tag) else None
    return SocialCheck(
        has_viewport=bool(viewport),
        viewport=viewport,
        og_title=_meta(soup, prop="og:title"),
        og_image=_meta(soup, prop="og:image"),
        og_description=_meta(soup, prop="og:description"),
        twitter_card=_meta(soup, name="twitter:card") or _meta(soup, prop="twitter:card"),
    )


@dataclass(frozen=True)
class HreflangCheck:
    """All ``rel="alternate" hreflang`` entries declared by the page."""

    entries: tuple[tuple[str, str], ...]  # (lang, absolute href)
    has_x_default: bool
    has_self_reference: bool
    invalid_langs: tuple[str, ...]

    @property
    def present(self) -> bool:
        return bool(self.entries)


#: ISO 639-1 language, optional ISO 15924 script, optional region, or "x-default".
_HREFLANG_RE = re.compile(r"^(x-default|[a-z]{2,3}(-[a-z]{4})?(-([a-z]{2}|\d{3}))?)$", re.I)


def check_hreflang(soup: BeautifulSoup, page_url: str) -> HreflangCheck:
    """Collect hreflang alternates; reciprocity is validated site-wide later."""
    entries: list[tuple[str, str]] = []
    invalid: list[str] = []

    for tag in soup.find_all("link"):
        rel = [r.lower() for r in (tag.get("rel") or [])]
        lang = tag.get("hreflang")
        href = tag.get("href")
        if "alternate" not in rel or not isinstance(lang, str) or not isinstance(href, str):
            continue
        lang = lang.strip()
        entries.append((lang, normalize_url(href, base=page_url)))
        if not _HREFLANG_RE.match(lang):
            invalid.append(lang)

    self_url = normalize_url(page_url)
    return HreflangCheck(
        entries=tuple(entries),
        has_x_default=any(lang.lower() == "x-default" for lang, _ in entries),
        has_self_reference=any(href == self_url for _, href in entries),
        invalid_langs=tuple(invalid),
    )


# --- headings ---------------------------------------------------------------


@dataclass(frozen=True)
class HeadingCheck:
    """Heading counts, H1 text and document-order violations."""

    h1_count: int
    h1_texts: tuple[str, ...]
    h2_count: int
    h3_count: int
    order_violations: tuple[str, ...]

    @property
    def missing_h1(self) -> bool:
        return self.h1_count == 0

    @property
    def multiple_h1(self) -> bool:
        return self.h1_count > 1


def check_headings(soup: BeautifulSoup) -> HeadingCheck:
    """Count headings and detect levels that skip a rank in document order.

    An ``h3`` appearing before any ``h2`` is the classic violation; we track the
    deepest level seen so far and flag any jump of more than one rank.
    """
    headings = soup.find_all(re.compile(r"^h[1-6]$", re.I))
    levels = [int(h.name[1]) for h in headings]

    violations: list[str] = []
    deepest_seen = 0
    for level in levels:
        if deepest_seen and level > deepest_seen + 1:
            violations.append("h%d appears before any h%d" % (level, level - 1))
        deepest_seen = max(deepest_seen, level)

    if levels and levels[0] != 1:
        violations.insert(0, "first heading is h%d, not h1" % levels[0])

    h1s = tuple(h.get_text(" ", strip=True) for h in headings if h.name.lower() == "h1")
    return HeadingCheck(
        h1_count=len(h1s),
        h1_texts=h1s,
        h2_count=sum(1 for level in levels if level == 2),
        h3_count=sum(1 for level in levels if level == 3),
        order_violations=tuple(dict.fromkeys(violations)),
    )


# --- content ----------------------------------------------------------------

#: Elements that never contain body copy.
_BOILERPLATE = ("script", "style", "noscript", "template", "svg", "iframe")
#: Containers that wrap the real page content, best candidate first.
_MAIN_SELECTORS = (
    "main",
    "#MainContent",
    "#main",
    "[role=main]",
    ".shopify-section--main-product",
    "article",
    "#content",
)


def extract_main_content(soup: BeautifulSoup) -> Tag:
    """Return the element most likely to hold the page's unique content.

    Shopify themes reliably wrap the page body in ``<main>`` or ``#MainContent``;
    falling back to ``<body>`` with nav/header/footer removed keeps the content
    hash from being dominated by sitewide chrome.
    """
    for selector in _MAIN_SELECTORS:
        try:
            node = soup.select_one(selector)
        except Exception:  # pragma: no cover - malformed selector support
            continue
        if node and len(node.get_text(strip=True)) > 100:
            return node

    body = soup.body or soup
    clone = parse_html(str(body))
    for tag in clone.find_all(["nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    return clone


def _visible_text(node: Tag) -> str:
    """Text content with scripts, styles and comments stripped."""
    clone = parse_html(str(node))
    for tag in clone.find_all(_BOILERPLATE):
        tag.decompose()
    return re.sub(r"\s+", " ", clone.get_text(" ", strip=True))


def _simhash(tokens: Iterable[str], bits: int = 64) -> int:
    """64-bit SimHash over word shingles, for near-duplicate detection."""
    vector = [0] * bits
    tokens = list(tokens)
    if not tokens:
        return 0
    for token in tokens:
        digest = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
        for i in range(bits):
            vector[i] += 1 if digest >> i & 1 else -1
    return sum(1 << i for i in range(bits) if vector[i] > 0)


def hamming(a: int, b: int) -> int:
    """Bit distance between two simhashes."""
    return bin(a ^ b).count("1")


@dataclass(frozen=True)
class ContentCheck:
    """Word count and fingerprints of the page's main content block."""

    word_count: int
    content_hash: str      # exact-duplicate key
    simhash: int           # near-duplicate key
    thin: bool
    text_sample: str


def check_content(soup: BeautifulSoup, *, shingle: int = 4) -> ContentCheck:
    """Measure visible words and fingerprint the main content block."""
    text = _visible_text(extract_main_content(soup))
    words = re.findall(r"[0-9a-zÀ-ɏ'-]+", text.lower())

    shingles = [" ".join(words[i:i + shingle]) for i in range(max(0, len(words) - shingle + 1))]
    return ContentCheck(
        word_count=len(words),
        content_hash=hashlib.sha1(" ".join(words).encode()).hexdigest(),
        simhash=_simhash(shingles or words),
        thin=len(words) < THIN_CONTENT_WORDS,
        text_sample=text[:300],
    )


# --- images -----------------------------------------------------------------


@dataclass(frozen=True)
class ImageCheck:
    """Image inventory for one page."""

    count: int
    missing_alt: int
    empty_alt: int
    sources: tuple[str, ...]           # absolute, deduped, in document order
    missing_alt_sources: tuple[str, ...]
    lazy_loaded: int


def check_images(soup: BeautifulSoup, page_url: str) -> ImageCheck:
    """Count images and alt-text gaps, resolving ``src``/``data-src`` to absolute URLs.

    A present-but-empty ``alt=""`` is correct for decorative images, so it is
    tracked separately from a missing attribute.
    """
    sources: list[str] = []
    missing_sources: list[str] = []
    missing = empty = lazy = 0

    content = extract_main_content(soup)
    images = content.find_all("img")
    for img in images:
        raw = img.get("src") or img.get("data-src")
        if not raw:
            srcset = img.get("srcset") or img.get("data-srcset") or ""
            raw = srcset.split(",")[0].strip().split(" ")[0] if srcset else None
        src = normalize_url(raw, base=page_url) if isinstance(raw, str) and raw.strip() else None
        if src and src not in sources:
            sources.append(src)

        alt = img.get("alt")
        if alt is None:
            missing += 1
            if src:
                missing_sources.append(src)
        elif not alt.strip():
            empty += 1

        if (img.get("loading") or "").lower() == "lazy":
            lazy += 1

    return ImageCheck(
        count=len(images),
        missing_alt=missing,
        empty_alt=empty,
        sources=tuple(sources),
        missing_alt_sources=tuple(missing_sources),
        lazy_loaded=lazy,
    )


# --- links ------------------------------------------------------------------


@dataclass(frozen=True)
class Link:
    """One outgoing ``<a href>`` from a page."""

    url: str
    anchor: str
    internal: bool
    nofollow: bool


@dataclass(frozen=True)
class LinkCheck:
    """Outgoing link inventory for one page."""

    links: tuple[Link, ...]
    internal_count: int
    external_count: int
    nofollow_count: int
    empty_anchor_count: int

    @property
    def internal_urls(self) -> tuple[str, ...]:
        """Deduped internal link targets, in document order."""
        return tuple(dict.fromkeys(link.url for link in self.links if link.internal))


def check_links(soup: BeautifulSoup, page_url: str) -> LinkCheck:
    """Collect outgoing links with anchor text and rel attributes.

    Anchor text is kept so the crawler can build the sitewide anchor report; an
    ``<a>`` wrapping only an image falls back to that image's alt text.
    """
    links: list[Link] = []
    empty_anchor = 0

    for a in soup.find_all("a"):
        href = a.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        href = href.strip()
        if href.startswith("#") or href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue

        absolute = normalize_url(href, base=page_url)
        if not is_crawlable(absolute):
            continue

        anchor = a.get_text(" ", strip=True)
        if not anchor:
            img = a.find("img")
            anchor = (img.get("alt") or "").strip() if img else ""
            if not anchor:
                empty_anchor += 1

        rel = " ".join(a.get("rel") or []).lower()
        links.append(
            Link(
                url=absolute,
                anchor=anchor[:200],
                internal=same_site(absolute, page_url),
                nofollow="nofollow" in rel or "sponsored" in rel or "ugc" in rel,
            )
        )

    return LinkCheck(
        links=tuple(links),
        internal_count=sum(1 for link in links if link.internal),
        external_count=sum(1 for link in links if not link.internal),
        nofollow_count=sum(1 for link in links if link.nofollow),
        empty_anchor_count=empty_anchor,
    )


# --- structured data --------------------------------------------------------


def parse_jsonld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Parse every JSON-LD block into a flat list of node dicts.

    Uses ``json.loads`` (never regex), tolerates a malformed block by skipping it,
    and flattens both top-level arrays and ``@graph`` containers so callers can
    simply scan for the node type they care about.
    """
    nodes: list[dict[str, Any]] = []

    def absorb(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                absorb(item)
        elif isinstance(value, dict):
            graph = value.get("@graph")
            if isinstance(graph, (list, dict)):
                absorb(graph)
            if any(key != "@graph" for key in value):
                nodes.append(value)

    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            absorb(json.loads(raw))
        except (json.JSONDecodeError, ValueError):
            # A single broken block should not cost us the rest of the page.
            continue

    return nodes


def _types(node: Mapping[str, Any]) -> list[str]:
    """Normalize a node's ``@type`` to a list of bare type names."""
    raw = node.get("@type") or node.get("type")
    values = raw if isinstance(raw, list) else [raw]
    return [str(v).rsplit("/", 1)[-1] for v in values if v]


def find_nodes(nodes: Iterable[Mapping[str, Any]], type_name: str) -> list[Mapping[str, Any]]:
    """All JSON-LD nodes whose ``@type`` matches ``type_name`` (case-insensitive)."""
    wanted = type_name.lower()
    return [n for n in nodes if any(t.lower() == wanted for t in _types(n))]


def _as_list(value: Any) -> list[Any]:
    """Wrap a scalar in a list; pass a list through; drop ``None``."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _to_decimal_str(value: Any) -> str | None:
    """Coerce a price-ish value to a plain decimal string, or ``None``.

    ``"$179.00"``, ``179``, ``"179,00"`` and ``17900`` (cents) all show up in the
    wild; this normalizes everything except the cents case, which callers that
    know the field's units handle themselves.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "")
        match = re.search(r"\d+(?:\.\d+)?", text)
        if not match:
            return None
        number = float(match.group())
    formatted = "%.2f" % number
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


#: ``priceType`` values that mean "this is the crossed-out list price", not the
#: price the shopper actually pays. Shopify emits StrikethroughPrice on every
#: discounted variant, and counting it as a current price would invent a
#: mismatch on any product that is on sale.
_LIST_PRICE_TYPES = frozenset({"strikethroughprice", "listprice", "msrp",
                               "recommendedretailprice"})


def _offer_prices(offer: Mapping[str, Any]) -> tuple[list[str], list[str], str | None, str | None]:
    """Split one Offer into ``(current_prices, list_prices, currency, availability)``."""
    current: list[str] = []
    listed: list[str] = []
    currency: str | None = None
    availability: str | None = None

    for key in ("price", "lowPrice", "highPrice"):
        price = _to_decimal_str(offer.get(key))
        if price and price not in current:
            current.append(price)

    for spec in _as_list(offer.get("priceSpecification")):
        if not isinstance(spec, dict):
            continue
        price = _to_decimal_str(spec.get("price"))
        if not price:
            continue
        price_type = str(spec.get("priceType") or "").rsplit("/", 1)[-1].lower()
        bucket = listed if price_type in _LIST_PRICE_TYPES else current
        if price not in bucket:
            bucket.append(price)
        if currency is None and isinstance(spec.get("priceCurrency"), str):
            currency = spec["priceCurrency"]

    if currency is None and isinstance(offer.get("priceCurrency"), str):
        currency = offer["priceCurrency"]
    if isinstance(offer.get("availability"), str):
        availability = offer["availability"].rsplit("/", 1)[-1]

    return current, listed, currency, availability


def _product_nodes(nodes: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Every Product-ish node, including variants nested under a ProductGroup.

    Shopify's current markup wraps a PDP in a ``ProductGroup`` and hangs each
    variant off ``hasVariant``; the offers -- and therefore every JSON-LD price
    on the page -- live on those variants, not on the group.
    """
    found: list[Mapping[str, Any]] = []
    queue = list(find_nodes(nodes, "Product")) + list(find_nodes(nodes, "ProductGroup"))

    while queue:
        node = queue.pop(0)
        if any(node is seen for seen in found):
            continue
        found.append(node)
        for variant in _as_list(node.get("hasVariant")):
            if isinstance(variant, dict) and not any(variant is seen for seen in found):
                queue.append(variant)
    return found


@dataclass(frozen=True)
class StructuredDataCheck:
    """Schema.org types present on the page plus extracted Product details."""

    types: tuple[str, ...]
    product_name: str | None
    offer_prices: tuple[str, ...]        # prices a shopper would actually pay
    list_prices: tuple[str, ...]         # strikethrough / compare-at prices
    offer_currency: str | None
    offer_availability: str | None
    rating_value: str | None
    review_count: int | None
    has_breadcrumb: bool
    variant_count: int
    #: Identity of the ProductGroup the reviews hang off, when there is one.
    #: Two pages sharing this are variants of one product, so a shared review
    #: count between them is legitimate pooling rather than a merged pool.
    product_group_id: str | None
    node_count: int


def check_structured_data(nodes: list[dict[str, Any]]) -> StructuredDataCheck:
    """Summarize JSON-LD, pulling out the Product fields Merchant Center reads."""
    types = tuple(dict.fromkeys(t for node in nodes for t in _types(node)))
    products = _product_nodes(nodes)

    prices: list[str] = []
    listed: list[str] = []
    currency: str | None = None
    availability: str | None = None
    rating: str | None = None
    review_count: int | None = None
    name: str | None = None
    group_id: str | None = None
    variants = 0

    for group in find_nodes(nodes, "ProductGroup"):
        for key in ("productGroupID", "@id"):
            value = group.get(key)
            if group_id is None and isinstance(value, str) and value.strip():
                group_id = value.strip()

    for product in products:
        if name is None and isinstance(product.get("name"), str):
            name = product["name"].strip()
        variants += len(_as_list(product.get("hasVariant")))

        for offer in _as_list(product.get("offers")):
            if not isinstance(offer, dict):
                continue
            current, strikethrough, offer_currency, offer_availability = _offer_prices(offer)
            prices.extend(p for p in current if p not in prices)
            listed.extend(p for p in strikethrough if p not in listed)
            currency = currency or offer_currency
            availability = availability or offer_availability

        for agg in _as_list(product.get("aggregateRating")):
            if not isinstance(agg, dict):
                continue
            if rating is None:
                rating = _to_decimal_str(agg.get("ratingValue"))
            for key in ("reviewCount", "ratingCount"):
                parsed = _to_decimal_str(agg.get(key))
                if parsed is not None:
                    review_count = max(review_count or 0, int(float(parsed)))

    return StructuredDataCheck(
        types=types,
        product_name=name,
        offer_prices=tuple(prices),
        list_prices=tuple(listed),
        offer_currency=currency,
        offer_availability=availability,
        rating_value=rating,
        review_count=review_count,
        has_breadcrumb=bool(find_nodes(nodes, "BreadcrumbList")),
        variant_count=variants,
        product_group_id=group_id,
        node_count=len(nodes),
    )


# --- price consistency (the signature check) --------------------------------


@dataclass(frozen=True)
class PriceCheck:
    """Every price the page advertises, and whether they agree.

    Merchant Center reads the Open Graph / microdata prices while Search reads
    the JSON-LD one; when they disagree the feed earns a price-mismatch
    disapproval and the product stops serving.
    """

    sources: dict[str, str] = field(default_factory=dict)  # source label -> price
    currencies: dict[str, str] = field(default_factory=dict)
    consistent: bool = True
    currency_consistent: bool = True
    is_product: bool = False

    @property
    def distinct_prices(self) -> tuple[str, ...]:
        """Every distinct price value found, in discovery order."""
        return tuple(dict.fromkeys(self.sources.values()))

    def summary(self) -> str:
        """Human-readable ``source=price`` list for the report detail column."""
        return ", ".join("%s=%s" % (k, v) for k, v in self.sources.items())


#: Meta properties that advertise a price, mapped to their report label.
_PRICE_META = (
    ("og:price:amount", "og:price:amount"),
    ("og:price:standard_amount", "og:price:standard_amount"),
    ("product:price:amount", "product:price:amount"),
    ("product:sale_price:amount", "product:sale_price:amount"),
)


def check_price_consistency(
    soup: BeautifulSoup, structured: StructuredDataCheck
) -> PriceCheck:
    """Compare OG, ``product:``, microdata and JSON-LD prices for disagreement.

    Three kinds of legitimate difference are excluded before anything is called
    a mismatch:

    * a declared sale price differing from the standard/list price,
    * a JSON-LD strikethrough (compare-at) price, which is never the live price,
    * a multi-variant product whose variants genuinely cost different amounts.

    What remains is a real disagreement between two signals that both claim to
    be the current price. A lone ``og:price:standard_amount`` disagreeing with
    the live price, with no sale price declared to explain it, also counts --
    it is the only OG price on the page, so that is the number a feed scraper
    reads.
    """
    sources: dict[str, str] = {}
    currencies: dict[str, str] = {}

    for label, prop in _PRICE_META:
        price = _to_decimal_str(_meta(soup, prop=prop) or _meta(soup, name=prop))
        if price:
            sources[label] = price

    for label, prop in (
        ("og:price:currency", "og:price:currency"),
        ("product:price:currency", "product:price:currency"),
    ):
        value = _meta(soup, prop=prop) or _meta(soup, name=prop)
        if isinstance(value, str) and value.strip():
            currencies[label] = value.strip().upper()

    # Microdata itemprop="price", still emitted by many Shopify themes.
    for node in soup.find_all(attrs={"itemprop": re.compile(r"^price$", re.I)}):
        price = _to_decimal_str(node.get("content") or node.get_text(strip=True))
        if price:
            sources.setdefault("itemprop:price", price)
            break

    jsonld_current = set(structured.offer_prices)
    if structured.offer_prices:
        sources["jsonld:offers.price"] = (
            structured.offer_prices[0] if len(structured.offer_prices) == 1
            else "%s-%s" % (min(structured.offer_prices, key=float),
                            max(structured.offer_prices, key=float))
        )
    if structured.list_prices:
        sources["jsonld:listPrice"] = structured.list_prices[0]
    if structured.offer_currency:
        currencies["jsonld:priceCurrency"] = structured.offer_currency.upper()

    # Signals that each claim to be the price a shopper pays right now.
    meta_current = {
        label: value for label, value in sources.items()
        if label in ("og:price:amount", "product:price:amount",
                     "product:sale_price:amount", "itemprop:price")
    }
    distinct_meta = set(meta_current.values())

    consistent = len(distinct_meta) <= 1
    # Against JSON-LD, a meta price only has to match *some* variant: a product
    # whose variants cost 89 and 99 is not inconsistent for advertising 89.
    if consistent and jsonld_current and distinct_meta:
        consistent = bool(distinct_meta & jsonld_current)

    standard = sources.get("og:price:standard_amount")
    has_sale_price = "product:sale_price:amount" in sources
    live_prices = distinct_meta | jsonld_current
    if standard and live_prices and standard not in live_prices and not has_sale_price:
        consistent = False

    return PriceCheck(
        sources=sources,
        currencies=currencies,
        consistent=consistent,
        currency_consistent=len(set(currencies.values())) <= 1,
        is_product=bool({"Product", "ProductGroup"} & set(structured.types)) or bool(sources),
    )


# --- review sanity ----------------------------------------------------------

#: Class tokens the common Shopify review apps (Judge.me, Loox, Yotpo, Okendo,
#: Stamped, Shopify Product Reviews) put on a *single* review block. Matched as
#: whole tokens, because every one of these apps also ships a widget wrapper
#: whose class merely starts with the same fragment (``jdgm-rev-widget``), and
#: counting the wrapper would inflate the on-page total by one per widget.
_REVIEW_BLOCK_TOKENS = frozenset(
    {
        "jdgm-rev", "spr-review", "review-item", "review__item", "review-card",
        "loox-review", "yotpo-review", "okendo-review", "stamped-review",
        "r--review",
    }
)
#: BEM-style modifier prefixes that still denote one review (``jdgm-rev--gap``).
_REVIEW_BLOCK_PREFIXES = ("jdgm-rev--", "review-item--", "review__item--")


def _is_review_block(tag: Tag) -> bool:
    """True when ``tag`` looks like one rendered review, not a widget wrapper."""
    tokens = tag.get("class") or []
    if isinstance(tokens, str):
        tokens = tokens.split()
    for token in tokens:
        lowered = token.lower()
        if lowered in _REVIEW_BLOCK_TOKENS or lowered.startswith(_REVIEW_BLOCK_PREFIXES):
            return True
    return str(tag.get("itemprop", "")).lower() == "review"


@dataclass(frozen=True)
class ReviewSanityCheck:
    """JSON-LD review count vs. what the rendered page actually shows."""

    schema_count: int | None
    onpage_count: int
    ratio: float | None
    suspicious: bool
    reason: str | None


def check_review_sanity(
    soup: BeautifulSoup,
    structured: StructuredDataCheck,
    *,
    ratio_threshold: float = 3.0,
    min_schema_count: int = 10,
) -> ReviewSanityCheck:
    """Flag Product schema claiming far more reviews than the page displays.

    A merged-reviews setup -- one review pool shared across every variant or the
    whole catalogue -- trips Google's review-snippet policy and can cost the
    star rating sitewide. The heuristic counts review blocks emitted by the
    common Shopify review apps and compares against the schema's count.
    """
    schema_count = structured.review_count
    onpage = len(soup.find_all(_is_review_block))

    if schema_count is None or schema_count < min_schema_count:
        return ReviewSanityCheck(schema_count, onpage, None, suspicious=False, reason=None)

    if onpage == 0:
        return ReviewSanityCheck(
            schema_count, onpage, float("inf"), suspicious=True,
            reason="schema claims %d reviews but no review blocks render on the page"
                   % schema_count,
        )

    # Paginated widgets legitimately show one page of reviews at a time, so the
    # threshold sits high enough to ignore that and catch merged pools instead.
    ratio = schema_count / onpage
    if ratio >= ratio_threshold:
        return ReviewSanityCheck(
            schema_count, onpage, ratio, suspicious=True,
            reason="schema claims %d reviews, page renders ~%d (%.0fx gap)"
                   % (schema_count, onpage, ratio),
        )
    return ReviewSanityCheck(schema_count, onpage, ratio, suspicious=False, reason=None)


# --- spelling ---------------------------------------------------------------

#: Whitelist candidates must look like a name, not a sentence-initial word.
_CAPITALIZED = re.compile(r"[A-Z][A-Za-z0-9]*(?:[0-9]+|[a-z]+[A-Za-z0-9]*)")


def page_language(soup: BeautifulSoup) -> str | None:
    """The document's declared language, lowercased, or ``None``."""
    html = soup.find("html")
    lang = html.get("lang") if isinstance(html, Tag) else None
    return lang.strip().lower() if isinstance(lang, str) and lang.strip() else None


def language_matches(soup: BeautifulSoup, expected: str) -> bool:
    """True when the page declares ``expected`` (or declares nothing at all).

    A page with no ``lang`` attribute is assumed to be in the audit language
    rather than skipped, because plenty of themes omit it on some templates.
    """
    declared = page_language(soup)
    return declared is None or declared.split("-")[0] == expected.split("-")[0].lower()


def collect_brand_candidates(soup: BeautifulSoup) -> tuple[str, ...]:
    """Capitalized words appearing mid-sentence -- likely brand or product names.

    Across a whole crawl, a word that appears capitalized mid-sentence on
    several pages is almost certainly a proper noun (``Leuchtturm1917``,
    ``Ritza``, ``Seidel``) rather than a typo. That is what lets a site build
    its own spelling whitelist without anyone maintaining a word list by hand.
    """
    text = _visible_text(extract_main_content(soup))
    candidates: list[str] = []

    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        # Skip each sentence's first word: it is capitalized by position.
        words = sentence.strip().split()
        for word in words[1:]:
            stripped = word.strip(".,;:!?()[]\"'“”‘’")
            if len(stripped) > 2 and _CAPITALIZED.fullmatch(stripped):
                candidates.append(stripped)

    return tuple(dict.fromkeys(candidates))


#: Class-name fragments marking a review app's rendered output. Everything
#: inside one of these is customer writing, not the shop's copy.
_REVIEW_WIDGET_RE = re.compile(
    r"(jdgm|spr-review|spr-content|loox|yotpo|okendo|stamped|"
    r"reviews?-(widget|list|section|container)|product-reviews)",
    re.I,
)


def spellcheckable_text(soup: BeautifulSoup) -> str:
    """Main-content text with customer-generated blocks removed.

    Review widgets are stripped before spellchecking. A typo in a customer's
    review is not the shop's typo, cannot be fixed by the shop, and on a site
    with thousands of reviews it drowns the handful of real errors in the copy
    that was actually written by the business.
    """
    clone = parse_html(str(extract_main_content(soup)))

    for tag in list(clone.find_all(True)):
        if not tag.parent:                       # already removed with an ancestor
            continue
        tokens = tag.get("class") or []
        if isinstance(tokens, str):
            tokens = tokens.split()
        identifier = " ".join(tokens) + " " + str(tag.get("id") or "")
        if _REVIEW_WIDGET_RE.search(identifier) or _is_review_block(tag):
            tag.decompose()

    return _visible_text(clone)


@dataclass(frozen=True)
class SpellingCheck:
    """Misspellings found in each customer-facing field of one page."""

    title: tuple[str, ...]
    meta_description: tuple[str, ...]
    h1: tuple[str, ...]
    body: tuple[str, ...]
    brand_candidates: tuple[str, ...]
    skipped: bool = False          # wrong language, or engine unavailable
    skip_reason: str | None = None

    @property
    def any_found(self) -> bool:
        return bool(self.title or self.meta_description or self.h1 or self.body)


#: Cap on stored body misspellings per page. The report shows the five worst,
#: and keeping every instance of a repeated typo would bloat the resume cache.
MAX_BODY_MISSPELLINGS = 50


def check_spelling(
    soup: BeautifulSoup,
    engine: Any,
    *,
    lang: str = "en",
    whitelist: Iterable[str] = (),
) -> SpellingCheck:
    """Spellcheck the title, meta description, H1 and main content block.

    The whitelist passed here is the *static* one (user-supplied plus words from
    the domain name). Brand terms discovered across the crawl are subtracted
    afterwards, once every page has been seen, because a term only qualifies
    once it has appeared on several pages.
    """
    if not language_matches(soup, lang):
        return SpellingCheck((), (), (), (), (), skipped=True,
                             skip_reason="page language is %s" % page_language(soup))

    allow = {w.lower() for w in whitelist}

    def check(text: str | None, limit: int | None = None) -> tuple[str, ...]:
        if not text:
            return ()
        found = [w for w in engine.unknown(text) if w.lower() not in allow]
        return tuple(found[:limit] if limit else found)

    title = check_title(soup)
    description = check_meta_description(soup)
    headings = check_headings(soup)
    body_text = spellcheckable_text(soup)

    return SpellingCheck(
        title=check(title.text),
        meta_description=check(description.text),
        h1=check(headings.h1_texts[0] if headings.h1_texts else None),
        body=check(body_text, MAX_BODY_MISSPELLINGS),
        brand_candidates=collect_brand_candidates(soup),
    )


# --- image roles and LCP loading --------------------------------------------

#: Below this rendered width an image is furniture, not content.
THUMBNAIL_MAX_PX = 120
#: At or above this rendered width, an image should be served responsively.
LARGE_IMAGE_PX = 300
#: Assumed viewport width when converting a ``vw`` size to pixels.
ASSUMED_VIEWPORT_PX = 1280


def parse_sizes(value: str | None) -> tuple[float | None, bool]:
    """Interpret a ``sizes`` attribute as ``(largest_px, viewport_relative)``.

    ``sizes`` is a media-query list -- ``(max-width: 768px) 100vw, 50vw`` -- so
    widths are extracted from every clause and the largest wins. A ``vw`` unit
    means the image scales with the viewport, which is the strongest available
    signal that it is a hero or gallery image rather than a thumbnail.
    """
    if not value or not value.strip():
        return None, False

    viewport_relative = False
    widths: list[float] = []

    for clause in value.split(","):
        # Drop any leading media condition; only the width part matters.
        candidate = clause.split(")")[-1].strip()
        match = re.search(r"([\d.]+)\s*(px|vw|em|rem)\b", candidate, re.I)
        if not match:
            continue
        number, unit = float(match.group(1)), match.group(2).lower()
        if unit == "vw":
            viewport_relative = True
            widths.append(number / 100 * ASSUMED_VIEWPORT_PX)
        elif unit in ("em", "rem"):
            widths.append(number * 16)
        else:
            widths.append(number)

    return (max(widths) if widths else None), viewport_relative


def srcset_max_width(value: str | None) -> int | None:
    """Largest ``w`` descriptor in a ``srcset``, or ``None``."""
    if not value:
        return None
    widths = [int(m) for m in re.findall(r"(\d+)w", value)]
    return max(widths) if widths else None


def _rendered_width(img: Tag) -> tuple[float | None, bool]:
    """Best guess at an image's rendered width, and whether it scales."""
    width, viewport_relative = parse_sizes(img.get("sizes"))
    if width is not None:
        return width, viewport_relative

    raw = img.get("width")
    if isinstance(raw, str) and raw.strip().rstrip("px").isdigit():
        return float(raw.strip().rstrip("px")), False

    return None, False


@dataclass(frozen=True)
class ImageRole:
    """One image with its inferred role and loading attributes."""

    src: str
    role: str              # hero | gallery | thumbnail | unknown
    rendered_px: float | None
    viewport_relative: bool
    lazy: bool
    fetchpriority: str | None
    has_srcset: bool
    srcset_max: int | None
    index: int             # document order within the main content block


@dataclass(frozen=True)
class ImageLoadingCheck:
    """Per-page verdict on image roles and their loading strategy."""

    images: tuple[ImageRole, ...]
    hero: ImageRole | None
    hero_lazy: bool
    fetchpriority_present: bool
    lazy_count: int
    eager_belowfold: tuple[str, ...]
    large_without_srcset: tuple[str, ...]

    @property
    def eager_belowfold_count(self) -> int:
        return len(self.eager_belowfold)


def classify_image_role(
    img: Tag, *, hero_taken: bool
) -> tuple[str, float | None, bool]:
    """Infer whether an image is a hero, a gallery shot or a thumbnail.

    Classification happens *before* any judgement, because the very same
    ``loading="lazy"`` attribute is correct on a thumbnail and a defect on a
    hero. Evidence, in order of trust: the ``sizes`` attribute, an explicit
    ``width``, the ``srcset`` width range, and document position.
    """
    rendered, viewport_relative = _rendered_width(img)
    widest = srcset_max_width(img.get("srcset") or img.get("data-srcset"))

    if rendered is not None and rendered <= THUMBNAIL_MAX_PX:
        return "thumbnail", rendered, viewport_relative

    looks_large = (
        viewport_relative
        or (rendered is not None and rendered >= LARGE_IMAGE_PX)
        or (rendered is None and widest is not None and widest >= 800)
    )
    if not looks_large:
        return ("unknown" if rendered is None else "thumbnail"), rendered, viewport_relative

    # The first large image inside the main content block is the hero, and
    # therefore the LCP candidate; later large images are gallery shots.
    return ("gallery" if hero_taken else "hero"), rendered, viewport_relative


def check_image_loading(soup: BeautifulSoup, page_url: str) -> ImageLoadingCheck:
    """Classify every content image and judge its loading strategy by role."""
    content = extract_main_content(soup)

    images: list[ImageRole] = []
    hero: ImageRole | None = None

    for index, img in enumerate(content.find_all("img")):
        raw = img.get("src") or img.get("data-src") or ""
        src = normalize_url(raw, base=page_url) if isinstance(raw, str) and raw.strip() else ""

        role, rendered, viewport_relative = classify_image_role(img, hero_taken=hero is not None)
        srcset = img.get("srcset") or img.get("data-srcset")
        record = ImageRole(
            src=src,
            role=role,
            rendered_px=rendered,
            viewport_relative=viewport_relative,
            lazy=(img.get("loading") or "").strip().lower() == "lazy",
            fetchpriority=(img.get("fetchpriority") or "").strip().lower() or None,
            has_srcset=bool(srcset),
            srcset_max=srcset_max_width(srcset),
            index=index,
        )
        images.append(record)
        if record.role == "hero":
            hero = record

    # "Below the fold" is approximated as "after the hero in document order":
    # anything loading eagerly down there competes with the LCP element.
    eager_belowfold = tuple(
        image.src for image in images
        if hero is not None and image.index > hero.index and not image.lazy
    )

    return ImageLoadingCheck(
        images=tuple(images),
        hero=hero,
        hero_lazy=bool(hero and hero.lazy),
        fetchpriority_present=bool(hero and hero.fetchpriority == "high"),
        lazy_count=sum(1 for image in images if image.lazy),
        eager_belowfold=eager_belowfold,
        large_without_srcset=tuple(
            image.src for image in images
            if image.role in ("hero", "gallery") and not image.has_srcset and image.src
        ),
    )


# --- heading furniture ------------------------------------------------------

#: Heading texts that describe template UI rather than page content.
FURNITURE_HEADINGS = (
    "featured collections", "you may also like", "related products",
    "recently viewed", "customer reviews", "share", "newsletter",
    "follow us", "shop the look", "you might also like", "more like this",
)

#: A page with at least this many real subheadings has a genuine structure and
#: is never reported, however much furniture sits alongside it.
RICH_HEADING_COUNT = 5
#: Above this many subheadings the page is clearly using headings for content.
MAX_SUBHEADINGS_FOR_FINDING = 3


def is_furniture_heading(text: str, furniture: Iterable[str] = FURNITURE_HEADINGS) -> bool:
    """True when a heading's text is template furniture rather than content."""
    normalized = re.sub(r"\s+", " ", (text or "")).strip().lower().rstrip(":")
    return any(normalized == phrase or normalized.startswith(phrase + " ")
               for phrase in furniture)


@dataclass(frozen=True)
class HeadingFurnitureCheck:
    """Whether a page's heading tree describes content or only template UI."""

    furniture: tuple[str, ...]
    content_headings: tuple[str, ...]
    total_subheadings: int
    furniture_ratio: float
    unmarked_content: bool


def check_heading_furniture(
    soup: BeautifulSoup, furniture: Iterable[str] = FURNITURE_HEADINGS
) -> HeadingFurnitureCheck:
    """Flag pages whose only subheadings are template furniture.

    The pattern: a product page whose only H2s are "Featured Collections" and
    "You may also like", while Description, Warranty and Shipping are styled
    divs. The heading tree describes the theme rather than the product, so
    nothing helps a search engine parse the sections that actually sell.
    """
    content = extract_main_content(soup)
    subheadings = [
        text for text in (
            h.get_text(" ", strip=True)
            for h in content.find_all(re.compile(r"^h[2-3]$", re.I))
        ) if text
    ]

    furniture_found = tuple(h for h in subheadings if is_furniture_heading(h, furniture))
    content_found = tuple(h for h in subheadings if not is_furniture_heading(h, furniture))

    total = len(subheadings)
    ratio = len(furniture_found) / total if total else 0.0

    return HeadingFurnitureCheck(
        furniture=furniture_found,
        content_headings=content_found,
        total_subheadings=total,
        furniture_ratio=ratio,
        unmarked_content=(
            total > 0
            and ratio >= 0.5
            and total <= MAX_SUBHEADINGS_FOR_FINDING
            and len(content_found) < RICH_HEADING_COUNT
        ),
    )
