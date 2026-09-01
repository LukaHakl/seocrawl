"""Report generation: the xlsx workbook, a client-ready HTML report, and diffs."""

from __future__ import annotations

import datetime as dt
import html
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .crawler import PageResult
from .findings import HIGH, LOW, MED, SEVERITY_ORDER, AnchorRow, Finding, SiteStats
from .psi import PsiResult
from .urls import registrable_host

# --- styling ----------------------------------------------------------------

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
TITLE_FONT = Font(bold=True, size=14, color="1F3864")
SECTION_FONT = Font(bold=True, size=11, color="1F3864")

FILL_HIGH = PatternFill("solid", fgColor="F8CBAD")   # red-orange
FILL_MED = PatternFill("solid", fgColor="FFE699")    # amber
FILL_LOW = PatternFill("solid", fgColor="E2EFDA")    # pale green
FILL_BY_SEVERITY = {HIGH: FILL_HIGH, MED: FILL_MED, LOW: FILL_LOW}

FONT_HIGH = Font(color="9C0006", bold=True)
THIN_BORDER = Border(bottom=Side(style="thin", color="D9D9D9"))
WRAP = Alignment(vertical="top", wrap_text=True)
TOP = Alignment(vertical="top")


def _join(value: Any) -> Any:
    """Flatten lists/dicts into a single cell-safe string."""
    if isinstance(value, dict):
        return ", ".join("%s=%s" % (k, v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        parts = [", ".join(str(i) for i in v) if isinstance(v, (list, tuple)) else str(v)
                 for v in value]
        return " | ".join(parts)
    return value


def _write_header(sheet: Worksheet, headers: Sequence[str], widths: Sequence[int]) -> None:
    """Write a styled header row, freeze it, and add column filters."""
    for index, (label, width) in enumerate(zip(headers, widths), start=1):
        cell = sheet.cell(row=1, column=index, value=label)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", horizontal="left")
        sheet.column_dimensions[get_column_letter(index)].width = width

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = "A1:%s1" % get_column_letter(len(headers))
    sheet.row_dimensions[1].height = 22


# --- Pages sheet ------------------------------------------------------------

#: (header, PageResult attribute, column width). Order is the sheet order.
PAGE_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("URL", "url", 52),
    ("Status", "status", 8),
    ("Indexable", "_indexable", 10),
    ("Depth", "depth", 7),
    ("Inlinks", "inlinks", 8),
    ("In sitemap", "in_sitemap", 11),
    ("Title", "title", 42),
    ("Title len", "title_length", 9),
    ("Title issue", "title_issue", 12),
    ("Meta description", "meta_description", 44),
    ("Desc len", "meta_description_length", 9),
    ("Desc issue", "meta_description_issue", 12),
    ("H1", "h1", 34),
    ("H1 count", "h1_count", 9),
    ("H2 count", "h2_count", 9),
    ("Furniture headings", "furniture_headings", 32),
    ("Content headings", "content_headings", 32),
    ("Unmarked sections", "unmarked_content_sections", 16),
    ("Words", "word_count", 8),
    ("Thin", "thin_content", 7),
    ("Canonical", "canonical", 46),
    ("Canonical state", "canonical_state", 15),
    ("Noindex", "noindex", 9),
    ("X-Robots-Tag", "x_robots_tag", 16),
    ("Redirect hops", "redirect_hops", 13),
    ("Redirect chain", "redirect_chain", 46),
    ("Final URL", "final_url", 46),
    ("Internal links", "internal_links", 13),
    ("External links", "external_links", 13),
    ("Nofollow links", "nofollow_links", 13),
    ("Images", "image_count", 8),
    ("Images no alt", "images_missing_alt", 13),
    ("Oversized images", "images_oversized", 40),
    ("Largest image KB", "largest_image_kb", 15),
    ("Images sized", "images_sized", 12),
    ("Hero image", "hero_image", 40),
    ("Hero lazy", "hero_lazy", 10),
    ("fetchpriority", "fetchpriority_present", 13),
    ("Imgs lazy", "imgs_lazy_count", 10),
    ("Imgs eager below fold", "imgs_eager_belowfold_count", 20),
    ("Large imgs no srcset", "imgs_large_no_srcset", 34),
    ("Schema types", "schema_types", 30),
    ("Product", "is_product", 9),
    ("Prices", "prices", 40),
    ("Price consistent", "price_consistent", 15),
    ("Availability", "availability", 14),
    ("Product group", "product_group_id", 22),
    ("Rating", "rating_value", 8),
    ("Reviews (schema)", "review_schema_count", 15),
    ("Reviews (on page)", "review_onpage_count", 15),
    ("Review flag", "review_suspicious", 11),
    ("hreflang", "hreflang", 40),
    ("URL flags", "url_flags", 26),
    ("Typos (title)", "spelling_title", 22),
    ("Typos (meta)", "spelling_meta", 22),
    ("Typos (H1)", "spelling_h1", 22),
    ("Typos (body)", "spelling_body", 34),
    ("Typos (body count)", "spelling_body_count", 17),
    ("Viewport", "viewport", 20),
    ("og:title", "og_title", 30),
    ("og:image", "og_image", 30),
    ("twitter:card", "twitter_card", 14),
    ("Content type", "content_type", 16),
    ("Size (KB)", "_size_kb", 10),
    ("Response ms", "response_ms", 11),
    ("Error", "error", 24),
)

#: Cells painted red when the predicate says this value is a HIGH problem.
_HIGH_CELL_RULES = {
    "status": lambda v, p: v is None or v >= 400,
    "_indexable": lambda v, p: v == "no",
    "title_issue": lambda v, p: v == "missing",
    "canonical_state": lambda v, p: v == "cross-domain",
    "noindex": lambda v, p: bool(v) and p.inlinks > 0,
    "price_consistent": lambda v, p: p.is_product and not v,
    "error": lambda v, p: bool(v),
    "viewport": lambda v, p: not v and p.status == 200,
    "hero_lazy": lambda v, p: bool(v),
}

#: Cells painted amber for MED problems.
_MED_CELL_RULES = {
    "redirect_hops": lambda v, p: (v or 0) > 1,
    "review_suspicious": lambda v, p: bool(v),
    "thin_content": lambda v, p: bool(v),
    "images_oversized": lambda v, p: bool(v),
    "h1_count": lambda v, p: v == 0 and p.status == 200,
    "meta_description_issue": lambda v, p: v == "missing",
    "url_flags": lambda v, p: bool(v),
    "inlinks": lambda v, p: v == 0 and p.in_sitemap and p.status == 200,
    "imgs_eager_belowfold_count": lambda v, p: (v or 0) > 0,
    "unmarked_content_sections": lambda v, p: bool(v),
    "spelling_title": lambda v, p: bool(v),
    "spelling_meta": lambda v, p: bool(v),
    "spelling_h1": lambda v, p: bool(v),
}


def _page_cell_value(page: PageResult, attribute: str) -> Any:
    """Resolve one column's value, including the derived pseudo-columns."""
    if attribute == "_indexable":
        return "yes" if page.indexable else "no"
    if attribute == "_size_kb":
        return round(page.size_bytes / 1024, 1)
    return _join(getattr(page, attribute))


def write_pages_sheet(sheet: Worksheet, pages: Sequence[PageResult]) -> None:
    """One row per URL, with problem cells filled red (HIGH) or amber (MED)."""
    _write_header(sheet, [c[0] for c in PAGE_COLUMNS], [c[2] for c in PAGE_COLUMNS])

    ordered = sorted(pages, key=lambda p: (-p.inlinks, p.path_depth, p.url))
    for row_index, page in enumerate(ordered, start=2):
        for column_index, (_, attribute, _width) in enumerate(PAGE_COLUMNS, start=1):
            value = _page_cell_value(page, attribute)
            cell = sheet.cell(row=row_index, column=column_index,
                              value="" if value is None else value)
            cell.alignment = TOP

            raw = getattr(page, attribute, value) if not attribute.startswith("_") else value
            rule = _HIGH_CELL_RULES.get(attribute)
            if rule and rule(raw, page):
                cell.fill = FILL_HIGH
                cell.font = FONT_HIGH
                continue
            rule = _MED_CELL_RULES.get(attribute)
            if rule and rule(raw, page):
                cell.fill = FILL_MED

        # Make the URL clickable -- an auditor lives in this column.
        url_cell = sheet.cell(row=row_index, column=1)
        url_cell.hyperlink = page.url
        url_cell.font = Font(color="0563C1", underline="single")


# --- Findings sheet ---------------------------------------------------------


def write_findings_sheet(sheet: Worksheet, findings: Sequence[Finding]) -> None:
    """Severity-sorted findings, one row each, with the full URL list attached."""
    headers = ("Severity", "Category", "Issue", "URLs affected", "Detail",
               "Why it matters", "Example URLs")
    _write_header(sheet, headers, (10, 14, 44, 14, 62, 70, 62))

    for row_index, finding in enumerate(sorted(findings, key=lambda f: f.sort_key()), start=2):
        values = (
            finding.severity,
            finding.category,
            finding.issue,
            finding.count,
            finding.detail,
            finding.why_it_matters,
            "\n".join(finding.urls[:25]),
        )
        for column_index, value in enumerate(values, start=1):
            cell = sheet.cell(row=row_index, column=column_index, value=value)
            cell.alignment = WRAP
            cell.border = THIN_BORDER

        severity_cell = sheet.cell(row=row_index, column=1)
        severity_cell.fill = FILL_BY_SEVERITY.get(finding.severity, FILL_LOW)
        if finding.severity == HIGH:
            severity_cell.font = FONT_HIGH
        sheet.row_dimensions[row_index].height = 46


# --- Summary sheet ----------------------------------------------------------


def write_summary_sheet(
    sheet: Worksheet,
    stats: SiteStats,
    findings: Sequence[Finding],
    *,
    site: str,
    crawled_at: str,
    duration_s: float | None = None,
) -> None:
    """Site stats plus the top ten findings, formatted to be read first."""
    sheet.column_dimensions["A"].width = 34
    sheet.column_dimensions["B"].width = 16
    sheet.column_dimensions["C"].width = 58
    sheet.column_dimensions["D"].width = 74

    sheet["A1"] = "SEO audit -- %s" % site
    sheet["A1"].font = TITLE_FONT
    sheet["A2"] = "Crawled %s%s" % (
        crawled_at, " in %.0fs" % duration_s if duration_s else ""
    )
    sheet["A2"].font = Font(italic=True, color="595959")

    row = 4
    sheet.cell(row=row, column=1, value="Crawl summary").font = SECTION_FONT
    row += 1

    severity_counts = {s: sum(1 for f in findings if f.severity == s)
                       for s in (HIGH, MED, LOW)}
    summary_rows: list[tuple[str, Any]] = [
        ("URLs crawled", stats.total_urls),
        ("Indexable", stats.indexable),
        ("Not indexable", stats.non_indexable),
        ("Product pages", stats.products),
        ("Orphan pages", stats.orphans),
        ("Ghost pages (not in sitemap)", stats.ghosts),
        ("Average word count", stats.avg_word_count),
        ("", ""),
        ("HIGH findings", severity_counts[HIGH]),
        ("MED findings", severity_counts[MED]),
        ("LOW findings", severity_counts[LOW]),
    ]
    for label, value in summary_rows:
        if label:
            sheet.cell(row=row, column=1, value=label).font = Font(bold=(value == ""))
            sheet.cell(row=row, column=2, value=value)
        row += 1

    row += 1
    sheet.cell(row=row, column=1, value="Response codes").font = SECTION_FONT
    row += 1
    for code, count in stats.status_counts.items():
        sheet.cell(row=row, column=1, value=code)
        sheet.cell(row=row, column=2, value=count)
        row += 1

    row += 1
    sheet.cell(row=row, column=1, value="Pages affected by category").font = SECTION_FONT
    row += 1
    for category, count in stats.pages_with_issues.items():
        sheet.cell(row=row, column=1, value=category)
        sheet.cell(row=row, column=2, value=count)
        sheet.cell(row=row, column=3, value="%s of crawled URLs" % stats.issue_rate[category])
        row += 1

    row += 2
    sheet.cell(row=row, column=1, value="Top findings").font = SECTION_FONT
    row += 1
    for label, width in (("Severity", 1), ("URLs", 2), ("Issue", 3), ("Why it matters", 4)):
        cell = sheet.cell(row=row, column=width, value=label)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
    row += 1

    for finding in sorted(findings, key=lambda f: f.sort_key())[:10]:
        sheet.cell(row=row, column=1, value=finding.severity).fill = \
            FILL_BY_SEVERITY.get(finding.severity, FILL_LOW)
        sheet.cell(row=row, column=2, value=finding.count)
        sheet.cell(row=row, column=3, value=finding.issue).alignment = WRAP
        sheet.cell(row=row, column=4, value=finding.why_it_matters).alignment = WRAP
        sheet.row_dimensions[row].height = 42
        row += 1


# --- Anchors and PSI sheets -------------------------------------------------


def write_anchors_sheet(sheet: Worksheet, anchors: Sequence[AnchorRow]) -> None:
    """Internal anchor text, most-used first."""
    _write_header(sheet, ("Anchor text", "Target URL", "Links", "Source pages"),
                  (46, 62, 9, 13))
    for row_index, anchor in enumerate(anchors, start=2):
        sheet.cell(row=row_index, column=1, value=anchor.anchor).alignment = TOP
        sheet.cell(row=row_index, column=2, value=anchor.target).alignment = TOP
        sheet.cell(row=row_index, column=3, value=anchor.count)
        sheet.cell(row=row_index, column=4, value=anchor.sources)
        if anchor.anchor == "(no anchor text)":
            sheet.cell(row=row_index, column=1).fill = FILL_MED


PSI_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("URL", "url", 56),
    ("Inlinks", "inlinks", 9),
    ("Score", "score_pct", 8),
    ("LCP (ms)", "lcp_ms", 11),
    ("CLS", "cls", 8),
    ("TBT (ms)", "tbt_ms", 11),
    ("FCP (ms)", "fcp_ms", 11),
    ("Speed Index (ms)", "speed_index_ms", 16),
    ("Field LCP (ms)", "field_lcp_ms", 14),
    ("Field CLS", "field_cls", 11),
    ("Field INP (ms)", "field_inp_ms", 14),
    ("Field verdict", "field_overall", 14),
    ("Error", "error", 30),
)

#: (attribute, "good" limit) -- above the limit the cell turns amber.
_PSI_LIMITS = (("lcp_ms", 2500), ("cls", 0.1), ("tbt_ms", 200),
               ("fcp_ms", 1800), ("field_lcp_ms", 2500), ("field_cls", 0.1),
               ("field_inp_ms", 200))


def write_psi_sheet(sheet: Worksheet, results: Sequence[PsiResult]) -> None:
    """PSI metrics, with every failing Core Web Vital highlighted."""
    _write_header(sheet, [c[0] for c in PSI_COLUMNS], [c[2] for c in PSI_COLUMNS])
    limits = dict(_PSI_LIMITS)

    for row_index, result in enumerate(results, start=2):
        for column_index, (_, attribute, _width) in enumerate(PSI_COLUMNS, start=1):
            value = getattr(result, attribute)
            if isinstance(value, float):
                value = round(value, 3)
            cell = sheet.cell(row=row_index, column=column_index,
                              value="" if value is None else value)

            limit = limits.get(attribute)
            if limit is not None and isinstance(value, (int, float)) and value > limit:
                cell.fill = FILL_HIGH if attribute in ("lcp_ms", "field_lcp_ms") else FILL_MED
            if attribute == "score_pct" and isinstance(value, int) and value < 90:
                cell.fill = FILL_HIGH if value < 50 else FILL_MED
            if attribute == "field_overall" and value == "SLOW":
                cell.fill = FILL_HIGH


# --- workbook ---------------------------------------------------------------


def report_name(start_url: str, extension: str, when: dt.date | None = None) -> str:
    """``audit_<domain>_<date>.<ext>``."""
    domain = registrable_host(start_url).replace(".", "-") or "site"
    stamp = (when or dt.date.today()).isoformat()
    return "audit_%s_%s.%s" % (domain, stamp, extension)


def write_workbook(
    path: Path,
    pages: Sequence[PageResult],
    findings: Sequence[Finding],
    stats: SiteStats,
    anchors: Sequence[AnchorRow] = (),
    psi_results: Sequence[PsiResult] = (),
    *,
    site: str = "",
    duration_s: float | None = None,
) -> Path:
    """Write the full audit workbook and return its path."""
    workbook = Workbook()
    crawled_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    summary = workbook.active
    summary.title = "Summary"
    write_summary_sheet(summary, stats, findings, site=site or "site",
                        crawled_at=crawled_at, duration_s=duration_s)

    write_pages_sheet(workbook.create_sheet("Pages"), pages)
    write_findings_sheet(workbook.create_sheet("Findings"), findings)
    if anchors:
        write_anchors_sheet(workbook.create_sheet("Anchors"), anchors)
    if psi_results:
        write_psi_sheet(workbook.create_sheet("PSI"), psi_results)

    # A machine-readable copy so --diff never has to re-parse styled cells.
    raw = workbook.create_sheet("_data")
    raw.sheet_state = "hidden"
    raw["A1"] = "findings_json"
    raw["A2"] = json.dumps([
        {"severity": f.severity, "category": f.category, "issue": f.issue,
         "count": f.count, "urls": f.urls[:MAX_DIFF_URLS], "detail": f.detail}
        for f in findings
    ])
    raw["B1"] = "urls_json"
    raw["B2"] = json.dumps(sorted(p.url for p in pages))

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path


#: URLs stored per finding in the hidden diff sheet.
MAX_DIFF_URLS = 200


# --- diff -------------------------------------------------------------------


@dataclass
class CrawlDiff:
    """What changed between a previous audit and this one."""

    new_issues: list[Finding] = field(default_factory=list)
    fixed_issues: list[Finding] = field(default_factory=list)
    worsened: list[tuple[Finding, int, int]] = field(default_factory=list)  # finding, was, now
    improved: list[tuple[Finding, int, int]] = field(default_factory=list)
    new_urls: list[str] = field(default_factory=list)
    removed_urls: list[str] = field(default_factory=list)
    previous_path: str = ""

    @property
    def is_empty(self) -> bool:
        return not any((self.new_issues, self.fixed_issues, self.worsened,
                        self.improved, self.new_urls, self.removed_urls))


def read_previous(path: Path) -> tuple[list[Finding], set[str]]:
    """Read findings and URLs back out of a previous audit workbook.

    Prefers the hidden ``_data`` sheet written by :func:`write_workbook`; falls
    back to parsing the visible Findings and Pages sheets so a workbook produced
    by an older version still diffs.
    """
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if "_data" in workbook.sheetnames:
            sheet = workbook["_data"]
            findings_json = sheet["A2"].value
            urls_json = sheet["B2"].value
            if findings_json:
                findings = [
                    Finding(severity=d["severity"], category=d["category"],
                            issue=d["issue"], urls=d.get("urls", []),
                            detail=d.get("detail", ""))
                    for d in json.loads(findings_json)
                ]
                urls = set(json.loads(urls_json or "[]"))
                return findings, urls

        findings = []
        if "Findings" in workbook.sheetnames:
            rows = workbook["Findings"].iter_rows(min_row=2, values_only=True)
            for row in rows:
                if not row or not row[0]:
                    continue
                severity, category, issue, count = row[0], row[1], row[2], row[3]
                examples = (row[6] or "").split("\n") if len(row) > 6 else []
                findings.append(Finding(
                    severity=str(severity), category=str(category or ""),
                    issue=str(issue or ""),
                    urls=[u for u in examples if u] or [""] * int(count or 0),
                    detail=str(row[4] or ""),
                ))

        urls = set()
        if "Pages" in workbook.sheetnames:
            for row in workbook["Pages"].iter_rows(min_row=2, max_col=1, values_only=True):
                if row and row[0]:
                    urls.add(str(row[0]))
        return findings, urls
    finally:
        workbook.close()


def diff_crawls(
    previous_path: Path, findings: Sequence[Finding], pages: Sequence[PageResult]
) -> CrawlDiff:
    """Compare this crawl against a previous audit workbook."""
    old_findings, old_urls = read_previous(previous_path)

    old_by_key = {f.key: f for f in old_findings}
    new_by_key = {f.key: f for f in findings}

    diff = CrawlDiff(previous_path=str(previous_path))
    diff.new_issues = [f for key, f in new_by_key.items() if key not in old_by_key]
    diff.fixed_issues = [f for key, f in old_by_key.items() if key not in new_by_key]

    for key, current in new_by_key.items():
        previous = old_by_key.get(key)
        if previous is None:
            continue
        if current.count > previous.count:
            diff.worsened.append((current, previous.count, current.count))
        elif current.count < previous.count:
            diff.improved.append((current, previous.count, current.count))

    current_urls = {p.url for p in pages}
    diff.new_urls = sorted(current_urls - old_urls)
    diff.removed_urls = sorted(old_urls - current_urls)

    diff.new_issues.sort(key=lambda f: f.sort_key())
    diff.fixed_issues.sort(key=lambda f: f.sort_key())
    return diff


def write_diff_sheet(workbook: Workbook, diff: CrawlDiff) -> None:
    """Add a Diff sheet describing the change since the previous crawl."""
    sheet = workbook.create_sheet("Diff", 1)
    _write_header(sheet, ("Change", "Severity", "Issue / URL", "Was", "Now", "Detail"),
                  (16, 10, 60, 8, 8, 60))

    row = 2

    def add(change: str, severity: str, label: str,
            was: Any = "", now: Any = "", detail: str = "", fill: PatternFill | None = None):
        nonlocal row
        for column, value in enumerate((change, severity, label, was, now, detail), start=1):
            cell = sheet.cell(row=row, column=column, value=value)
            cell.alignment = WRAP
        if fill:
            sheet.cell(row=row, column=1).fill = fill
        row += 1

    for finding in diff.new_issues:
        add("NEW ISSUE", finding.severity, finding.issue, "", finding.count,
            finding.detail, FILL_HIGH)
    for finding, was, now in sorted(diff.worsened, key=lambda t: t[0].sort_key()):
        add("WORSE", finding.severity, finding.issue, was, now, finding.detail, FILL_MED)
    for finding, was, now in sorted(diff.improved, key=lambda t: t[0].sort_key()):
        add("IMPROVED", finding.severity, finding.issue, was, now, finding.detail, FILL_LOW)
    for finding in diff.fixed_issues:
        add("FIXED", finding.severity, finding.issue, finding.count, 0, "", FILL_LOW)
    for url in diff.new_urls[:200]:
        add("NEW URL", "", url)
    for url in diff.removed_urls[:200]:
        add("REMOVED URL", "", url)


# --- HTML report ------------------------------------------------------------

_HTML_CSS = """
:root { color-scheme: light dark;
  --bg:#ffffff; --fg:#1a1a1a; --muted:#5c6670; --line:#e3e6ea; --card:#f7f8fa;
  --high:#c0392b; --high-bg:#fdecea; --med:#b7791f; --med-bg:#fef6e4;
  --low:#2f6f4f; --low-bg:#eaf5ef; --accent:#1f3864; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#14171a; --fg:#e8eaed; --muted:#9aa4af; --line:#2a2f36; --card:#1c2025;
  --high:#ff8a80; --high-bg:#3a1f1d; --med:#ffcf70; --med-bg:#3a2f18;
  --low:#8fd3ad; --low-bg:#1c2f26; --accent:#8ab4f8; } }
* { box-sizing:border-box; }
body { margin:0; padding:0; background:var(--bg); color:var(--fg);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
.wrap { max-width:1000px; margin:0 auto; padding:40px 24px 80px; }
h1 { font-size:26px; margin:0 0 4px; color:var(--accent); }
h2 { font-size:19px; margin:40px 0 12px; padding-bottom:6px; border-bottom:2px solid var(--line); }
.sub { color:var(--muted); margin:0 0 28px; font-size:14px; }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:12px; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px 16px; }
.stat .n { font-size:24px; font-weight:700; display:block; }
.stat .l { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
.finding { border:1px solid var(--line); border-left-width:5px; border-radius:8px;
  padding:16px 18px; margin:12px 0; background:var(--card); }
.finding.HIGH { border-left-color:var(--high); }
.finding.MED  { border-left-color:var(--med); }
.finding.LOW  { border-left-color:var(--low); }
.badge { display:inline-block; font-size:11px; font-weight:700; letter-spacing:.06em;
  padding:2px 8px; border-radius:4px; vertical-align:2px; }
.badge.HIGH { background:var(--high-bg); color:var(--high); }
.badge.MED  { background:var(--med-bg);  color:var(--med); }
.badge.LOW  { background:var(--low-bg);  color:var(--low); }
.finding h3 { display:inline; font-size:16px; margin:0 0 0 10px; }
.count { float:right; color:var(--muted); font-size:13px; }
.detail { margin:10px 0 0; font-size:14px; }
.why { margin:10px 0 0; padding:10px 12px; background:var(--bg); border-radius:6px;
  border:1px dashed var(--line); font-size:14px; color:var(--muted); }
.why b { color:var(--fg); font-weight:600; }
details { margin-top:10px; font-size:13px; }
summary { cursor:pointer; color:var(--accent); }
details ul { margin:8px 0 0; padding-left:20px; max-height:260px; overflow:auto; }
details li { word-break:break-all; margin:2px 0; }
table { width:100%; border-collapse:collapse; font-size:14px; margin-top:8px;
  display:block; overflow-x:auto; }
th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
footer { margin-top:56px; padding-top:16px; border-top:1px solid var(--line);
  color:var(--muted); font-size:13px; }
"""


def _finding_html(finding: Finding) -> str:
    """Render one finding as a card."""
    urls = "".join("<li>%s</li>" % html.escape(u) for u in finding.urls[:100])
    more = ("<li><em>... and %d more</em></li>" % (finding.count - 100)
            if finding.count > 100 else "")
    url_block = (
        "<details><summary>%d affected URL%s</summary><ul>%s%s</ul></details>"
        % (finding.count, "" if finding.count == 1 else "s", urls, more)
    ) if finding.urls else ""

    return (
        '<div class="finding {sev}">'
        '<span class="badge {sev}">{sev}</span><h3>{issue}</h3>'
        '<span class="count">{count} URL{plural}</span>'
        '<p class="detail">{detail}</p>'
        '<p class="why"><b>Why it matters:</b> {why}</p>'
        "{urls}</div>"
    ).format(
        sev=finding.severity,
        issue=html.escape(finding.issue),
        count=finding.count,
        plural="" if finding.count == 1 else "s",
        detail=html.escape(finding.detail),
        why=html.escape(finding.why_it_matters),
        urls=url_block,
    )


def write_html_report(
    path: Path,
    stats: SiteStats,
    findings: Sequence[Finding],
    *,
    site: str,
    psi_results: Sequence[PsiResult] = (),
    diff: CrawlDiff | None = None,
) -> Path:
    """Write a single self-contained HTML report, presentable to a client as-is."""
    ordered = sorted(findings, key=lambda f: f.sort_key())
    counts = {s: sum(1 for f in ordered if f.severity == s) for s in (HIGH, MED, LOW)}

    stat_cards = "".join(
        '<div class="stat"><span class="n">%s</span><span class="l">%s</span></div>' % (v, k)
        for k, v in (
            ("URLs crawled", stats.total_urls),
            ("Indexable", stats.indexable),
            ("Products", stats.products),
            ("Orphans", stats.orphans),
            ("High", counts[HIGH]),
            ("Medium", counts[MED]),
            ("Low", counts[LOW]),
        )
    )

    sections = []
    for severity, label in ((HIGH, "Critical (HIGH)"), (MED, "Worth fixing (MED)"),
                            (LOW, "Polish (LOW)")):
        group = [f for f in ordered if f.severity == severity]
        if group:
            sections.append("<h2>%s</h2>%s" % (
                label, "".join(_finding_html(f) for f in group)))

    psi_block = ""
    if psi_results:
        rows = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                html.escape(r.url),
                r.score_pct if r.score_pct is not None else "-",
                "%.1fs" % ((r.field_lcp_ms or r.lcp_ms) / 1000)
                if (r.field_lcp_ms or r.lcp_ms) else "-",
                "%.3f" % (r.field_cls if r.field_cls is not None else r.cls)
                if (r.field_cls is not None or r.cls is not None) else "-",
                "%.0fms" % r.tbt_ms if r.tbt_ms is not None else "-",
            )
            for r in psi_results
        )
        psi_block = (
            "<h2>PageSpeed (mobile)</h2><table><tr><th>URL</th><th>Score</th>"
            "<th>LCP</th><th>CLS</th><th>TBT</th></tr>%s</table>" % rows
        )

    diff_block = ""
    if diff is not None:
        lines = []
        if diff.new_issues:
            lines.append("<li><b>%d new issue types</b> since the last crawl: %s</li>"
                         % (len(diff.new_issues),
                            html.escape(", ".join(f.issue for f in diff.new_issues[:5]))))
        if diff.fixed_issues:
            lines.append("<li><b>%d issue types fixed</b>: %s</li>"
                         % (len(diff.fixed_issues),
                            html.escape(", ".join(f.issue for f in diff.fixed_issues[:5]))))
        if diff.worsened:
            lines.append("<li>%d issues affect more URLs than before</li>" % len(diff.worsened))
        if diff.improved:
            lines.append("<li>%d issues affect fewer URLs than before</li>" % len(diff.improved))
        if diff.new_urls or diff.removed_urls:
            lines.append("<li>%d new URLs, %d removed</li>"
                         % (len(diff.new_urls), len(diff.removed_urls)))
        if lines:
            diff_block = "<h2>Change since last crawl</h2><ul>%s</ul>" % "".join(lines)

    document = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>SEO audit -- {site}</title><style>{css}</style></head><body><div class='wrap'>"
        "<h1>SEO audit -- {site}</h1>"
        "<p class='sub'>Crawled {when} &middot; {total} URLs &middot; "
        "{high} critical, {med} medium, {low} low findings</p>"
        "<div class='stats'>{cards}</div>"
        "{diff}{sections}{psi}"
        "<footer>Generated by seocrawl. Findings are ordered by severity, then by how "
        "many URLs they affect.</footer>"
        "</div></body></html>"
    ).format(
        site=html.escape(site),
        css=_HTML_CSS,
        when=dt.datetime.now().strftime("%d %b %Y, %H:%M"),
        total=stats.total_urls,
        high=counts[HIGH], med=counts[MED], low=counts[LOW],
        cards=stat_cards,
        diff=diff_block,
        sections="".join(sections) or "<h2>No issues found</h2>",
        psi=psi_block,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    return path


# --- keyword sheets ---------------------------------------------------------

#: Status -> fill. GAP is the money row, so it gets the loudest colour.
_STATUS_FILL = {
    "GAP": FILL_HIGH,
    "CANNIBAL": FILL_MED,
    "WEAK": FILL_LOW,
    "OWNED": None,
}

KEYWORD_MAP_COLUMNS: tuple[tuple[str, int], ...] = (
    ("Keyword", 38), ("Volume (mid)", 12), ("Volume range", 18),
    ("Competition", 12), ("Top bid (high)", 14), ("Status", 10),
    ("Owner URL", 56), ("Page type", 12), ("Score", 8),
    ("Contenders", 62), ("Inverted targeting", 34),
)


def write_keyword_map_sheet(sheet: Worksheet, keyword_map) -> None:
    """Every keyword with its verdict, GAP first."""
    _write_header(sheet, [c[0] for c in KEYWORD_MAP_COLUMNS],
                  [c[1] for c in KEYWORD_MAP_COLUMNS])

    for row_index, match in enumerate(keyword_map.matches, start=2):
        row = match.row
        values = (
            match.keyword,
            match.volume_mid,
            row.volume.label,
            row.competition or "",
            row.top_bid_high if row.top_bid_high is not None else "",
            match.status,
            match.owner or "",
            match.owner_page_type or "",
            match.score,
            match.contender_summary,
            match.inverted,
        )
        for column_index, value in enumerate(values, start=1):
            cell = sheet.cell(row=row_index, column=column_index, value=value)
            cell.alignment = TOP

        status_cell = sheet.cell(row=row_index, column=6)
        fill = _STATUS_FILL.get(match.status)
        if fill:
            status_cell.fill = fill
        if match.status == "GAP":
            status_cell.font = FONT_HIGH
        if match.inverted:
            sheet.cell(row=row_index, column=11).fill = FILL_MED


def write_gaps_sheet(sheet: Worksheet, keyword_map) -> None:
    """Gap keywords grouped into clusters, with the action for each.

    One row per cluster rather than per keyword: a list of ninety unowned
    keywords is a research task, while "twelve keywords about slim wallets, no
    owning URL" is a page to build.
    """
    _write_header(
        sheet,
        ("Cluster", "Keywords", "Combined volume", "Suggested action",
         "Keywords in cluster", "Top keyword", "Top volume"),
        (30, 10, 16, 78, 62, 30, 11),
    )

    for row_index, cluster in enumerate(keyword_map.clusters, start=2):
        ranked = sorted(cluster.keywords, key=lambda m: -m.volume_mid)
        values = (
            cluster.head,
            cluster.size,
            cluster.volume,
            cluster.suggested_action(),
            ", ".join(m.keyword for m in ranked[:25]),
            ranked[0].keyword if ranked else "",
            ranked[0].volume_mid if ranked else 0,
        )
        for column_index, value in enumerate(values, start=1):
            cell = sheet.cell(row=row_index, column=column_index, value=value)
            cell.alignment = WRAP if column_index in (4, 5) else TOP

        if cluster.size > 1:
            sheet.cell(row=row_index, column=1).fill = FILL_HIGH
        sheet.row_dimensions[row_index].height = 32


def write_cannibalization_sheet(sheet: Worksheet, keyword_map) -> None:
    """One row per contested keyword, contenders side by side."""
    _write_header(
        sheet,
        ("Keyword", "Volume (mid)", "Contenders", "Page types",
         "Page A", "Score A", "Page B", "Score B", "Margin"),
        (34, 12, 10, 24, 52, 9, 52, 9, 9),
    )

    contested = keyword_map.by_status("CANNIBAL")
    for row_index, match in enumerate(contested, start=2):
        contenders = match.contenders
        first = contenders[0] if contenders else None
        second = contenders[1] if len(contenders) > 1 else None

        values = (
            match.keyword,
            match.volume_mid,
            len(contenders),
            ", ".join(sorted({c.page_type for c in contenders})),
            first.url if first else "",
            round(first.score, 3) if first else "",
            second.url if second else "",
            round(second.score, 3) if second else "",
            round(first.score - second.score, 3) if first and second else "",
        )
        for column_index, value in enumerate(values, start=1):
            sheet.cell(row=row_index, column=column_index, value=value).alignment = TOP

        # A product competing with its own collection is the case worth fixing.
        if len({c.page_type for c in contenders}) > 1:
            sheet.cell(row=row_index, column=4).fill = FILL_MED


def write_keyword_sheets(path: Path, keyword_map, *, append_to: Path | None = None) -> Path:
    """Write the three keyword sheets, into an existing audit or standalone.

    Appending puts the keyword map next to the crawl it was joined against,
    which is where it is actually useful; the standalone file exists for when
    the audit workbook belongs to a client and should not be modified.
    """
    if append_to is not None and Path(append_to).is_file():
        workbook = load_workbook(append_to)
        for name in ("Keyword Map", "Gaps", "Cannibalization"):
            if name in workbook.sheetnames:
                del workbook[name]     # re-running must not stack duplicates
    else:
        workbook = Workbook()
        workbook.remove(workbook.active)

    write_keyword_map_sheet(workbook.create_sheet("Keyword Map"), keyword_map)
    write_gaps_sheet(workbook.create_sheet("Gaps"), keyword_map)
    write_cannibalization_sheet(workbook.create_sheet("Cannibalization"), keyword_map)

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path
