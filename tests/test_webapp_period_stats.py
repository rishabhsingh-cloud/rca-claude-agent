"""The Quality tab's "This period" counter: Bug/Incident raised in a date range (never
before the counter's go-live day) and how many were accepted / rejected / left alone."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rca_agent.webapp import db

RCA = '{"verdict_label": "Issue Accepted"}'


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.set_setting(db.PERIOD_START_KEY, "2026-10-01")
    return db


def _ticket(key, created, status=None, rca=False, pin=True):
    db.upsert_ticket(key, "t", "d", f"{created}T10:00:00.000+0530",
                     issue_type="Bug" if pin else "Task", pin=pin)
    if rca:
        db.save_rca(key, RCA, 1)
    if status == "accepted":
        db.mark_accepted(key, "c1")
    elif status == "rejected":
        db.mark_rejected(key, "why", "c2")
    elif status == "unclear":
        db.mark_unclear(key, "", ["too_long"])


def test_start_is_set_once(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()                       # first start fixes the day
    first = db.get_period_start()
    db.init_db()                       # a restart must not move it
    assert first and db.get_period_start() == first


def test_start_uses_ist_day(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    with db._conn() as con:
        con.execute("DELETE FROM app_settings WHERE key = ?", (db.PERIOD_START_KEY,))
    # 20:00 UTC on 30 Sep is already 1 Oct in India.
    assert db.get_period_start(datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)) == "2026-10-01"


def test_counts_add_up(store):
    _ticket("AUT-1", "2026-10-02", "accepted", rca=True)
    _ticket("AUT-2", "2026-10-02", "rejected", rca=True)
    _ticket("AUT-3", "2026-10-03", rca=True)            # run, nobody decided
    _ticket("AUT-4", "2026-10-03")                      # not run yet
    _ticket("AUT-5", "2026-10-04", "unclear", rca=True)
    _ticket("AUT-6", "2026-10-04", rca=True, pin=False)  # a Task — never counted
    s = db.get_period_stats()
    assert (s["raised"], s["accepted"], s["rejected"], s["no_action"],
            s["not_run"], s["unclear"]) == (5, 1, 1, 1, 1, 1)


def test_before_go_live_never_counted(store):
    _ticket("AUT-1", "2026-09-30", "accepted", rca=True)   # raised before go-live
    _ticket("AUT-2", "2026-10-01", "accepted", rca=True)
    s = db.get_period_stats("2026-09-01", "")
    assert s["from"] == "2026-10-01" and s["raised"] == 1 and s["accepted"] == 1


def test_date_range_is_inclusive(store):
    for i, d in enumerate(["2026-10-01", "2026-10-05", "2026-10-06"], 1):
        _ticket(f"AUT-{i}", d)
    assert db.get_period_stats("2026-10-01", "2026-10-05")["raised"] == 2


# --- HTTP ---------------------------------------------------------------------

fastapi = pytest.importorskip("fastapi")  # webapp extra; skip where only .[dev] is installed


class _Jira:
    def __init__(self, fail=False):
        self.fail, self.jql = fail, []

    def search(self, jql, max_results=100):
        from rca_agent.jira import JiraError
        self.jql.append(jql)
        if self.fail:
            raise JiraError("down")
        return []


def _client(monkeypatch, jira):
    from fastapi.testclient import TestClient
    from rca_agent.webapp import app as webapp
    monkeypatch.setattr(webapp, "_jira", lambda: jira)
    return TestClient(webapp.app)


def test_endpoint_syncs_from_go_live_and_ignores_bad_dates(store, monkeypatch):
    jira = _Jira()
    _ticket("AUT-1", "2026-10-02", rca=True)
    r = _client(monkeypatch, jira).get("/api/quality/period",
                                       params={"from_date": "x\" OR 1=1", "to_date": "bad"})
    assert r.status_code == 200
    body = r.json()
    assert body["stale"] is False and body["raised"] == 1 and body["no_action"] == 1
    assert 'created >= "2026-10-01"' in jira.jql[0] and "OR 1=1" not in jira.jql[0]


def test_endpoint_falls_back_when_jira_down(store, monkeypatch):
    _ticket("AUT-1", "2026-10-02", "accepted", rca=True)
    r = _client(monkeypatch, _Jira(fail=True)).get("/api/quality/period")
    assert r.status_code == 200
    assert r.json()["stale"] is True and r.json()["accepted"] == 1
