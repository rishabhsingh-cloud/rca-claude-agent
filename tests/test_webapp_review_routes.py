"""Review decisions over HTTP: the local-only reject (`POST /reject_local`) must record
the miss without ever touching Jira, mirror `/accept`'s guards, and count in Quality."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")  # webapp extra; skip where only .[dev] is installed
from fastapi.testclient import TestClient  # noqa: E402

from rca_agent.webapp import app as webapp
from rca_agent.webapp import db

KEY = "AUT-1"
RCA = '{"verdict_label": "Issue Accepted", "cause_categories": ["code"]}'


class _RecordingJira:
    """Records every write so a test can assert nothing reached Jira."""

    def __init__(self):
        self.calls: list[tuple] = []

    def add_comment_adf(self, key, adf):
        self.calls.append(("add_comment_adf", key))
        return {"id": "c1"}

    def post_verdict(self, key, verdict):
        self.calls.append(("post_verdict", key))
        return {"id": "c2"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    fake = _RecordingJira()
    monkeypatch.setattr(webapp, "_jira", lambda: fake)
    # No `with` → no lifespan → the Auto-RCA poller thread never starts.
    return TestClient(webapp.app), fake


def _seed_with_rca(key: str = KEY) -> None:
    db.upsert_ticket(key, "title", "desc", "2026-09-01T00:00:00.000+0530")
    db.save_rca(key, RCA, 3)  # bot_rca_json set, status -> rca_ready


def test_reject_local_records_miss_without_jira(client):
    c, fake = client
    _seed_with_rca()
    r = c.post(f"/api/tickets/{KEY}/reject_local", json={"human_rca": "  real cause  "})
    assert r.status_code == 200 and r.json() == {"status": "rejected"}
    row = db.get_ticket(KEY)
    assert row["status"] == "rejected"
    assert row["human_rca"] == "real cause"
    assert row["comment_id"] == ""          # nothing posted
    assert row["human_rca_draft"] == "real cause"  # kept: re-surfaces after a re-run
    assert fake.calls == []
    assert db.get_job(KEY)["status"] is None  # synchronous, no background job


def test_reject_local_empty_text_leaves_draft_alone(client):
    c, _ = client
    _seed_with_rca()
    db.save_human_rca_draft(KEY, "my notes")
    r = c.post(f"/api/tickets/{KEY}/reject_local", json={"human_rca": ""})
    assert r.status_code == 200
    row = db.get_ticket(KEY)
    assert row["status"] == "rejected"
    assert row["human_rca"] == ""
    assert row["human_rca_draft"] == "my notes"


def test_reject_local_body_is_optional(client):
    c, _ = client
    _seed_with_rca()
    assert c.post(f"/api/tickets/{KEY}/reject_local", json={}).status_code == 200


def test_reject_local_requires_an_rca(client):
    c, _ = client
    db.upsert_ticket(KEY, "title", "desc", "2026-09-01T00:00:00.000+0530")  # no RCA yet
    r = c.post(f"/api/tickets/{KEY}/reject_local", json={"human_rca": "x"})
    assert r.status_code == 400 and "run RCA first" in r.json()["detail"]
    assert c.post("/api/tickets/AUT-404/reject_local", json={}).status_code == 400


def test_reject_local_refuses_already_reviewed(client):
    c, _ = client
    _seed_with_rca()
    db.mark_accepted(KEY, "")
    r = c.post(f"/api/tickets/{KEY}/reject_local", json={"human_rca": "x"})
    assert r.status_code == 400 and r.json()["detail"] == "Already reviewed"
    assert db.get_ticket(KEY)["status"] == "accepted"


@pytest.mark.parametrize("route", ["reject_local", "accept"])
def test_local_decision_waits_for_in_flight_jira_post(client, route):
    c, _ = client
    _seed_with_rca()
    assert db.start_job(KEY, "reject")  # a Jira post is running in the background
    r = c.post(f"/api/tickets/{KEY}/{route}", json={"human_rca": "x"})
    assert r.status_code == 409
    assert db.get_ticket(KEY)["status"] == "rca_ready"
    db.finish_job(KEY)
    assert c.post(f"/api/tickets/{KEY}/{route}", json={"human_rca": "x"}).status_code == 200


def test_reject_local_counts_in_quality(client):
    c, _ = client
    _seed_with_rca()
    c.post(f"/api/tickets/{KEY}/reject_local", json={})
    q = c.get("/api/quality").json()
    assert (q["rejected"], q["accepted"], q["reviewed"]) == (1, 0, 1)
    s = c.get("/api/scoreboard").json()
    assert (s["rejected"], s["accepted"]) == (1, 0)


def test_posting_reject_still_requires_text(client):
    """Regression lock: the Jira-posting route is untouched."""
    c, fake = client
    _seed_with_rca()
    r = c.post(f"/api/tickets/{KEY}/reject", json={"human_rca": "  "})
    assert r.status_code == 400 and fake.calls == []
