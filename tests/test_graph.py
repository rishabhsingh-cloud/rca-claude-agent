"""Tests for the Step 3 localization + graph layer (no API, no network)."""

from __future__ import annotations

import json

from rca_agent.config import FIXTURES_DIR, get_settings
from rca_agent.gitlab_client import MockGitLabClient
from rca_agent.graph import build_graph_from_sources
from rca_agent.graph_store import build_repo_graph, load_repo_graph
from rca_agent.graphify_adapter import from_graphify_json
from rca_agent.investigation import investigate
from rca_agent.summarize import generate_summary
from rca_agent.tickets import load_fixture_ticket

PROJECT = "acme/billing-service"


# --- AST graph -----------------------------------------------------------------

def test_graph_resolves_self_call_and_construction():
    g = build_repo_graph(PROJECT)
    # self.subtotal() inside compute_total resolves to the method on the class.
    assert g.has_edge("compute_total", "subtotal")
    # Direction matters: subtotal does not call compute_total.
    assert not g.has_edge("subtotal", "compute_total")
    # build_invoice constructs Invoice and LineItem (class "calls").
    assert g.has_edge("build_invoice", "Invoice")
    assert g.has_edge("build_invoice", "LineItem")


def test_callers_and_blast_radius():
    g = build_repo_graph(PROJECT)
    callers = [n.name for n, _ in g.callers_of("compute_total")]
    assert "create_invoice_endpoint" in callers
    blast = [n.name for n in g.transitive_callers("compute_total")]
    assert "create_invoice_endpoint" in blast


def test_dependents_via_imports():
    g = build_repo_graph(PROJECT)
    # api.py imports billing.invoice -> it depends on invoice.py.
    assert "billing/api.py" in g.dependents_of("billing/invoice.py")
    assert "billing/api.py" in g.dependents_of("invoice")


def test_subgraph_around_symbol():
    g = build_repo_graph(PROJECT)
    sub = g.get_subgraph("compute_total", depth=1)
    names = {n["name"] for n in sub["nodes"]}
    assert {"compute_total", "subtotal", "create_invoice_endpoint"} <= names


def test_persisted_artifact_loads_and_matches_build():
    # load_repo_graph reads fixtures/graphs/<proj>.json (the published map).
    loaded = load_repo_graph(PROJECT)
    built = build_repo_graph(PROJECT)
    assert set(loaded.nodes) == set(built.nodes)
    assert loaded.has_edge("compute_total", "subtotal")


# --- summary generator ---------------------------------------------------------

def test_summary_lists_symbols_and_preserves_human_block():
    g = build_repo_graph(PROJECT, sha="deadbeef")
    existing = (
        "<!-- AUTO-GENERATED BLOCK (indexer owns this; do not hand-edit) -->\n"
        "old auto content\n"
        "<!-- END AUTO-GENERATED BLOCK -->\n\n"
        "<!-- HUMAN BLOCK (agent never touches) -->\n"
        "## Notes for QA\nKeep me.\n"
        "<!-- END HUMAN BLOCK -->\n"
    )
    md = generate_summary(g, sha="deadbeef", generated_at="2026-06-20", existing_md=existing)
    assert "compute_total" in md
    assert "sha: deadbeef" in md
    assert "create_invoice_endpoint" in md  # entry point
    assert "Keep me." in md                  # human block preserved
    assert "old auto content" not in md      # auto block regenerated, not appended


# --- graphify adapter ----------------------------------------------------------

def _graphify_sample():
    # Real graphify schema: source_file + source_location "L<n>", `relation`,
    # `confidence`, file_type, label with parens.
    return {
        "directed": True,
        "nodes": [
            {"id": "a.py::foo", "label": "foo()", "source_file": "a.py",
             "source_location": "L3", "file_type": "code"},
            {"id": "a.py::bar", "label": "bar()", "source_file": "a.py",
             "source_location": "L10", "file_type": "code"},
            {"id": "doc1", "label": "why this exists", "file_type": "rationale",
             "source_file": "a.py", "source_location": "L1"},
        ],
        "links": [
            {"source": "a.py::foo", "target": "a.py::bar", "relation": "calls",
             "confidence": "EXTRACTED"},
            {"source": "a.py::foo", "target": "doc1", "relation": "rationale_for"},  # dropped
        ],
    }


def test_graphify_adapter_directed_preserves_direction():
    g = from_graphify_json(_graphify_sample(), project="x", directed=True)
    assert set(g.nodes) == {"a.py::foo", "a.py::bar"}  # rationale node skipped
    assert g.has_edge("foo", "bar")
    assert not g.has_edge("bar", "foo")  # directed: one way only


def test_graphify_adapter_undirected_records_both_ways():
    g = from_graphify_json(_graphify_sample(), project="x", directed=False)
    # Undirected source -> we can't tell direction, so both directions exist.
    assert g.has_edge("foo", "bar") and g.has_edge("bar", "foo")


def test_adapter_on_real_graphify_run():
    """Loads the persisted output of an actual `graphify --directed` run."""
    fp = FIXTURES_DIR / "graphs" / "acme__billing-service.graphify.json"
    data = json.loads(fp.read_text(encoding="utf-8"))
    g = from_graphify_json(data, project="acme/billing-service",
                           path_strip=data["path_strip"])
    # File + rationale nodes dropped; symbols kept with repo-relative paths.
    assert "billing/invoice.py:25" in [n.ref() for n in g.resolve("compute_total")]
    # Directed call edge survives; reverse does not.
    assert g.has_edge("compute_total", "subtotal")
    assert not g.has_edge("subtotal", "compute_total")
    assert "create_invoice_endpoint" in [n.name for n, _ in g.callers_of("build_invoice")]
    # Import graph reconstructed from graphify's file-node import edges.
    assert "billing/api.py" in g.dependents_of("billing/invoice.py")


# --- end-to-end: blast radius reaches the verdict ------------------------------

def test_investigate_populates_blast_radius():
    key, text = load_fixture_ticket("RCA-101")
    v = investigate(key, text, MockGitLabClient(), get_settings())
    assert any("create_invoice_endpoint" in b for b in v.blast_radius)
