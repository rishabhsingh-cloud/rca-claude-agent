"""/api/autorun/* routes and the atomic claim on the manual /rca route."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from rca_agent.webapp import app as webapp  # noqa: E402
from rca_agent.webapp import autorun, db  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    monkeypatch.setattr(autorun, "poller", None)
    # No lifespan (no `with`), so no poller thread is started for the test app.
    return TestClient(webapp.app)


def test_get_settings_returns_defaults(client):
    s = client.get("/api/autorun/settings").json()
    assert s["enabled"] is False and s["allowed_types"] == ["Bug", "Incident"]
    assert s["work_types"] == list(autorun.WORK_TYPES)
    assert s["exclude_labels"] == ["qa-found"] and "qa3" in s["exclude_env_keywords"]


def test_put_partial_update_and_watermark(client):
    r = client.put("/api/autorun/settings", json={"daily_cap": 5})
    assert r.status_code == 200 and r.json()["daily_cap"] == 5
    assert r.json()["enabled"] is False and r.json()["enabled_at"] is None
    r = client.put("/api/autorun/settings", json={"enabled": True})
    first = r.json()["enabled_at"]
    assert r.json()["enabled"] is True and first
    r = client.put("/api/autorun/settings", json={"enabled": True, "max_parallel": 1})
    assert r.json()["enabled_at"] == first          # on -> on keeps the watermark


@pytest.mark.parametrize("body", [
    {"interval_seconds": 5}, {"daily_cap": 999}, {"max_parallel": 0},
    {"allowed_types": ["Story"]}, {"allowed_types": []},
    {"exclude_env_keywords": ["bad token!"]},
])
def test_put_rejects_bad_values(client, body):
    r = client.put("/api/autorun/settings", json=body)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]


def test_status_without_poller(client):
    st = client.get("/api/autorun/status").json()
    assert st["poller_alive"] is False and st["enabled"] is False
    assert st["runs_today"] == 0 and st["in_flight"] == []
    assert client.post("/api/autorun/poll_now").status_code == 409


def test_status_with_poller(client, monkeypatch):
    class _P:
        def snapshot(self):
            return {"poller_alive": True, "state": "ok", "last_error": None,
                    "last_poll_at": "2026-09-21T08:00:00Z"}

        def run_once(self):
            self.ran = True
            return {}
    p = _P()
    monkeypatch.setattr(autorun, "poller", p)
    st = client.get("/api/autorun/status").json()
    assert st["poller_alive"] is True and st["state"] == "ok"
    assert client.post("/api/autorun/poll_now").status_code == 200 and p.ran


def test_manual_rca_claim_is_atomic(client, monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(webapp, "_run_rca_background", lambda key: started.append(key))
    db.upsert_ticket("AUT-1", "t", "d", "")
    assert client.post("/api/tickets/AUT-1/rca").json()["status"] == "started"
    assert client.post("/api/tickets/AUT-1/rca").json()["status"] == "already_running"
    row = db.get_ticket("AUT-1")
    assert row["status"] == "running" and row["trigger_source"] == "manual"
    assert row["auto_run_at"] is None
