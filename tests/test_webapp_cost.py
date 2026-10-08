"""Cost tab: per-run ledger, aggregation, /api/cost, and that runs are recorded
(including failures) without ever breaking the RCA."""
from __future__ import annotations

import pytest

from rca_agent.usage import RunUsage
from rca_agent.webapp import db


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return db


def _run(**kw):
    row = dict(ticket_key="AUT-1", kind="rca", trigger="manual", status="ok",
               model="claude-opus-4-8", input_tokens=10, output_tokens=20,
               cache_read_tokens=100, cache_write_tokens=5, cost_usd=1.0,
               cost_source="sdk", started_at="2026-10-01T10:00:00Z")
    row.update(kw)
    return row


def test_stats_aggregate(store):
    store.record_run(**_run())
    store.record_run(**_run(cost_usd=3.0, trigger="auto"))
    store.record_run(**_run(kind="fix", cost_usd=0.5, model="claude-sonnet-5"))
    store.record_run(**_run(ticket_key="AUT-2", status="failed", cost_usd=2.0))
    store.record_run(**_run(kind="eval", cost_usd=10.0, trigger="backfill"))
    s = store.get_cost_stats()
    assert s["total"]["runs"] == 5 and s["total"]["cost_usd"] == 16.5
    # Eval spend is kept out of the per-ticket numbers.
    assert s["tickets"]["cost_usd"] == 6.5 and s["tickets"]["distinct_tickets"] == 2
    assert s["tickets"]["avg_per_ticket_usd"] == 3.25
    assert s["by_kind"]["rca"]["avg_cost_usd"] == 2.0
    assert s["failed"]["cost_usd"] == 2.0
    assert s["by_trigger"]["auto"]["runs"] == 1
    assert s["by_model"]["claude-sonnet-5"]["cost_usd"] == 0.5
    assert s["backfill_start"] == "2026-10-01"


def test_date_filter_uses_ist_day(store):
    # 20:00 UTC on 1 Oct is 01:30 IST on 2 Oct.
    store.record_run(**_run(started_at="2026-10-01T20:00:00Z"))
    assert store.get_cost_stats("2026-10-02", "2026-10-02")["total"]["runs"] == 1
    assert store.get_cost_stats("2026-10-01", "2026-10-01")["total"]["runs"] == 0


def test_source_ref_idempotent(store):
    assert store.record_run(**_run(source_ref="phoenix:abc"))
    assert not store.record_run(**_run(source_ref="phoenix:abc"))
    assert store.get_cost_stats()["total"]["runs"] == 1


def test_api_cost_and_reset_keeps_ledger(store, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rca_agent.webapp import app as webapp
    store.upsert_ticket("AUT-1", "t", "d", "2026-10-01T00:00:00.000+0530")
    store.save_rca("AUT-1", "{}", 1)
    store.record_run(**_run())
    c = TestClient(webapp.app)
    r = c.get("/api/cost", params={"from_date": "2026-10-01", "to_date": "bad"})
    assert r.status_code == 200 and r.json()["total"]["runs"] == 1
    assert r.json()["to"] == ""   # invalid date ignored
    c.post("/api/tickets/AUT-1/reset")
    assert store.get_cost_stats()["total"]["runs"] == 1


def _fake_env(monkeypatch, webapp, run_agent):
    class _J:
        def get(self, key, drop_all_comments=True):
            return key, "text"

        def get_all_attachments(self, key):
            return {"images": [], "pdfs": []}

    monkeypatch.setattr(webapp, "_jira", lambda: _J())
    monkeypatch.setattr(webapp, "_gl", lambda: object())
    monkeypatch.setattr(webapp, "run_agent", run_agent)


def test_rca_run_recorded_ok_and_failed(store, monkeypatch):
    pytest.importorskip("fastapi")
    from rca_agent.webapp import app as webapp
    store.upsert_ticket("AUT-1", "t", "d", "2026-10-01T00:00:00.000+0530")
    store.claim_running("AUT-1", "auto")

    async def boom(*a, usage: RunUsage, **kw):
        usage.input_tokens, usage.output_tokens, usage.model = 1000, 1000, "claude-opus-4-8"
        raise RuntimeError("agent died")

    _fake_env(monkeypatch, webapp, boom)
    webapp._run_rca_background("AUT-1")
    s = store.get_cost_stats()
    r = s["recent"][0]
    assert r["status"] == "failed" and r["trigger"] == "auto"
    assert r["cost_source"] == "estimated" and r["cost_usd"] > 0
    assert store.get_ticket("AUT-1")["status"] == "failed"   # RCA failure path intact


def test_no_row_when_model_never_called(store, monkeypatch):
    pytest.importorskip("fastapi")
    from rca_agent.webapp import app as webapp
    store.upsert_ticket("AUT-1", "t", "d", "2026-10-01T00:00:00.000+0530")

    async def never(*a, **kw):
        raise RuntimeError("before any model call")

    _fake_env(monkeypatch, webapp, never)
    webapp._run_rca_background("AUT-1")
    assert store.get_cost_stats()["total"]["runs"] == 0
