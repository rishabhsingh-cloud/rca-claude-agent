"""Tests for no-trace localization: graph symbol search + GitLab code search."""

from __future__ import annotations

from rca_agent.config import get_settings
from rca_agent.gitlab_client import MockGitLabClient
from rca_agent.graph_store import build_repo_graph
from rca_agent.investigation import investigate

PROJECT = "acme/billing-service"


def test_graph_symbol_search_finds_relevant_function():
    g = build_repo_graph(PROJECT)
    hits = g.search_symbols(["compute", "total"])
    assert any(n.name == "compute_total" for n in hits)
    # ranking puts the multi-term match at/near the top
    assert hits[0].name in ("compute_total", "subtotal")


def test_search_blobs_mock_greps_source():
    c = MockGitLabClient()
    hits = c.search_blobs(PROJECT, "main", "tax_rate")
    assert hits and all("tax_rate" in h["data"].lower() for h in hits)
    assert any(h["path"] == "billing/invoice.py" for h in hits)


def test_search_blobs_returns_line_numbers():
    c = MockGitLabClient()
    hits = c.search_blobs(PROJECT, "main", "def compute_total")
    assert hits[0]["path"] == "billing/invoice.py"
    assert hits[0]["startline"] == 25


def test_no_trace_verdict_localizes_candidate_symbols():
    # A no-trace functional ticket whose words point at the billing repo.
    text = ("[BILL-9] Invoice total is wrong\n"
            "When computing the invoice total for some customers the amount is "
            "off. No error or traceback, the total just looks incorrect.")
    v = investigate("BILL-9", text, MockGitLabClient(), get_settings())
    assert v.confidence.value == "low"
    assert v.evidence_chain == []
    # It should now surface concrete candidate symbols, not just repo names.
    assert any("billing/invoice.py:" in c for c in v.candidates)
