"""Tests for the post-hoc blame guard (blame gathered but dropped from verdict)."""

from __future__ import annotations

from rca_agent.agent import blame_dropped_note
from rca_agent.schema import Confidence, EvidenceLink, Triage, Verdict

BLAME = "mcp__rca__git_blame"


def _verdict(evidence=None, is_regression=None):
    return Verdict(
        ticket="AUT-9706", probable_root_cause="cause",
        plain_summary="summary",
        evidence_chain=evidence or [],
        triage=Triage.REAL_BUG, confidence=Confidence.MEDIUM,
        is_regression=is_regression,
    )


def test_flags_when_blame_ran_but_nothing_recorded():
    # The AUT-9706 case: blame called, dates used in prose, but no evidence entry
    # and is_regression left null.
    note = blame_dropped_note(_verdict(), {BLAME, "mcp__rca__fetch_file_lines"})
    assert note.startswith("[auto-check]")
    assert "no blame/commit evidence" in note
    assert "is_regression left unset" in note


def test_silent_when_blame_recorded_and_regression_set():
    ev = [EvidenceLink("blame", "billing/invoice.py:27", "SHA abc1234 2022-10-01",
                       url="http://gl/p/-/commit/abc1234")]
    assert blame_dropped_note(_verdict(ev, is_regression=False), {BLAME}) == ""


def test_silent_when_blame_never_ran():
    # No blame call -> guard must not fire even if is_regression is null.
    assert blame_dropped_note(_verdict(), {"mcp__rca__fetch_file_lines"}) == ""


def test_commit_evidence_counts_but_missing_regression_still_flags():
    ev = [EvidenceLink("commit", "abc1234", "the change",
                       url="http://gl/p/-/commit/abc1234")]
    note = blame_dropped_note(_verdict(ev, is_regression=None), {BLAME})
    assert "no blame/commit evidence" not in note      # commit evidence satisfies that half
    assert "is_regression left unset" in note
