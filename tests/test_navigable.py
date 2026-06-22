"""Tests for the QA-navigable verdict: plain_summary + clickable evidence links."""

from __future__ import annotations

from rca_agent.config import Settings, get_settings
from rca_agent.gitlab_client import MockGitLabClient, web_blob_url, web_commit_url
from rca_agent.investigation import investigate
from rca_agent.schema import Confidence, EvidenceLink, Triage, Verdict, render_verdict
from rca_agent.tickets import load_fixture_ticket


def _settings(gitlab_url=None):
    return Settings(backend="mock", gitlab_url=gitlab_url, gitlab_token=None,
                    jira_url=None, jira_email=None, jira_token=None,
                    model="m", context_lines=12)


# --- URL helpers ---------------------------------------------------------------

def test_web_url_helpers():
    assert web_blob_url("http://gl/", "a/b", "main", "x/y.py", 27) == \
        "http://gl/a/b/-/blob/main/x/y.py#L27"
    assert web_commit_url("http://gl", "a/b", "deadbeef") == "http://gl/a/b/-/commit/deadbeef"
    assert web_blob_url("", "a/b", "main", "x.py", 1) == ""  # no base -> no link


# --- schema + render -----------------------------------------------------------

def test_schema_requires_plain_summary_and_allows_url():
    s = Verdict.to_json_schema()
    assert "plain_summary" in s["required"]
    assert "headline" in s["required"]
    assert "url" in s["properties"]["evidence_chain"]["items"]["properties"]


def _full_verdict():
    return Verdict(
        ticket="T-1", probable_root_cause="long technical cause paragraph here",
        headline="Liability screen reads the old DB collection; fixed by MR 42.",
        plain_summary="The totals screen shows blank for some regions.",
        evidence_chain=[
            EvidenceLink("file_content", "billing/invoice.py:27", "the totalling code",
                         url="http://gl/p/-/blob/main/billing/invoice.py#L27"),
            EvidenceLink("commit", "abc1234", "the change", url="http://gl/p/-/commit/abc1234"),
            EvidenceLink("merge_request", "!42", "introducing MR", url="http://gl/p/-/merge_requests/42"),
        ],
        triage=Triage.REAL_BUG, confidence=Confidence.HIGH, introducing_mr="!42",
        suggested_next_action="Deploy MR 42.",
    )


def test_brief_render_is_short_and_scannable():
    out = render_verdict(_full_verdict(), brief=True)
    assert "Liability screen reads the old DB collection" in out  # headline
    assert "In plain terms:" in out
    assert "Key links:" in out
    assert "+ 1 more" in out                       # 3 evidence, 2 key links shown
    assert "Probable root cause:" not in out       # full-only section omitted


def test_verdict_to_adf_has_summary_and_collapsible_details():
    from rca_agent.schema import verdict_to_adf
    doc = verdict_to_adf(_full_verdict())
    assert doc["type"] == "doc"
    # first visible node carries the headline
    assert "Liability screen reads" in doc["content"][0]["content"][0]["text"]
    # there's a collapsible expand block titled "RCA details ..."
    expand = next(n for n in doc["content"] if n["type"] == "expand")
    assert "RCA details" in expand["attrs"]["title"]
    # evidence links are clickable inside the details
    blob = repr(expand["content"])
    assert "link" in blob and "/merge_requests/42" in blob


def test_render_shows_plain_terms_and_clickable_links():
    v = Verdict(
        ticket="T-1", probable_root_cause="technical cause",
        plain_summary="The invoice total screen breaks for some regions.",
        evidence_chain=[EvidenceLink("file_content", "billing/invoice.py:27",
                                     "the code that computes the total",
                                     url="http://gl/p/-/blob/main/billing/invoice.py#L27")],
        triage=Triage.REAL_BUG, confidence=Confidence.HIGH,
    )
    out = render_verdict(v)
    assert "In plain terms:" in out
    assert "The invoice total screen breaks" in out
    assert "open: http://gl/p/-/blob/main/billing/invoice.py#L27" in out


# --- deterministic pipeline populates them -------------------------------------

def test_investigate_populates_plain_summary_and_links():
    key, text = load_fixture_ticket("RCA-101")
    v = investigate(key, text, MockGitLabClient(), _settings("https://gl.example.com"))
    assert v.plain_summary and "invoice.py" in v.plain_summary
    # crash-site evidence carries a clickable GitLab link built from the base URL
    assert v.evidence_chain[0].url == \
        "https://gl.example.com/acme/billing-service/-/blob/main/billing/invoice.py#L27"


def test_investigate_no_base_url_still_summarizes_without_links():
    key, text = load_fixture_ticket("RCA-101")
    v = investigate(key, text, MockGitLabClient(), _settings(gitlab_url=None))
    assert v.plain_summary                       # plain summary always present
    # base-derived links (file/blame/commit) are empty without a base URL...
    base_kinds = {"stack_frame", "file_content", "blame", "commit"}
    assert all(e.url == "" for e in v.evidence_chain if e.kind in base_kinds)
    # ...but a merge_request carries its own web_url regardless.
    mr = next((e for e in v.evidence_chain if e.kind == "merge_request"), None)
    assert mr is not None and mr.url.endswith("/merge_requests/42")
