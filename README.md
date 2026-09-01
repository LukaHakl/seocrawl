# seocrawl

A mini Screaming Frog for small/mid e-commerce sites — built for Shopify, sized
for up to ~10,000 URLs. Crawls politely, audits every page, and writes an xlsx
(plus an optional client-ready HTML report) where every finding says what it
costs in plain money terms.

## Install

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows; use bin/activate on macOS/Linux
pip install -e .
```

Python 3.11+.

There is a `[js]` extra declaring Playwright. **It is deliberately not wired
up** — nothing in seocrawl imports it, and installing it changes nothing. It
stays declared because the day a target stops server-rendering its prices, the
dependency decision is already made; but Shopify serves prices and schema in
the HTML, so rendering would cost seconds per page to discover exactly what the
raw response already said. Adding it before something needs it would be the
expensive kind of thoroughness.

## The UI

```bash
seocrawl ui
```

Opens a local page in your browser: enter a URL, watch pages tick by with a
stop button, and get the findings plus a download link for the spreadsheet when
it finishes. Reports download through the browser rather than opening in a
local app, so it works on a machine with no spreadsheet software installed; the
HTML report opens in a new tab. The Keyword map tab takes the Planner export and joins it against
any report you have already produced. Everything runs on your machine and
nothing is uploaded; the server binds to localhost only.

## Three commands to start with

Check the tool runs and the site is reachable — 200 URLs, default 0.7s delay:

```bash
seocrawl https://example.com --max-urls 200
```

> **Treat a capped run as a smoke test, not an audit.** Several checks are
> site-level: the merged-review fingerprint compares review counts *across the
> whole catalogue*, near-duplicate detection compares every page against every
> other, and the anchor→destination check aggregates by unique anchor text. Cap
> the crawl and those checks measure the sample instead of the site — a review
> pool spread across 400 products looks clean in the first 200, and 200 pages
> of a 2,000-page catalogue can manufacture a duplicate cluster that is really
> just one collection template. The findings that come back are not wrong so
> much as unfalsifiable. Never report from a capped crawl.

Full audit with a client-ready HTML report and PageSpeed on the ten most-linked
pages:

```bash
seocrawl https://example.com --html --psi --psi-key YOUR_KEY
```

Re-audit and compare against last month, to see what got fixed and what broke:

```bash
seocrawl https://example.com --html --diff audit_example-com_2026-07-26.xlsx
```

Output lands in the working directory as `audit_<domain>_<date>.xlsx`.

## What each sheet means

| Sheet | What it is |
|---|---|
| **Summary** | Read this first. Crawl stats, response-code spread, share of pages hit by each issue category, and the top ten findings with their business impact. |
| **Pages** | One row per URL, ~50 columns. Frozen header and filters on every column. **Red cells are HIGH-severity problems** (4xx/5xx, price mismatch, cross-domain canonical, noindex on a linked page, missing viewport); **amber cells are MED** (redirect chains, thin content, oversized images, orphans, review-schema flags). |
| **Findings** | Every site-level issue, severity-sorted, with the affected URL count, a detail line and a "why it matters" written for a client rather than a developer. |
| **Anchors** | Internal anchor text aggregated by (anchor → target), most-used first. Empty anchors are highlighted. |
| **PSI** | Only with `--psi`. Lighthouse lab metrics plus CrUX field data, failing Core Web Vitals highlighted. |
| **Diff** | Only with `--diff`. New issues, issues that got worse or better, fixed issues, and new/removed URLs. |
| **Keyword Map** | Only with `seocrawl keywords`. Every keyword with its status (GAP/CANNIBAL/WEAK/OWNED), owning URL, page type, score and any inverted-targeting flag. Sorted GAP first by volume. |
| **Gaps** | Unowned demand, clustered by shared phrase, one row per cluster with the page you would build. **Read this sheet first.** |
| **Cannibalization** | Contested keywords with their competing pages side by side. |

## How it crawls

- **Discovery merges two sources.** robots.txt `Sitemap:` lines first (falling back
  to `/sitemap.xml`), recursing sitemap indexes fully — merged with a BFS link
  crawl from the start URL. That is what surfaces both **orphans** (in the
  sitemap, nothing links to them) and **ghosts** (linked, missing from the
  sitemap).
- **Link-discovered URLs get crawl priority.** On a run capped by `--max-urls`,
  the budget follows the site's own navigation first; sitemap-only URLs are
  crawled with whatever is left, shallowest first. Without this a capped crawl
  spends its whole budget on whatever the sitemap happens to list first.
- **Politeness is not optional.** robots.txt is obeyed (including its
  `Crawl-delay` when it is stricter than yours), a global rate limiter spaces
  requests no matter how many workers run, 429/5xx get exponential backoff with
  `Retry-After`, and repeated 403s stop the crawl with an explanation instead of
  hammering a WAF. The User-Agent identifies the tool and carries a contact
  address.
- **Resume is automatic.** Every page is written to a sqlite cache as it is
  crawled, so an interrupted 8k-URL run picks up where it stopped. Use
  `--recrawl` to force a fresh crawl.
- **Redirects are followed by hand** so every hop is recorded, not just the count.

## What each check means

Beyond the standard crawl checks (titles, metas, canonicals, redirects,
duplicates, orphans, hreflang, images, structured data), seocrawl carries a few
that came out of real audits:

- **Price consistency** - compares every price signal on a product page and
  reports a HIGH when two of them disagree, because that is what disapproves a
  Merchant Center item. Details below.
- **Review schema sanity** - Product schema claiming far more reviews than the
  page renders, reported as MED with "verify" wording. Details below.
- **Merged review pool** - three or more *distinct* products reporting an
  identical review count. Unlike the ratio check above this is unambiguous, so
  it is HIGH: variants of one product are collapsed by ProductGroup id first.
- **Hero image lazy-loading** - images are classified by role (hero, gallery,
  thumbnail) from their `sizes`, `srcset` and DOM position *before* being
  judged, because `loading="lazy"` is correct on a thumbnail and a defect on the
  LCP element. A lazy hero is HIGH; a hero without `fetchpriority="high"` is LOW;
  below-fold images loading eagerly are MED.
- **Heading furniture** - pages whose only H2s are template UI ("Featured
  Collections", "You may also like") while the real sections are styled divs.
  MED, and usually a cheap theme fix.
- **Anchor / destination mismatch** - sitewide anchors like "Sale" or "Popular"
  that point at the homepage instead of a real destination. Aggregated per
  (anchor -> destination) pair, since these live in the template. MED.
- **Typos** - title, meta description, H1 and body copy. SERP-visible text and
  H1s are MED; body typos are LOW and aggregated. Details below.

## The two e-commerce checks worth knowing about

**Price consistency.** Shopping feeds read the Open Graph / `product:` price
while Search reads the JSON-LD one. When they disagree, Merchant Center flags a
price mismatch and disapproves the item. seocrawl compares
`og:price:amount`, `og:price:standard_amount`, `product:price:amount`,
microdata `itemprop="price"` and JSON-LD `offers.price` — traversing Shopify's
`ProductGroup → hasVariant[] → offers.priceSpecification[]` shape, and
correctly ignoring `StrikethroughPrice` (compare-at) values and legitimate
per-variant price ranges, so only genuine disagreements are reported as HIGH.

**Review schema sanity.** If Product schema claims far more reviews than the
page actually renders, that is the fingerprint of a merged review pool — one set
of reviews shared across every variant or the whole catalogue — which risks
Google's review-snippet policy and can cost star ratings sitewide. Reported as
MED, deliberately: a heavily paginated review widget can look the same, so the
finding says *verify*, and the Pages sheet gives you the schema count and the
on-page count side by side to judge it.

## The typo check

Spellchecking a shop is mostly a false-positive problem, so most of the work is
in *not* reporting things:

- **Engine**: `spellcheck_engine = "auto"` prefers LanguageTool (grammar as well
  as spelling) and falls back to pyspellchecker when Java is unavailable, saying
  so rather than silently checking nothing. Install the grammar extra with
  `pip install -e ".[grammar]"`. Force either with `--spellcheck-engine`.
- **A whitelist the site builds itself**: before judging anything, seocrawl
  collects every capitalized mid-sentence word across the crawl. Anything seen
  on 3+ pages (`brand_term_min_pages`) is a brand or product name --
  "Leuchtturm1917", "Ritza", "Seidel" -- not a typo. The domain name and its
  parts are whitelisted automatically, and `spellcheck_whitelist` adds your own.
  The pass only ever *removes* candidates, so a real typo cannot be introduced.
- **Locale**: the bundled dictionary is US English, so British and Canadian
  spellings ("colour", "moulds", "organise", "centre") are normalized before
  being judged rather than reported as errors.
- **Punctuation and compounds**: possessives, contractions and hyphenated
  compounds ("men's", "I've", "saddle-stitched") are split and judged by their
  parts, so only a genuinely misspelled part is reported.
- **Customer reviews are excluded**: review-widget output (Judge.me, Loox,
  Yotpo, Okendo, Stamped) is stripped before checking. A typo in a customer's
  review is not the shop's typo and cannot be fixed by them; on a site with
  thousands of reviews, leaving them in buries the handful of real errors.
- **Language gate**: pages whose `html lang` does not match `lang` are skipped.

## Keyword mapping

A keyword list on its own is a wish; joined against a crawl it becomes a map.
The workflow is: **export from Keyword Planner -> `seocrawl keywords` -> read the
Gaps sheet first.** Point the subcommand at the export and at an audit workbook
and it cleans the file the way you would by hand (filters reported as a funnel,
so a surprising final count is diagnosable), then joins every keyword against
every indexable page to answer four questions: which URL **owns** the term,
which terms have **no owner** at all, where two pages **compete** for one term,
and where the targeting is **inverted** -- a head term aimed at a single product,
or a five-word specific aimed at a category. Gaps are clustered by shared
two-token phrase and each cluster carries the page you would build to fill it.

```bash
seocrawl keywords --gkp planner_export.csv --crawl audit_example-com_2026-08-26.xlsx     --brand acme --contains wallet --not-contains code --geo-note "US"
```

The three new sheets are appended to the audit workbook (`--standalone` writes a
separate file instead). The export itself is handled defensively: GKP ships
UTF-16 tab-delimited files with a metadata preamble, localized headers, bucketed
volumes ("1 tis. - 10 tis.", "1K - 10K") and locale-specific number formats.
All of that is detected rather than assumed, and a file that cannot be read at
all fails with the headers it *did* find.

Two things worth knowing about the scoring. Each page's target tokens come from
its URL slug (weight 3), title and H1 (weight 2) -- what a page declares it is
about outranks its body copy. And each keyword token is weighted by how much it
distinguishes pages: on a leather-goods site every page says "leather" and
"wallet", so those tokens separate nothing and the distinguishing token
("minimalist", "bifold") decides the match. Without that, a search for
"minimalist leather wallet" matches half the catalogue and the gap list comes
back empty. Pass `--no-idf` to weigh every token equally.

Every run prints the caveat, because it is always true:

> GKP volumes are ranges for non-spending accounts and geo-dependent — treat as
> relative sizes, not truth. GSC data supersedes this file the day you get access.

## Configuration

Every flag can also live in a `seocrawl.toml` (read from the working directory,
or passed with `--config`). CLI flags win over the file, the file wins over the
defaults. A commented example ships in the repo, pre-loaded with the Shopify
paths worth excluding (cart, account, checkout, customer auth, faceted
collections). Use TOML *literal* strings (single quotes) for regex patterns so
backslashes need no escaping.

```toml
[seocrawl]
max_urls = 5000
delay = 1.0
workers = 4
exclude = ['/cart', '\?filter\.']
psi_top = 10

spellcheck_engine = "auto"       # or "languagetool" / "pyspellchecker" / "off"
lang = "en"
spellcheck_whitelist = ["Ritza", "Seidel"]
furniture_headings = []          # empty = built-in list
destination_anchors = []         # empty = built-in list
```

`--psi-key` also falls back to the `PSI_API_KEY` environment variable.

Run `seocrawl --help` for the full flag list.

## Tests

```bash
pytest
```

509 tests, covering every check against HTML fixtures and the crawler against
throwaway localhost sites with a redirect chain, a 404, a noindex page, an
orphan, a ghost, a robots.txt disallow rule, and a four-page site that exercises
the two-pass spelling whitelist end to end. The keyword fixtures include the
same eight keywords exported twice -- UTF-16/tab/Slovenian and UTF-8/comma/
English -- which must parse to byte-identical normalized rows.
