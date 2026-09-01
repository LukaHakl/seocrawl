"""Site-level findings: the patterns you can only see once the crawl is done.

Each analyser takes the whole crawl and yields :class:`Finding` objects. Every
finding carries a plain-money "why it matters" line, because a list of technical
defects is not an audit -- an audit says what the defect costs.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Sequence

from .checks import NEAR_DUPLICATE_DISTANCE, hamming
from .crawler import OutLink, PageResult
from .urls import normalize_url, registrable_host

HIGH, MED, LOW = "HIGH", "MED", "LOW"
SEVERITY_ORDER = {HIGH: 0, MED: 1, LOW: 2}

#: How many affected URLs to keep on a finding before truncating the list.
MAX_URLS_PER_FINDING = 200

#: Anchor texts that promise a specific destination. A link reading "Sale" that
#: lands on the homepage has broken a promise; one reading "Home" has not.
DESTINATION_ANCHORS = (
    "sale", "shop", "popular", "new", "new arrivals", "best sellers",
    "bestsellers", "collection", "collections", "clearance", "deals", "outlet",
    "gifts", "browse", "shop now", "learn more",
)


@dataclass
class Finding:
    """One site-level issue, ready to print, export or diff."""

    severity: str
    category: str
    issue: str
    urls: list[str] = field(default_factory=list)
    detail: str = ""
    why_it_matters: str = ""

    @property
    def count(self) -> int:
        return len(self.urls)

    @property
    def key(self) -> str:
        """Stable identity for diffing one crawl against another."""
        return "%s|%s" % (self.category, self.issue)

    def sort_key(self) -> tuple[int, int, str]:
        return (SEVERITY_ORDER.get(self.severity, 3), -self.count, self.issue)


@dataclass
class SiteStats:
    """Headline counts for the Summary sheet."""

    total_urls: int = 0
    indexable: int = 0
    non_indexable: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    products: int = 0
    orphans: int = 0
    ghosts: int = 0
    avg_word_count: int = 0
    pages_with_issues: dict[str, int] = field(default_factory=dict)
    issue_rate: dict[str, str] = field(default_factory=dict)


class Audit:
    """Holds one crawl's results and derives every site-level finding."""

    def __init__(
        self,
        pages: dict[str, PageResult],
        links: Sequence[OutLink],
        sitemap_urls: Iterable[str] = (),
        *,
        near_duplicate_distance: int = NEAR_DUPLICATE_DISTANCE,
        destination_anchors: Iterable[str] = (),
    ) -> None:
        self.pages = pages
        self.links = list(links)
        self.sitemap_urls = set(sitemap_urls)
        self.near_duplicate_distance = near_duplicate_distance
        self.destination_anchors = tuple(destination_anchors) or DESTINATION_ANCHORS

        #: Pages worth judging on content quality: reachable, indexable HTML.
        self.indexable = [p for p in pages.values() if p.indexable]

    # --- helpers ---

    def _finding(
        self, severity: str, category: str, issue: str,
        urls: Iterable[str], detail: str, why: str,
    ) -> Finding:
        """Build a finding, truncating very long URL lists."""
        url_list = list(dict.fromkeys(urls))
        if len(url_list) > MAX_URLS_PER_FINDING:
            detail = "%s (showing first %d of %d URLs)" % (
                detail, MAX_URLS_PER_FINDING, len(url_list)
            )
            url_list = url_list[:MAX_URLS_PER_FINDING]
        return Finding(severity, category, issue, url_list, detail, why)

    # --- indexability -------------------------------------------------------

    def check_broken_pages(self) -> Iterator[Finding]:
        """4xx/5xx responses and outright request failures."""
        server_errors = [p.url for p in self.pages.values() if (p.status or 0) >= 500]
        if server_errors:
            yield self._finding(
                HIGH, "indexability", "Server errors (5xx)", server_errors,
                "%d URLs returned a server error" % len(server_errors),
                "Google drops 5xx URLs from the index within days and slows crawling "
                "of the whole site while they persist.",
            )

        not_found = [p.url for p in self.pages.values() if 400 <= (p.status or 0) < 500]
        if not_found:
            in_sitemap = [u for u in not_found if u in self.sitemap_urls]
            severity = HIGH if in_sitemap else MED
            yield self._finding(
                severity, "indexability", "Dead URLs (4xx)", not_found,
                "%d dead URLs%s" % (
                    len(not_found),
                    ", %d of them still listed in the sitemap" % len(in_sitemap)
                    if in_sitemap else "",
                ),
                "Every dead URL in the sitemap teaches Google the sitemap is unreliable, "
                "so it crawls the rest of it less often.",
            )

        failed = [p.url for p in self.pages.values() if p.error and p.status is None]
        if failed:
            yield self._finding(
                MED, "indexability", "Request failures", failed,
                "%d URLs could not be fetched (timeout or connection error)" % len(failed),
                "Pages that time out for a crawler time out for shoppers too; if it is "
                "intermittent, Google logs it as a crawl error and backs off.",
            )

    def check_noindex(self) -> Iterator[Finding]:
        """Pages excluded from the index that the site still links to."""
        linked_noindex = [
            p.url for p in self.pages.values()
            if p.noindex and p.status == 200 and p.inlinks > 0
        ]
        if linked_noindex:
            via_header = [
                p.url for p in self.pages.values()
                if p.noindex and p.x_robots_tag and p.inlinks > 0
            ]
            detail = "%d internally linked pages carry noindex" % len(linked_noindex)
            if via_header:
                detail += " (%d of them via the X-Robots-Tag header, not the meta tag)" % len(via_header)
            yield self._finding(
                HIGH, "indexability", "Noindex on internally linked pages", linked_noindex,
                detail,
                "These pages cannot rank or earn traffic, yet the site spends internal "
                "link equity pointing at them. If the noindex is accidental, this is "
                "usually the single biggest traffic hole in an audit.",
            )

    def check_canonicals(self) -> Iterator[Finding]:
        """Canonical tags pointing off-site or missing entirely."""
        cross = [p.url for p in self.pages.values()
                 if p.canonical_state == "cross-domain" and p.status == 200]
        if cross:
            yield self._finding(
                HIGH, "canonical", "Canonical points to another domain", cross,
                "%d pages canonicalise to an external domain" % len(cross),
                "You are handing the ranking to someone else's URL -- these pages will "
                "not rank at all, no matter how good the content is.",
            )

        missing = [p.url for p in self.pages.values()
                   if p.canonical_state == "missing" and p.status == 200]
        if missing:
            yield self._finding(
                MED, "canonical", "Missing canonical tag", missing,
                "%d indexable pages have no canonical link" % len(missing),
                "Without a canonical, any URL variant Google finds (?variant=, ?utm_, "
                "trailing slash) can be indexed instead of the clean one and split the "
                "ranking signals between them.",
            )

    # --- duplication --------------------------------------------------------

    def _duplicate_groups(self, attribute: str) -> list[tuple[str, list[str]]]:
        """Group indexable pages by a shared non-empty attribute value."""
        groups: dict[str, list[str]] = defaultdict(list)
        for page in self.indexable:
            value = getattr(page, attribute)
            if value:
                groups[value].append(page.url)
        return [(v, urls) for v, urls in groups.items() if len(urls) > 1]

    def check_duplicate_titles(self) -> Iterator[Finding]:
        """Identical <title> across more than one indexable page."""
        groups = sorted(self._duplicate_groups("title"), key=lambda g: -len(g[1]))
        if not groups:
            return
        affected = [url for _, urls in groups for url in urls]
        biggest = groups[0]
        yield self._finding(
            HIGH, "duplication", "Duplicate title tags", affected,
            "%d pages share %d duplicated titles; the largest group is %d pages titled %r"
            % (len(affected), len(groups), len(biggest[1]), biggest[0][:80]),
            "When several pages carry the same title Google picks one to rank and treats "
            "the rest as also-rans -- they cannibalise each other instead of covering "
            "different queries.",
        )

    def check_duplicate_descriptions(self) -> Iterator[Finding]:
        """Identical meta description across more than one indexable page."""
        groups = sorted(self._duplicate_groups("meta_description"), key=lambda g: -len(g[1]))
        if not groups:
            return
        affected = [url for _, urls in groups for url in urls]
        yield self._finding(
            MED, "duplication", "Duplicate meta descriptions", affected,
            "%d pages share %d duplicated descriptions" % (len(affected), len(groups)),
            "A description that describes every page describes none of them; Google "
            "rewrites it with page text and you lose control of the click-through pitch.",
        )

    def check_duplicate_content(self) -> Iterator[Finding]:
        """Exact and near-duplicate main content across indexable pages."""
        exact = sorted(self._duplicate_groups("content_hash"), key=lambda g: -len(g[1]))
        if exact:
            affected = [url for _, urls in exact for url in urls]
            yield self._finding(
                HIGH, "duplication", "Identical page content", affected,
                "%d pages fall into %d groups with byte-identical main content"
                % (len(affected), len(exact)),
                "Identical pages compete for the same query and dilute each other's "
                "link equity; on a product catalogue this is usually variant URLs that "
                "should canonicalise to one parent.",
            )

        # Near-duplicates: compare only pages not already exactly duplicated.
        exact_urls = {url for _, urls in exact for url in urls}
        candidates = [p for p in self.indexable
                      if p.url not in exact_urls and p.simhash and p.word_count >= 50]

        seen: set[str] = set()
        clusters: list[list[str]] = []
        for i, page in enumerate(candidates):
            if page.url in seen:
                continue
            cluster = [page.url]
            for other in candidates[i + 1:]:
                if other.url in seen:
                    continue
                if hamming(page.simhash, other.simhash) <= self.near_duplicate_distance:
                    cluster.append(other.url)
                    seen.add(other.url)
            if len(cluster) > 1:
                seen.add(page.url)
                clusters.append(cluster)

        if clusters:
            affected = [url for cluster in clusters for url in cluster]
            yield self._finding(
                MED, "duplication", "Near-duplicate content", affected,
                "%d pages fall into %d near-identical clusters (largest: %d pages)"
                % (len(affected), len(clusters), max(len(c) for c in clusters)),
                "Pages that differ only in a product name read as boilerplate. Google "
                "indexes one and quietly ignores the rest, so the long-tail terms in "
                "those descriptions never rank.",
            )

    # --- link graph ---------------------------------------------------------

    def check_orphans_and_ghosts(self) -> Iterator[Finding]:
        """Sitemap pages nothing links to, and linked pages the sitemap forgot."""
        orphans = [
            p.url for p in self.pages.values()
            if p.url in self.sitemap_urls and p.inlinks == 0
            and p.status == 200 and p.url not in self._start_urls()
        ]
        if orphans:
            yield self._finding(
                MED, "structure", "Orphan pages (in sitemap, no internal links)", orphans,
                "%d pages are reachable only via the sitemap" % len(orphans),
                "An orphan gets almost no crawl priority and no internal link equity, so "
                "it ranks far below its content quality. One link from a category page "
                "usually fixes it.",
            )

        if self.sitemap_urls:
            ghosts = [
                p.url for p in self.pages.values()
                if p.url not in self.sitemap_urls and p.status == 200
                and p.indexable and p.inlinks > 0
            ]
            if ghosts:
                yield self._finding(
                    MED, "structure", "Ghost pages (linked, missing from sitemap)", ghosts,
                    "%d indexable linked pages are absent from the sitemap" % len(ghosts),
                    "The sitemap is how Google finds new and updated pages fastest. "
                    "Anything missing from it waits on link discovery instead, which on "
                    "a small site can mean weeks.",
                )

    def _start_urls(self) -> set[str]:
        """URLs seeded rather than discovered -- a homepage is never an orphan."""
        return {p.url for p in self.pages.values() if p.discovered_by == "start"}

    def check_broken_internal_links(self) -> Iterator[Finding]:
        """Internal links whose target returned 4xx/5xx, named source-first."""
        dead = {
            p.url for p in self.pages.values()
            if (p.status or 0) >= 400 or (p.error and p.status is None)
        }
        if not dead:
            return

        broken = [link for link in self.links if link.internal and link.target in dead]
        if not broken:
            return

        by_source: dict[str, list[str]] = defaultdict(list)
        for link in broken:
            by_source[link.source].append(link.target)

        details = [
            "%s -> %s" % (source, targets[0] if len(targets) == 1
                          else "%s (+%d more)" % (targets[0], len(targets) - 1))
            for source, targets in list(by_source.items())[:5]
        ]
        yield self._finding(
            HIGH, "links", "Broken internal links", sorted(by_source),
            "%d links from %d pages point at %d dead URLs. Examples: %s"
            % (len(broken), len(by_source), len(set(l.target for l in broken)),
               "; ".join(details)),
            "Every broken link is a shopper hitting a 404 mid-journey and link equity "
            "poured into a dead end.",
        )

    def check_links_to_redirects(self) -> Iterator[Finding]:
        """Internal links pointing at a URL that redirects."""
        redirected = {
            p.url for p in self.pages.values()
            if p.redirect_hops > 0 and (p.status or 0) < 400
        }
        sources = sorted({
            link.source for link in self.links
            if link.internal and link.target in redirected
        })
        if not sources:
            return
        yield self._finding(
            LOW, "links", "Internal links pointing at redirects", sources,
            "%d pages link to URLs that redirect instead of linking to the destination"
            % len(sources),
            "Each hop wastes crawl budget and a sliver of link equity. Updating the "
            "hrefs to the final URL is a one-time find-and-replace.",
        )

    def check_redirects(self) -> Iterator[Finding]:
        """Multi-hop chains and loops."""
        chains = [p.url for p in self.pages.values() if p.redirect_hops > 1]
        if chains:
            worst = max(self.pages.values(), key=lambda p: p.redirect_hops)
            yield self._finding(
                MED, "redirects", "Redirect chains longer than one hop", chains,
                "%d URLs redirect more than once (worst: %d hops from %s)"
                % (len(chains), worst.redirect_hops, worst.url),
                "Chains slow every page load that hits them and Google gives up after "
                "about five hops, leaving the destination undiscovered.",
            )

        # A URL repeating inside a chain is only a loop if the chain never
        # resolves. OAuth and login bounces legitimately revisit the same
        # authorize endpoint twice and still land on a 200.
        loops = []
        for page in self.pages.values():
            if not page.redirect_chain:
                continue
            targets = [hop.split(" -> ", 1)[-1] for hop in page.redirect_chain]
            returned_to_start = page.url in targets
            never_resolved = 300 <= (page.status or 0) < 400
            if returned_to_start or (never_resolved and len(targets) != len(set(targets))):
                loops.append(page.url)
        if loops:
            yield self._finding(
                HIGH, "redirects", "Redirect loops", loops,
                "%d URLs redirect in a cycle and never resolve" % len(loops),
                "A looping URL is completely inaccessible -- to shoppers, to Google, and "
                "to any ad or email campaign still pointing at it.",
            )

    # --- hreflang -----------------------------------------------------------

    def check_hreflang(self) -> Iterator[Finding]:
        """Reciprocity, self-reference and x-default across the hreflang cluster."""
        declaring = [p for p in self.pages.values() if p.hreflang]
        if not declaring:
            return

        # Reciprocity: if A names B, B must name A back. Only pages we actually
        # crawled can be judged, so uncrawled targets are reported separately.
        declared: dict[str, set[str]] = {
            p.url: {normalize_url(href) for _, href in p.hreflang} for p in declaring
        }
        non_reciprocal: list[str] = []
        examples: list[str] = []
        for source, targets in declared.items():
            for target in targets:
                if target == source or target not in declared:
                    continue
                if source not in declared[target]:
                    non_reciprocal.append(source)
                    if len(examples) < 3:
                        examples.append("%s names %s, which does not name it back"
                                        % (source, target))
                    break

        if non_reciprocal:
            yield self._finding(
                HIGH, "hreflang", "hreflang reciprocity failures", non_reciprocal,
                "%d pages declare an alternate that does not point back. %s"
                % (len(non_reciprocal), "; ".join(examples)),
                "Google ignores the entire hreflang cluster when the return tags are "
                "missing, so shoppers keep landing on the wrong country's store and "
                "bouncing at the currency.",
            )

        no_x_default = [p.url for p in declaring if not p.hreflang_x_default]
        if no_x_default:
            yield self._finding(
                MED, "hreflang", "Missing x-default", no_x_default,
                "%d pages declare hreflang alternates without an x-default" % len(no_x_default),
                "x-default is what Google serves to every country you did not name. "
                "Without it those visitors get whichever locale Google guesses.",
            )

        no_self = [p.url for p in declaring if not any(
            normalize_url(href) == p.url for _, href in p.hreflang)]
        if no_self:
            yield self._finding(
                MED, "hreflang", "hreflang set missing a self-reference", no_self,
                "%d pages omit themselves from their own hreflang set" % len(no_self),
                "The spec requires a self-referencing entry; without it the cluster is "
                "invalid and gets dropped wholesale.",
            )

        invalid = [p.url for p in declaring if p.hreflang_invalid]
        if invalid:
            codes = sorted({code for p in declaring for code in p.hreflang_invalid})
            yield self._finding(
                MED, "hreflang", "Invalid hreflang language codes", invalid,
                "%d pages use malformed codes: %s" % (len(invalid), ", ".join(codes[:8])),
                "An unparseable code is silently discarded, so that locale never gets "
                "targeted at all.",
            )

    # --- e-commerce ---------------------------------------------------------

    def check_price_consistency(self) -> Iterator[Finding]:
        """The signature check: disagreeing price signals on product pages."""
        mismatched = [p for p in self.pages.values()
                      if p.is_product and not p.price_consistent and p.status == 200]
        if mismatched:
            examples = "; ".join(
                "%s (%s)" % (p.url.rsplit("/", 1)[-1],
                             ", ".join("%s=%s" % kv for kv in p.prices.items()))
                for p in mismatched[:3]
            )
            yield self._finding(
                HIGH, "ecommerce", "Product price signals disagree", [p.url for p in mismatched],
                "%d product pages advertise more than one price. %s"
                % (len(mismatched), examples),
                "Merchant Center reads the Open Graph price and Search reads the JSON-LD "
                "one. When they disagree Google flags a price mismatch, disapproves the "
                "item, and the product stops serving in Shopping until it is fixed.",
            )

        currency = [p.url for p in self.pages.values()
                    if p.is_product and not p.currency_consistent and p.status == 200]
        if currency:
            yield self._finding(
                HIGH, "ecommerce", "Product currency signals disagree",  currency,
                "%d product pages declare more than one currency" % len(currency),
                "A currency mismatch disapproves the feed item exactly like a price "
                "mismatch, and it is easy to miss because both numbers look right.",
            )

    def check_merged_review_pool(self) -> Iterator[Finding]:
        """Distinct products reporting an identical review count.

        The per-page ratio check says "verify"; this one is unambiguous. A
        paginated widget explains a big gap between the schema count and the
        rendered count, but it cannot explain three different products claiming
        exactly the same number of reviews. That only happens when one review
        pool is being served to every product.

        Variants of one product legitimately share a pool, so pages carrying the
        same ``ProductGroup`` id -- or the same product name -- count once.
        """
        by_count: dict[int, list[PageResult]] = defaultdict(list)
        for page in self.pages.values():
            if page.is_product and page.review_schema_count and page.status == 200:
                by_count[page.review_schema_count].append(page)

        for count, pages in sorted(by_count.items(), key=lambda kv: -len(kv[1])):
            # Collapse variants: one entry per distinct product identity.
            distinct: dict[str, PageResult] = {}
            for page in pages:
                identity = (
                    page.product_group_id
                    or (page.product_name or page.h1 or page.url).strip().lower()
                )
                distinct.setdefault(identity, page)

            if len(distinct) < 3:
                continue

            urls = [page.url for page in distinct.values()]
            yield self._finding(
                HIGH, "ecommerce", "Merged review pool across distinct products", urls,
                "%d distinct products share an identical review count of %d"
                % (len(distinct), count),
                "One review pool is being served to every product. Google treats "
                "that as a review-snippet policy violation and can strip star "
                "ratings from the whole domain -- several percent of click-through "
                "on every product listing. Fix it in the review app's grouping "
                "settings, not in the schema.",
            )

    def check_review_schema(self) -> Iterator[Finding]:
        """Product schema claiming far more reviews than the page renders."""
        suspicious = [p for p in self.pages.values() if p.review_suspicious]
        if not suspicious:
            return
        examples = "; ".join("%s: %s" % (p.url.rsplit("/", 1)[-1], p.review_reason)
                             for p in suspicious[:3])
        yield self._finding(
            MED, "ecommerce", "Verify review schema grouping "
                              "(Google review snippet policy risk)",
            [p.url for p in suspicious],
            "%d product pages claim a review count the page does not show. %s"
            % (len(suspicious), examples),
            "This is the fingerprint of a merged review pool -- one set of reviews "
            "reused across every product. Google treats that as a review-snippet policy "
            "violation and can strip the star ratings from the whole domain, which is "
            "usually worth several percent of click-through on every product listing.",
        )

    def check_missing_product_schema(self) -> Iterator[Finding]:
        """Product URLs with no Product markup at all.

        ``ProductGroup`` counts: it is the type Shopify emits for a PDP with
        variants, and Google reads it exactly like ``Product``.
        """
        looks_like_product = [
            p for p in self.pages.values()
            if p.status == 200 and "/products/" in p.url
            and not {"Product", "ProductGroup"} & set(p.schema_types)
        ]
        if looks_like_product:
            yield self._finding(
                MED, "ecommerce", "Product pages without Product schema",
                [p.url for p in looks_like_product],
                "%d /products/ URLs emit no Product structured data" % len(looks_like_product),
                "No Product markup means no price, availability or rating in the search "
                "result, and no eligibility for free Shopping listings.",
            )

    # --- images and headings ------------------------------------------------

    def check_hero_loading(self) -> Iterator[Finding]:
        """LCP images deprioritized by the browser, and their inverse."""
        lazy_heroes = [p.url for p in self.pages.values()
                       if p.hero_lazy and p.status == 200]
        if lazy_heroes:
            yield self._finding(
                HIGH, "performance", "Hero image is lazy-loaded", lazy_heroes,
                "%d pages set loading=\"lazy\" on their largest above-the-fold image"
                % len(lazy_heroes),
                "The LCP element is being deprioritized by the browser: it waits for "
                "layout before it even starts downloading. This is a one-attribute "
                "theme fix (eager plus fetchpriority=\"high\") and typically a "
                "measurable LCP improvement on every product page at once.",
            )

        no_priority = [
            p.url for p in self.pages.values()
            if p.hero_image and not p.hero_lazy and not p.fetchpriority_present
            and p.status == 200
        ]
        if no_priority:
            yield self._finding(
                LOW, "performance", "Hero image without fetchpriority=\"high\"", no_priority,
                "%d pages leave the LCP image at default priority" % len(no_priority),
                "The browser discovers the hero at the same priority as everything "
                "else in the page. Marking it high moves it to the front of the "
                "queue -- a smaller win than removing a lazy attribute, but free.",
            )

        eager = [p for p in self.pages.values()
                 if p.imgs_eager_belowfold_count > 0 and p.status == 200]
        if eager:
            total = sum(p.imgs_eager_belowfold_count for p in eager)
            yield self._finding(
                MED, "performance", "Below-fold images load eagerly",
                [p.url for p in eager],
                "%d below-fold images across %d pages load eagerly" % (total, len(eager)),
                "Every one of them competes with the hero for the same connection "
                "on the first screen, so the image the shopper is actually waiting "
                "for arrives later.",
            )

        no_srcset = [p for p in self.pages.values() if p.imgs_large_no_srcset]
        if no_srcset:
            total = sum(len(p.imgs_large_no_srcset) for p in no_srcset)
            yield self._finding(
                LOW, "images", "Large images served without srcset",
                [p.url for p in no_srcset],
                "%d large images across %d pages have no srcset" % (total, len(no_srcset)),
                "The master-size file is shipped to every viewport, so a phone "
                "downloads the desktop image. Adding srcset usually cuts the "
                "mobile payload by more than half.",
            )

    def check_heading_furniture(self) -> Iterator[Finding]:
        """Pages whose heading tree carries only template UI."""
        unmarked = [p for p in self.pages.values()
                    if p.unmarked_content_sections and p.status == 200]
        if not unmarked:
            return

        examples = "; ".join(
            "%s (%s)" % (p.url.rsplit("/", 1)[-1],
                         ", ".join(repr(h) for h in p.furniture_headings[:2]))
            for p in unmarked[:3]
        )
        yield self._finding(
            MED, "on-page", "Content sections carry no headings", [p.url for p in unmarked],
            "%d pages where the only subheadings are template furniture. %s"
            % (len(unmarked), examples),
            "The heading tree describes the theme rather than the product -- "
            "Description, Warranty and Shipping are styled divs, so nothing tells a "
            "search engine what the page's real sections are. Cheap theme fix, and "
            "it is what makes a page eligible for section-level snippets.",
        )

    def check_anchor_destination_mismatch(self) -> Iterator[Finding]:
        """Anchors promising a destination they do not deliver.

        Sitewide nav where "Sale", "Popular" and a promo banner all point at the
        homepage. Because these live in the template, the finding is aggregated
        per unique (anchor text -> destination) pair rather than per page.
        """
        homepages = {p.url for p in self.pages.values() if p.discovered_by == "start"}
        homepages |= {u for u in self.sitemap_urls if urlsplit(u).path in ("", "/")}
        for page in self.pages.values():
            if urlsplit(page.url).path in ("", "/"):
                homepages.add(page.url)
        if not homepages:
            return

        brand = registrable_host(next(iter(homepages))).split(".")[0].lower()

        pairs: dict[tuple[str, str], set[str]] = defaultdict(set)
        for link in self.links:
            if not link.internal:
                continue
            anchor = link.anchor.strip()
            if not anchor or not self._promises_destination(anchor, brand):
                continue
            target = link.target
            if target in homepages or target.endswith("#") or urlsplit(target).path in ("", "/"):
                pairs[(anchor, "homepage")].add(link.source)

        if not pairs:
            return

        ranked = sorted(pairs.items(), key=lambda kv: -len(kv[1]))
        examples = "; ".join(
            "%r -> homepage on %d pages" % (anchor, len(sources))
            for (anchor, _), sources in ranked[:4]
        )
        affected = sorted({source for sources in pairs.values() for source in sources})

        yield self._finding(
            MED, "links", "Anchor text promises a destination it does not deliver",
            affected,
            "%d distinct anchor texts point at the homepage instead. %s"
            % (len(pairs), examples),
            "A shopper clicking \"Sale\" expects sale products and lands on the "
            "homepage instead -- a dead end for the highest-intent click on the "
            "page. Sitewide, it also wastes the internal link equity those "
            "template links carry, and tells Google nothing about what the "
            "destination is for.",
        )

    def _promises_destination(self, anchor: str, brand: str) -> bool:
        """True when anchor text implies a specific page rather than 'home'."""
        text = anchor.strip().lower()
        if not text or brand and brand in text:
            return False
        # Logo links and home links are supposed to point at the homepage.
        if text in ("home", "homepage", "logo") or text.startswith("back to"):
            return False
        if re.search(r"\d+\s*%|\bsave\b|\bcode\b", text):
            return True
        return any(
            text == keyword or re.search(r"\b%s\b" % re.escape(keyword), text)
            for keyword in self.destination_anchors
        )

    # --- on-page ------------------------------------------------------------

    def check_titles_and_descriptions(self) -> Iterator[Finding]:
        """Missing and badly sized titles and descriptions."""
        rules = (
            (HIGH, "title_issue", "missing", "Missing title tag",
             "Google writes its own title from whatever text it finds. You lose the "
             "keyword and the pitch on the one line every searcher reads."),
            (LOW, "title_issue", "too long", "Title too long for the SERP",
             "The tail gets truncated with an ellipsis, so any keyword or brand at the "
             "end never appears in the result."),
            (LOW, "title_issue", "too short", "Title too short",
             "A short title leaves SERP real estate unused -- free space for a "
             "qualifier that wins the click."),
            (MED, "meta_description_issue", "missing", "Missing meta description",
             "Google falls back to scraped page text, which usually reads like nav "
             "labels and costs click-through on a result you already rank for."),
            (LOW, "meta_description_issue", "too long", "Meta description too long",
             "Truncated before the call to action, so the reason to click never shows."),
        )
        for severity, attribute, value, issue, why in rules:
            urls = [p.url for p in self.indexable if getattr(p, attribute) == value]
            if urls:
                yield self._finding(
                    severity, "on-page", issue, urls,
                    "%d indexable pages" % len(urls), why,
                )

    def check_headings(self) -> Iterator[Finding]:
        """H1 coverage and heading-order problems."""
        missing = [p.url for p in self.indexable if p.h1_count == 0]
        if missing:
            yield self._finding(
                MED, "on-page", "Missing H1", missing,
                "%d indexable pages have no H1" % len(missing),
                "The H1 is the strongest on-page topic signal after the title; a page "
                "without one is guessing at its own subject.",
            )

        multiple = [p.url for p in self.indexable if p.h1_count > 1]
        if multiple:
            yield self._finding(
                LOW, "on-page", "Multiple H1 tags", multiple,
                "%d pages have more than one H1" % len(multiple),
                "Not a ranking penalty, but it usually means the theme is using H1 for "
                "styling, which blurs the page's topic.",
            )

        bad_order = [p.url for p in self.indexable if p.heading_issues]
        if bad_order:
            yield self._finding(
                LOW, "on-page", "Heading order violations", bad_order,
                "%d pages skip a heading level (an H3 before any H2, for example)"
                % len(bad_order),
                "Skipped levels break the outline screen readers and Google both use to "
                "understand which sections belong to which topic.",
            )

    def check_thin_content(self) -> Iterator[Finding]:
        """Indexable pages with very little body copy."""
        thin = [p.url for p in self.indexable if p.thin_content and p.word_count > 0]
        if not thin:
            return
        average = sum(p.word_count for p in self.indexable) // max(1, len(self.indexable))
        yield self._finding(
            MED, "content", "Thin content", thin,
            "%d indexable pages have under %d words (site average: %d)"
            % (len(thin), 250, average),
            "Thin pages rarely rank for anything beyond their exact product name, and in "
            "bulk they drag down how Google rates the whole site's quality.",
        )

    def check_images(self) -> Iterator[Finding]:
        """Alt text gaps and images heavy enough to hurt LCP."""
        no_alt = [p.url for p in self.indexable if p.images_missing_alt > 0]
        if no_alt:
            total = sum(p.images_missing_alt for p in self.indexable)
            yield self._finding(
                LOW, "images", "Images missing alt text", no_alt,
                "%d images across %d pages have no alt attribute" % (total, len(no_alt)),
                "Alt text is how product images rank in Google Images -- a real traffic "
                "channel for physical goods -- and it is an accessibility requirement.",
            )

        oversized = [p.url for p in self.pages.values() if p.images_oversized]
        if oversized:
            worst = max(self.pages.values(), key=lambda p: p.largest_image_kb)
            # Image sizing is sampled per URL template, so say so rather than
            # letting the count read as "these are all of them".
            sized = sum(1 for p in self.pages.values() if p.images_sized and p.status == 200)
            skipped = sum(1 for p in self.pages.values() if not p.images_sized)
            coverage = (
                " -- measured on %d of %d pages; %d sister pages of already-sampled "
                "templates were not size-checked" % (sized, sized + skipped, skipped)
                if skipped else ""
            )
            yield self._finding(
                MED, "images", "Oversized images", oversized,
                "%d pages serve an image over 300 KB (largest: %d KB on %s)%s"
                % (len(oversized), worst.largest_image_kb, worst.url, coverage),
                "The hero image is almost always the Largest Contentful Paint element. "
                "Every 100 KB of it is measurable delay on mobile, and LCP is a ranking "
                "factor as well as a conversion one.",
            )

    def check_social_tags(self) -> Iterator[Finding]:
        """Viewport and social preview coverage."""
        no_viewport = [p.url for p in self.indexable if not p.viewport]
        if no_viewport:
            yield self._finding(
                HIGH, "mobile", "Missing viewport meta tag", no_viewport,
                "%d pages have no viewport declaration" % len(no_viewport),
                "Without it mobile browsers render the desktop layout zoomed out. Google "
                "indexes mobile-first, so this is what it sees.",
            )

        no_og = [p.url for p in self.indexable if "og:image" in p.missing_social]
        if no_og:
            yield self._finding(
                LOW, "social", "Missing og:image", no_og,
                "%d pages have no Open Graph image" % len(no_og),
                "Shares of these URLs render as a bare text link on every social "
                "platform and in most messaging apps, which measurably cuts the clicks.",
            )

    def check_url_hygiene(self) -> Iterator[Finding]:
        """Cosmetic and structural URL problems worth cleaning up."""
        flagged: dict[str, list[str]] = defaultdict(list)
        for page in self.pages.values():
            for flag in page.url_flags:
                flagged[flag].append(page.url)

        messages = {
            "copy-suffix": (MED, "URLs with a -copy suffix",
                            "A '-copy' URL is a duplicated page someone forgot to delete. "
                            "It competes with the original and looks unprofessional in the SERP."),
            "uppercase": (LOW, "Uppercase characters in URLs",
                          "Servers treat /Product and /product as different pages, so the "
                          "same content can get indexed twice."),
            "deep-path": (LOW, "URLs more than four levels deep",
                          "Deep URLs get crawled less often and inherit less link equity "
                          "from the homepage."),
            "query-params-no-canonical": (MED, "Query-string URLs without a canonical",
                                          "Every parameter combination becomes its own "
                                          "indexable page, splitting ranking signals across "
                                          "dozens of near-identical URLs."),
        }
        for flag, (severity, issue, why) in messages.items():
            urls = flagged.get(flag)
            if urls:
                yield self._finding(
                    severity, "urls", issue, urls, "%d URLs affected" % len(urls), why,
                )

    # --- spelling -----------------------------------------------------------

    def check_spelling(self) -> Iterator[Finding]:
        """Typos, split by how much of the customer sees them.

        Title and meta description are what a searcher reads *before* deciding
        to click, so a typo there is judged before the site gets a chance to
        make its case. Body typos are aggregated, because reporting every
        instance of one repeated word is noise, not an audit.
        """
        serp = [p for p in self.indexable if p.spelling_title or p.spelling_meta]
        if serp:
            examples = "; ".join(
                "%s: %s" % (p.url.rsplit("/", 1)[-1],
                            ", ".join(repr(w) for w in (p.spelling_title + p.spelling_meta)[:3]))
                for p in serp[:3]
            )
            yield self._finding(
                MED, "content", "Typos in title or meta description",
                [p.url for p in serp],
                "%d pages have a misspelling in the text shown in search results. %s"
                % (len(serp), examples),
                "This is the copy a searcher reads before they have seen the site at "
                "all. A visible typo in the SERP erodes trust before the click, on a "
                "line you are already paying for in rankings.",
            )

        h1s = [p for p in self.indexable if p.spelling_h1]
        if h1s:
            examples = "; ".join(
                "%s: %s" % (p.url.rsplit("/", 1)[-1], ", ".join(repr(w) for w in p.spelling_h1[:3]))
                for p in h1s[:3]
            )
            yield self._finding(
                MED, "content", "Typos in H1", [p.url for p in h1s],
                "%d pages have a misspelling in their H1. %s" % (len(h1s), examples),
                "The H1 is the first thing on the page and the strongest on-page "
                "topic signal. A typo there reads as carelessness at the exact "
                "moment a new visitor is deciding whether to trust the shop.",
            )

        body = [p for p in self.indexable if p.spelling_body_count]
        if body:
            total = sum(p.spelling_body_count for p in body)
            worst = max(body, key=lambda p: p.spelling_body_count)
            examples = ", ".join(repr(w) for w in worst.spelling_body[:5])
            yield self._finding(
                LOW, "content", "Typos in body content", [p.url for p in body],
                "%d misspellings across %d pages (worst: %d on %s -- %s)"
                % (total, len(body), worst.spelling_body_count,
                   worst.url.rsplit("/", 1)[-1], examples),
                "Individually minor, but in bulk it is the kind of sloppiness that "
                "shows up in how people rate a shop's credibility -- and product "
                "copy is the text doing the selling.",
            )

    # --- assembly -----------------------------------------------------------

    def analysers(self) -> Sequence[Callable[[], Iterator[Finding]]]:
        """Every site-level analyser, in report order."""
        return (
            self.check_broken_pages,
            self.check_noindex,
            self.check_canonicals,
            self.check_price_consistency,
            self.check_merged_review_pool,
            self.check_review_schema,
            self.check_missing_product_schema,
            self.check_duplicate_titles,
            self.check_duplicate_descriptions,
            self.check_duplicate_content,
            self.check_broken_internal_links,
            self.check_links_to_redirects,
            self.check_redirects,
            self.check_orphans_and_ghosts,
            self.check_hreflang,
            self.check_hero_loading,
            self.check_anchor_destination_mismatch,
            self.check_titles_and_descriptions,
            self.check_headings,
            self.check_heading_furniture,
            self.check_spelling,
            self.check_thin_content,
            self.check_images,
            self.check_social_tags,
            self.check_url_hygiene,
        )

    def run(self) -> list[Finding]:
        """Run every analyser and return findings sorted by severity, then reach."""
        findings: list[Finding] = []
        for analyser in self.analysers():
            findings.extend(analyser())
        findings.sort(key=lambda f: f.sort_key())
        return findings

    def stats(self, findings: Sequence[Finding] | None = None) -> SiteStats:
        """Headline numbers plus the share of pages hit by each issue category."""
        findings = self.run() if findings is None else findings
        pages = list(self.pages.values())
        total = len(pages)

        status_counts = Counter(
            "%dxx" % (p.status // 100) if p.status else "failed" for p in pages
        )

        by_category: dict[str, set[str]] = defaultdict(set)
        for finding in findings:
            by_category[finding.category].update(finding.urls)

        pages_with_issues = {c: len(urls) for c, urls in sorted(by_category.items())}
        issue_rate = {
            category: "%.0f%%" % (100 * count / total) if total else "0%"
            for category, count in pages_with_issues.items()
        }

        return SiteStats(
            total_urls=total,
            indexable=len(self.indexable),
            non_indexable=total - len(self.indexable),
            status_counts=dict(sorted(status_counts.items())),
            products=sum(1 for p in pages if p.is_product),
            orphans=sum(1 for p in pages
                        if p.url in self.sitemap_urls and p.inlinks == 0
                        and p.status == 200 and p.discovered_by != "start"),
            ghosts=sum(1 for p in pages
                       if self.sitemap_urls and p.url not in self.sitemap_urls
                       and p.indexable and p.inlinks > 0),
            avg_word_count=(sum(p.word_count for p in self.indexable) //
                            max(1, len(self.indexable))),
            pages_with_issues=pages_with_issues,
            issue_rate=issue_rate,
        )


# --- anchor text report -----------------------------------------------------


@dataclass
class AnchorRow:
    """One (anchor text -> target) pair with its usage count."""

    anchor: str
    target: str
    count: int
    sources: int


def anchor_report(links: Sequence[OutLink], limit: int = 1000) -> list[AnchorRow]:
    """Aggregate internal anchor text by (anchor, target), most used first."""
    counts: Counter[tuple[str, str]] = Counter()
    sources: dict[tuple[str, str], set[str]] = defaultdict(set)

    for link in links:
        if not link.internal:
            continue
        key = (link.anchor.strip() or "(no anchor text)", link.target)
        counts[key] += 1
        sources[key].add(link.source)

    rows = [
        AnchorRow(anchor=anchor, target=target, count=count, sources=len(sources[(anchor, target)]))
        for (anchor, target), count in counts.most_common(limit)
    ]
    return rows
