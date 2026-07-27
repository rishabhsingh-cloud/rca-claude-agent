"""Tests for find_screen — the image-ticket → screen/API/backend localizer."""
from __future__ import annotations

import json

import rca_agent.screen_index as si
from rca_agent.screen_index import find_screen

_ROWS = [
    {"area": "Reconcile", "modules": ["GSTR2BReconcile", "IMSPRReconcile"],
     "route_prefixes": ["/reconciliation"], "backends": ["gst-enterprise-service"],
     "api_calls": ["POST /reconciliation/accept-action", "GET /reconciliation/get_job_detail"],
     "fingerprint_sample": ["Reconciliation", "2B vs Purchase", "Summary View",
                            "Amount as Per Purchase Records"]},
    {"area": "Returns", "modules": ["GSTR1", "GSTR3B"],
     "route_prefixes": ["/gst-returns/gstr-1", "/gst-returns/3b"],
     "backends": ["gst-enterprise-service"], "api_calls": ["GET /gst-returns/gstr-1"],
     "fingerprint_sample": ["GSTR-1", "Return Filing", "Dashboard"]},
]


def _write_index(tmp_path):
    (tmp_path / "frontend_areas.jsonl").write_text(
        "\n".join(json.dumps(r) for r in _ROWS), encoding="utf-8")


def test_find_screen_routes_reco_query(tmp_path, monkeypatch):
    # A reco screenshot's text must route to the Reconcile area — NOT Returns, even though
    # "GSTR" is shared (coverage-based scoring counts distinct query tokens, so the many
    # gstr-* Returns terms don't out-vote the distinctive "reconciliation"/"purchase").
    monkeypatch.setenv("RCA_INDEX_DIR", str(tmp_path))
    _write_index(tmp_path)
    r = find_screen("Screenshot: GSTR-2B vs Purchase reconciliation, Summary View tab; "
                    "UI total and report total do not match")
    assert r["matches"], r
    top = r["matches"][0]
    assert top["area"] == "Reconcile"
    assert "gst-enterprise-service" in top["backends"]
    assert any("reconciliation" in a.lower() for a in top["api_calls"])


def test_find_screen_routes_returns_query(tmp_path, monkeypatch):
    monkeypatch.setenv("RCA_INDEX_DIR", str(tmp_path))
    _write_index(tmp_path)
    r = find_screen("GSTR-1 return filing dashboard — invoices missing")
    assert r["matches"] and r["matches"][0]["area"] == "Returns"


def test_find_screen_empty_text_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("RCA_INDEX_DIR", str(tmp_path))
    _write_index(tmp_path)
    assert "error" in find_screen("   ")


def test_find_screen_missing_index_errors(tmp_path, monkeypatch):
    # No index in index_dir() AND none at the dev fallback (ROOT) -> a clean error, not a crash.
    monkeypatch.setenv("RCA_INDEX_DIR", str(tmp_path))       # empty dir
    monkeypatch.setattr(si, "ROOT", tmp_path)                 # fallback also empty
    assert "error" in find_screen("reconciliation 2b")
