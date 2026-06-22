"""Tests for the repo-summary + cross-service architecture layer."""

from __future__ import annotations

from rca_agent.architecture import orientation_excerpt, search_architecture
from rca_agent.prompts import build_system_prompt
from rca_agent.routing import read_summary, route_repo


# --- repo summaries ------------------------------------------------------------

def test_read_summary_returns_human_text():
    md = read_summary("acme/billing-service")
    assert md and "Notes for QA" in md  # the human block is present


def test_read_summary_missing_repo_is_none():
    assert read_summary("acme/does-not-exist") is None


# --- architecture reference ----------------------------------------------------

def test_orientation_excerpt_has_quick_orientation_and_appendix():
    exc = orientation_excerpt()
    assert "RCA Quick Orientation" in exc
    assert "Cross-cutting RCA notes" in exc  # Appendix included
    assert "Inter-Project Dependencies" in exc


def test_search_architecture_finds_relevant_section():
    hits = search_architecture("invoice total wrong on create")
    assert hits
    joined = " ".join(h["text"] for h in hits)
    assert "compute_total" in joined or "billing engine" in joined


def test_search_architecture_empty_query_returns_nothing():
    assert search_architecture("a an to") == []  # all stopword-length tokens


# --- prompt wiring -------------------------------------------------------------

def test_system_prompt_embeds_cross_service_map():
    p = build_system_prompt()
    assert "Cross-service map" in p
    assert "RCA Quick Orientation" in p
    # the no-trace 3-layer flow + new tools are advertised
    assert "mcp__rca__search_architecture" in p
    assert "mcp__rca__get_repo_summary" in p
