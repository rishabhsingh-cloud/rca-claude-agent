"""Tests for the New Relic tool — the GSTIN-scoped error lookup (Roadmap step 1).

Monkeypatches `_run_nrql` so no live NR is needed; asserts the NRQL we build.
"""
from __future__ import annotations

import rca_agent.newrelic as nr


def _capture(monkeypatch):
    seen = {}
    monkeypatch.setattr(nr, "_run_nrql", lambda nrql: seen.setdefault("nrql", nrql) or {"results": []})
    return seen


def test_gstin_mode_is_cross_service_transaction_facet(monkeypatch):
    seen = _capture(monkeypatch)
    nr.search_nr_errors("gst-enterprise-service", gstin="27AAAAA0000A1Z5")
    q = seen["nrql"]
    # queries Transaction (has the header), NOT TransactionError (doesn't)
    assert "FROM Transaction " in q and "TransactionError" not in q
    # filters by the gstin, faceted by app (cross-service), errors only
    assert "27AAAAA0000A1Z5" in q and "request.header.Gstin" in q
    assert "FACET appName" in q and "error IS true" in q
    # NOT pinned to a single appName (a customer's errors can be on any app)
    assert "appName = '" not in q and "appName='" not in q
    # PII-safe: no free-text fields surfaced
    assert "request.uri" not in q and "error.message" not in q


def test_default_mode_uses_transaction_error(monkeypatch):
    seen = _capture(monkeypatch)
    nr.search_nr_errors("gst-enterprise-service")   # no gstin
    q = seen["nrql"]
    assert "FROM TransactionError" in q
    assert "request.header.Gstin" not in q


def test_gstin_only_used_as_filter_not_returned(monkeypatch):
    # The gstin must appear only in the WHERE clause, never in SELECT/FACET (never surfaced).
    seen = _capture(monkeypatch)
    nr.search_nr_errors("x", gstin="27AAAAA0000A1Z5")
    q = seen["nrql"]
    where = q[q.index("WHERE"):]
    assert "27AAAAA0000A1Z5" in where
    assert "27AAAAA0000A1Z5" not in q[:q.index("WHERE")]
