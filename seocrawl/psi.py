"""PageSpeed Insights integration.

Runs Google's PSI API against the pages that actually earn money -- the ones
with the most internal inlinks -- and turns failed Core Web Vitals thresholds
into findings alongside everything else in the audit.

Lab data (Lighthouse) is always present; field data (CrUX) only exists for URLs
with enough real Chrome traffic, so it is reported when available and never
required.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterator, Sequence

import requests

from .crawler import PageResult
from .findings import HIGH, MED, Finding

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

#: Google's "good" thresholds for the Core Web Vitals, in the units we store.
LCP_GOOD_MS = 2500.0
CLS_GOOD = 0.1
INP_GOOD_MS = 200.0
TBT_GOOD_MS = 200.0
FCP_GOOD_MS = 1800.0
PERFORMANCE_GOOD = 0.9

#: Lighthouse audit id -> field name on :class:`PsiResult`.
_LAB_AUDITS = {
    "largest-contentful-paint": "lcp_ms",
    "cumulative-layout-shift": "cls",
    "total-blocking-time": "tbt_ms",
    "first-contentful-paint": "fcp_ms",
    "speed-index": "speed_index_ms",
    "interactive": "tti_ms",
}

#: CrUX metric key -> (field name, divisor to reach our unit).
_FIELD_METRICS = {
    "LARGEST_CONTENTFUL_PAINT_MS": ("field_lcp_ms", 1.0),
    "CUMULATIVE_LAYOUT_SHIFT_SCORE": ("field_cls", 100.0),
    "INTERACTION_TO_NEXT_PAINT": ("field_inp_ms", 1.0),
    "FIRST_CONTENTFUL_PAINT_MS": ("field_fcp_ms", 1.0),
}


@dataclass
class PsiResult:
    """One PageSpeed Insights run for one URL."""

    url: str
    strategy: str = "mobile"
    inlinks: int = 0

    # Lighthouse lab data
    performance_score: float | None = None
    lcp_ms: float | None = None
    cls: float | None = None
    tbt_ms: float | None = None
    fcp_ms: float | None = None
    speed_index_ms: float | None = None
    tti_ms: float | None = None

    # CrUX field data (real users, 28-day rolling window)
    field_lcp_ms: float | None = None
    field_cls: float | None = None
    field_inp_ms: float | None = None
    field_fcp_ms: float | None = None
    field_overall: str | None = None       # FAST / AVERAGE / SLOW
    has_field_data: bool = False

    error: str | None = None

    @property
    def score_pct(self) -> int | None:
        """Performance score as the 0-100 number the PSI UI shows."""
        return None if self.performance_score is None else round(self.performance_score * 100)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _audit_value(payload: dict[str, Any], audit_id: str) -> float | None:
    """Pull one Lighthouse audit's ``numericValue``, tolerating absence."""
    audit = (payload.get("lighthouseResult", {}).get("audits", {}) or {}).get(audit_id)
    if not isinstance(audit, dict):
        return None
    value = audit.get("numericValue")
    return float(value) if isinstance(value, (int, float)) else None


def parse_psi_response(payload: dict[str, Any], url: str, strategy: str = "mobile") -> PsiResult:
    """Turn a raw PSI API payload into a :class:`PsiResult`.

    Kept separate from the HTTP call so the parsing is testable against a
    recorded payload without touching the network.
    """
    result = PsiResult(url=url, strategy=strategy)

    categories = payload.get("lighthouseResult", {}).get("categories", {}) or {}
    score = (categories.get("performance") or {}).get("score")
    if isinstance(score, (int, float)):
        result.performance_score = float(score)

    for audit_id, attribute in _LAB_AUDITS.items():
        setattr(result, attribute, _audit_value(payload, audit_id))

    experience = payload.get("loadingExperience") or {}
    metrics = experience.get("metrics") or {}
    for key, (attribute, divisor) in _FIELD_METRICS.items():
        metric = metrics.get(key)
        if isinstance(metric, dict) and isinstance(metric.get("percentile"), (int, float)):
            setattr(result, attribute, float(metric["percentile"]) / divisor)
            result.has_field_data = True
    if isinstance(experience.get("overall_category"), str):
        result.field_overall = experience["overall_category"]

    return result


class PsiClient:
    """Rate-limited PageSpeed Insights client.

    PSI is slow (10-30s per URL) and quota-limited, so calls are strictly
    serialized with a configurable gap and a transient failure is retried once.
    """

    def __init__(
        self,
        api_key: str | None,
        *,
        strategy: str = "mobile",
        delay: float = 1.5,
        timeout: float = 90.0,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key
        self.strategy = strategy
        self.delay = delay
        self.timeout = timeout
        self.session = session or requests.Session()
        self._last_call = 0.0

    def _wait(self) -> None:
        """Keep at least ``delay`` seconds between API calls."""
        elapsed = time.monotonic() - self._last_call
        if self._last_call and elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_call = time.monotonic()

    def analyze(self, url: str) -> PsiResult:
        """Run PSI for one URL, returning a result whose ``error`` is set on failure."""
        params = {"url": url, "strategy": self.strategy, "category": "performance"}
        if self.api_key:
            params["key"] = self.api_key

        for attempt in range(2):
            self._wait()
            try:
                response = self.session.get(PSI_ENDPOINT, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == 0:
                    continue
                return PsiResult(url=url, strategy=self.strategy, error=type(exc).__name__)

            if response.status_code == 200:
                try:
                    return parse_psi_response(response.json(), url, self.strategy)
                except ValueError:
                    return PsiResult(url=url, strategy=self.strategy,
                                     error="unparseable PSI response")

            if response.status_code in (429, 500, 503) and attempt == 0:
                time.sleep(5)
                continue

            message = "HTTP %d" % response.status_code
            try:
                detail = response.json().get("error", {}).get("message")
                if detail:
                    message = "%s: %s" % (message, detail[:200])
            except ValueError:
                pass
            return PsiResult(url=url, strategy=self.strategy, error=message)

        return PsiResult(url=url, strategy=self.strategy, error="PSI request failed")


def select_urls(pages: Sequence[PageResult], top: int = 10) -> list[PageResult]:
    """The money pages: indexable HTML ranked by internal inlink count.

    Internal linking is the site's own vote on what matters, so it is a better
    proxy for commercial importance than crawl order or URL depth.
    """
    candidates = [p for p in pages if p.indexable and p.status == 200]
    candidates.sort(key=lambda p: (-p.inlinks, p.path_depth, p.url))
    return candidates[:top]


def run_psi(
    pages: Sequence[PageResult],
    api_key: str | None,
    *,
    top: int = 10,
    strategy: str = "mobile",
    delay: float = 1.5,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> list[PsiResult]:
    """Run PSI over the top ``top`` pages and return one result each."""
    targets = select_urls(pages, top)
    client = PsiClient(api_key, strategy=strategy, delay=delay)
    notify = on_progress or (lambda url, done, total: None)

    results: list[PsiResult] = []
    for index, page in enumerate(targets, start=1):
        result = client.analyze(page.url)
        result.inlinks = page.inlinks
        results.append(result)
        notify(page.url, index, len(targets))
    return results


def psi_findings(results: Sequence[PsiResult]) -> list[Finding]:
    """Turn failed Core Web Vitals thresholds into audit findings.

    Field data wins when present: CrUX is what Google actually ranks on, and a
    lab score can look fine on a page real users find slow.
    """
    findings: list[Finding] = []
    measured = [r for r in results if r.error is None]
    if not measured:
        return findings

    def add(severity: str, issue: str, urls: list[str], detail: str, why: str) -> None:
        if urls:
            findings.append(Finding(severity, "performance", issue, urls, detail, why))

    def slow(result: PsiResult, lab_attr: str, field_attr: str, limit: float) -> bool:
        value = getattr(result, field_attr) or getattr(result, lab_attr)
        return value is not None and value > limit

    lcp = [r for r in measured if slow(r, "lcp_ms", "field_lcp_ms", LCP_GOOD_MS)]
    if lcp:
        worst = max(lcp, key=lambda r: r.field_lcp_ms or r.lcp_ms or 0)
        add(HIGH, "LCP above 2.5s", [r.url for r in lcp],
            "%d of %d tested pages exceed the LCP threshold (worst: %.1fs on %s)"
            % (len(lcp), len(measured), (worst.field_lcp_ms or worst.lcp_ms) / 1000, worst.url),
            "Largest Contentful Paint is a confirmed ranking factor and the metric "
            "shoppers feel as 'this shop is slow'. On mobile commerce every extra "
            "second past 2.5s measurably cuts conversions.")

    cls = [r for r in measured if slow(r, "cls", "field_cls", CLS_GOOD)]
    if cls:
        add(MED, "CLS above 0.1", [r.url for r in cls],
            "%d of %d tested pages shift layout past the threshold" % (len(cls), len(measured)),
            "Layout shift is what makes a shopper tap the wrong button as the page "
            "settles -- it fails Core Web Vitals and costs add-to-cart clicks.")

    inp = [r for r in measured if r.field_inp_ms and r.field_inp_ms > INP_GOOD_MS]
    if inp:
        add(MED, "INP above 200ms (field data)", [r.url for r in inp],
            "%d pages respond slowly to real user interaction" % len(inp),
            "Interaction to Next Paint replaced FID as a Core Web Vital. Slow response "
            "on variant pickers and add-to-cart is exactly where it hurts revenue.")

    tbt = [r for r in measured if r.tbt_ms and r.tbt_ms > TBT_GOOD_MS]
    if tbt:
        worst = max(tbt, key=lambda r: r.tbt_ms or 0)
        add(MED, "Total Blocking Time above 200ms", [r.url for r in tbt],
            "%d pages block the main thread too long (worst: %.0fms on %s)"
            % (len(tbt), worst.tbt_ms, worst.url),
            "High TBT is almost always third-party apps -- review widgets, chat, "
            "upsell scripts. It is the cheapest performance win on most Shopify "
            "stores because it is a deletion, not a rebuild.")

    poor = [r for r in measured
            if r.performance_score is not None and r.performance_score < PERFORMANCE_GOOD]
    if poor:
        worst = min(poor, key=lambda r: r.performance_score or 1)
        add(MED, "Low PageSpeed performance score", [r.url for r in poor],
            "%d of %d tested pages score under %d (worst: %d on %s)"
            % (len(poor), len(measured), round(PERFORMANCE_GOOD * 100),
               worst.score_pct, worst.url),
            "The score itself does not rank, but it tracks the metrics that do and it "
            "is the number a client will check after the audit.")

    failing_field = [r for r in measured if r.field_overall == "SLOW"]
    if failing_field:
        add(HIGH, "Failing Core Web Vitals with real users", [r.url for r in failing_field],
            "%d pages are rated SLOW in Chrome field data" % len(failing_field),
            "This is measured from real Chrome visitors, not a lab simulation -- it is "
            "the data Google uses, and it is already affecting these rankings.")

    return findings
