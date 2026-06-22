"""Tests for the deterministic trace-first slice (no API, no network)."""

from __future__ import annotations

from rca_agent.config import get_settings
from rca_agent.gitlab_client import MockGitLabClient
from rca_agent.investigation import investigate
from rca_agent.schema import Confidence, Triage
from rca_agent.stack_trace import crash_site, parse_stack_trace
from rca_agent.tickets import load_fixture_ticket


# --- stack trace parsing -------------------------------------------------------

def test_python_trace_crash_site_is_last_frame():
    text = (
        'Traceback (most recent call last):\n'
        '  File "billing/api.py", line 16, in create_invoice_endpoint\n'
        '    total = invoice.compute_total()\n'
        '  File "billing/invoice.py", line 27, in compute_total\n'
        '    return self.subtotal() * self.tax_rate\n'
        'TypeError: ...\n'
    )
    frames = parse_stack_trace(text)
    assert [f.ref() for f in frames] == ["billing/api.py:16", "billing/invoice.py:27"]
    cs = crash_site(frames)
    assert cs.ref() == "billing/invoice.py:27"
    assert cs.symbol == "compute_total"


def test_java_trace_is_normalized_to_crash_last():
    text = (
        'Exception in thread "main" java.lang.NullPointerException\n'
        '\tat com.acme.Billing.computeTotal(Billing.java:87)\n'
        '\tat com.acme.Billing.main(Billing.java:21)\n'
    )
    frames = parse_stack_trace(text)
    # Java lists innermost first; we reverse so the crash site is last.
    assert crash_site(frames).ref() == "Billing.java:87"


def test_js_trace_is_normalized_to_crash_last():
    text = (
        "TypeError: x\n"
        "    at computeTotal (/srv/app/billing/invoice.js:27:12)\n"
        "    at handler (/srv/app/billing/api.js:16:5)\n"
    )
    frames = parse_stack_trace(text)
    # Crash site is normalized to last; the parser preserves the path verbatim
    # (mapping deploy paths like /srv/app/... to repo-relative is a later refinement).
    assert crash_site(frames).ref() == "/srv/app/billing/invoice.js:27"


def test_no_trace_returns_empty():
    assert parse_stack_trace("the dashboard feels slow") == []


# --- mock GitLab client --------------------------------------------------------

def test_blame_maps_line_to_correct_commit():
    c = MockGitLabClient()
    # Line 27 falls in the regression span (lines 18-27).
    commit = c.blame_line("acme/billing-service", "main", "billing/invoice.py", 27)
    assert commit is not None
    assert commit.short_id == "abc1234d"
    # Line 5 is in the initial span (lines 1-17).
    initial = c.blame_line("acme/billing-service", "main", "billing/invoice.py", 5)
    assert initial.short_id == "0aa1b2c3"


def test_mr_for_introducing_commit():
    c = MockGitLabClient()
    mrs = c.merge_requests_for_commit(
        "acme/billing-service", "abc1234d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b")
    assert len(mrs) == 1 and mrs[0].iid == 42 and mrs[0].state == "merged"


# --- full pipeline -------------------------------------------------------------

def test_investigate_produces_high_confidence_regression_verdict():
    key, text = load_fixture_ticket("RCA-101")
    v = investigate(key, text, MockGitLabClient(), get_settings())

    assert v.confidence is Confidence.HIGH
    assert v.triage is Triage.REAL_BUG
    assert v.is_regression is True
    assert "merge_requests/42" in (v.introducing_mr or "")

    kinds = [e.kind for e in v.evidence_chain]
    # The full chain: symptom -> code -> blame -> commit -> introducing MR.
    assert kinds == ["stack_frame", "file_content", "blame", "commit", "merge_request"]
    # Verification pass actually fetched the crash line content.
    assert any("self.tax_rate" in e.detail for e in v.evidence_chain)


def test_no_trace_ticket_degrades_to_candidates_not_a_fabricated_cause():
    v = investigate("RCA-999", "Dashboard feels slow in the afternoon. No error.",
                    MockGitLabClient(), get_settings())
    assert v.confidence is Confidence.LOW
    assert v.triage is Triage.INSUFFICIENT_EVIDENCE
    assert v.evidence_chain == []
