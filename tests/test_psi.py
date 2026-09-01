"""Tests for the PageSpeed Insights integration (no network calls)."""

from __future__ import annotations

import pytest

from seocrawl.crawler import PageResult
from seocrawl.findings import HIGH, MED
from seocrawl.psi import (
    PsiClient,
    PsiResult,
    parse_psi_response,
    psi_findings,
    select_urls,
)

SITE = "https://shop.test"


def payload(*, score=0.55, lcp=4200.0, cls=0.24, tbt=610.0, fcp=1900.0, field=True):
    """A PSI API response shaped like the real one."""
    body = {
        "lighthouseResult": {
            "categories": {"performance": {"score": score}},
            "audits": {
                "largest-contentful-paint": {"numericValue": lcp},
                "cumulative-layout-shift": {"numericValue": cls},
                "total-blocking-time": {"numericValue": tbt},
                "first-contentful-paint": {"numericValue": fcp},
                "speed-index": {"numericValue": 3300.0},
                "interactive": {"numericValue": 5100.0},
            },
        }
    }
    if field:
        body["loadingExperience"] = {
            "overall_category": "SLOW",
            "metrics": {
                "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 3800},
                "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 15},
                "INTERACTION_TO_NEXT_PAINT": {"percentile": 340},
                "FIRST_CONTENTFUL_PAINT_MS": {"percentile": 2200},
            },
        }
    return body


# --- parsing ----------------------------------------------------------------


def test_parse_lab_metrics():
    result = parse_psi_response(payload(), SITE + "/a")
    assert result.performance_score == 0.55 and result.score_pct == 55
    assert result.lcp_ms == 4200.0 and result.cls == 0.24
    assert result.tbt_ms == 610.0 and result.fcp_ms == 1900.0
    assert result.error is None


def test_parse_field_metrics_and_unit_conversion():
    """CrUX reports CLS x100, so 15 must come back as 0.15."""
    result = parse_psi_response(payload(), SITE + "/a")
    assert result.has_field_data
    assert result.field_lcp_ms == 3800.0
    assert result.field_cls == pytest.approx(0.15)
    assert result.field_inp_ms == 340.0
    assert result.field_overall == "SLOW"


def test_parse_without_field_data():
    """A low-traffic URL has no CrUX entry; lab data must still parse."""
    result = parse_psi_response(payload(field=False), SITE + "/a")
    assert not result.has_field_data
    assert result.field_lcp_ms is None and result.lcp_ms == 4200.0


def test_parse_tolerates_an_empty_payload():
    result = parse_psi_response({}, SITE + "/a")
    assert result.performance_score is None and result.lcp_ms is None
    assert result.error is None


def test_parse_tolerates_missing_audits():
    result = parse_psi_response({"lighthouseResult": {"audits": {}}}, SITE + "/a")
    assert result.lcp_ms is None


# --- URL selection ----------------------------------------------------------


def _page(path, inlinks, **kwargs):
    defaults = dict(url=SITE + path, status=200, canonical_state="self", inlinks=inlinks)
    defaults.update(kwargs)
    return PageResult(**defaults)


def test_select_urls_ranks_by_inlinks():
    pages = [_page("/a", 2), _page("/b", 40), _page("/c", 9)]
    assert [p.url for p in select_urls(pages, top=2)] == [SITE + "/b", SITE + "/c"]


def test_select_urls_skips_non_indexable():
    pages = [_page("/good", 5), _page("/no", 99, noindex=True),
             _page("/dead", 99, status=404)]
    assert [p.url for p in select_urls(pages, top=5)] == [SITE + "/good"]


def test_select_urls_respects_the_limit():
    assert len(select_urls([_page("/%d" % i, i) for i in range(50)], top=10)) == 10


# --- findings ---------------------------------------------------------------


def _result(**kwargs) -> PsiResult:
    defaults = dict(url=SITE + "/a", performance_score=0.95, lcp_ms=1800.0,
                    cls=0.02, tbt_ms=90.0, fcp_ms=1200.0)
    defaults.update(kwargs)
    return PsiResult(**defaults)


def find(findings, fragment):
    return next((f for f in findings if fragment.lower() in f.issue.lower()), None)


def test_no_findings_when_everything_passes():
    assert psi_findings([_result()]) == []


def test_slow_lcp_is_high():
    result = find(psi_findings([_result(lcp_ms=4200.0)]), "LCP")
    assert result.severity == HIGH and "4.2s" in result.detail


def test_field_lcp_overrides_a_good_lab_score():
    """A fast lab run must not mask slow real-user data."""
    finding = find(psi_findings([_result(lcp_ms=1200.0, field_lcp_ms=3900.0)]), "LCP")
    assert finding is not None and "3.9s" in finding.detail


def test_high_cls_is_med():
    assert find(psi_findings([_result(cls=0.3)]), "CLS").severity == MED


def test_slow_inp_uses_field_data_only():
    assert find(psi_findings([_result(field_inp_ms=350.0)]), "INP").severity == MED
    assert find(psi_findings([_result()]), "INP") is None


def test_high_tbt_names_the_worst_page():
    finding = find(psi_findings([_result(tbt_ms=800.0)]), "Total Blocking Time")
    assert "800ms" in finding.detail


def test_low_score_is_reported():
    finding = find(psi_findings([_result(performance_score=0.42)]), "performance score")
    assert finding.severity == MED and "42" in finding.detail


def test_slow_field_category_is_high():
    finding = find(psi_findings([_result(field_overall="SLOW")]), "real users")
    assert finding.severity == HIGH


def test_errored_results_are_ignored():
    assert psi_findings([PsiResult(url=SITE + "/a", error="HTTP 429")]) == []


def test_all_psi_findings_have_a_why():
    findings = psi_findings([_result(lcp_ms=5000.0, cls=0.4, tbt_ms=900.0,
                                     performance_score=0.3, field_overall="SLOW")])
    assert len(findings) == 5
    assert all(f.why_it_matters and f.category == "performance" for f in findings)


# --- client behaviour -------------------------------------------------------


class FakeSession:
    """Records calls and replays a canned sequence of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        return self.responses.pop(0)


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def test_client_sends_key_and_strategy():
    session = FakeSession([FakeResponse(200, payload())])
    client = PsiClient("KEY123", strategy="mobile", delay=0, session=session)
    result = client.analyze(SITE + "/a")

    assert result.score_pct == 55
    assert session.calls[0]["key"] == "KEY123"
    assert session.calls[0]["strategy"] == "mobile"


def test_client_works_without_an_api_key():
    session = FakeSession([FakeResponse(200, payload())])
    PsiClient(None, delay=0, session=session).analyze(SITE + "/a")
    assert "key" not in session.calls[0]


def test_client_reports_api_errors_without_raising():
    session = FakeSession([FakeResponse(400, {"error": {"message": "Invalid key"}})])
    result = PsiClient("bad", delay=0, session=session).analyze(SITE + "/a")
    assert "HTTP 400" in result.error and "Invalid key" in result.error


def test_client_retries_once_on_rate_limit():
    session = FakeSession([FakeResponse(429, {}), FakeResponse(200, payload())])
    client = PsiClient("k", delay=0, session=session)
    client._wait = lambda: None  # skip the 5s backoff sleep in tests

    import seocrawl.psi as psi_module
    original_sleep = psi_module.time.sleep
    psi_module.time.sleep = lambda _: None
    try:
        result = client.analyze(SITE + "/a")
    finally:
        psi_module.time.sleep = original_sleep

    assert result.error is None and len(session.calls) == 2
