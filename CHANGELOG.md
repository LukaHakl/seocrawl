# Changelog

Written after the fact, from the state of the code and the order the checks
were built in. Versions describe capability, not release dates — this was one
person's tool before it was a package.

## 1.3.0 — the local UI

- `seocrawl ui`: a localhost web interface. Enter a URL, watch pages tick by,
  stop mid-crawl and still get a report. Built because the audience for a crawl
  report is frequently not the person who can operate a CLI.
- Everything stays on the machine; the server binds to localhost and nothing is
  uploaded.
- Single-file HTML report (`--html`) alongside the xlsx, for handing to someone
  who has no spreadsheet software.
- PageSpeed Insights integration (`--psi`) on the most-linked pages.
- Month-over-month diff (`--diff previous.xlsx`) so a re-audit shows what was
  fixed and what regressed.

## 1.2.0 — keyword mapping

- `seocrawl keywords --gkp planner_export.csv --crawl audit.xlsx`: joins a
  Google Keyword Planner export against a finished crawl to produce a keyword
  map — which URL owns which keyword, which keywords nothing targets, and where
  two pages compete for the same term.
- The Planner export parser handles what GKP actually emits rather than what it
  documents: UTF-16 with tab delimiters, metadata preamble rows before the
  header, localised headers, and bucketed volume ranges like `1 tis. – 10 tis.`
  that non-spending accounts get instead of numbers.

## 1.1.0 — the checks Screaming Frog does not have

This is where the tool stopped being a worse Screaming Frog and started being a
different one.

- **Spellcheck** across titles, metas, H1s and main content, with the site's own
  brand terms whitelisted automatically — a shop is not misspelling its own
  name, and that is the single largest source of false positives.
- **Image and LCP classification**: which image is the hero, whether it is
  lazy-loaded when it should not be, whether `srcset` is missing.
- **Heading furniture detection** — separating real headings from template
  boilerplate ("You may also like") so heading structure means something.
- **Anchor text vs destination mismatch**: links promising a specific
  destination that land on the homepage instead.
- **Site-level merged-review fingerprint**: identical review counts repeating
  across distinct products. A single product showing 19 of 1,417 reviews is
  ambiguous — pagination looks the same as pooling. The same count appearing on
  forty different products is not ambiguous. This is what upgrades the 1.0
  review check from a hedge to an assertion, and it is why capping a crawl
  invalidates it.

## 1.0.0 — the audit core

- Sitemap-first crawl, polite by default (0.7s delay, robots.txt respected).
- Indexation, titles, meta descriptions, H1s, canonicals, redirects, hreflang.
- JSON-LD extraction and validation.
- **Price consistency**: `og:price` against product schema against the
  displayed price. Two calibrations are baked in and should not be "fixed":
  `StrikethroughPrice` is compare-at markup, not a mismatch, and Shopify's
  internal JS price variables are cents-denominated, so a JS var reading `3800`
  agrees with an `og:price:amount` of `38.00`. Read the meta, not the variable.
- **Review schema sanity** with a configurable `ratio_threshold`, reporting the
  schema count and the on-page count side by side, and reporting MED with
  "verify" wording rather than asserting a violation. That hedge is deliberate.
- xlsx output with Summary, Pages and Findings sheets, every finding carrying a
  severity and a plain-language line about what it costs.
