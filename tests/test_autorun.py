"""Auto-RCA poller: rules, dedupe, caps and error handling — all offline, no threads
needed beyond the stubbed run recorder (the real run pipeline is never invoked)."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from rca_agent.jira import JiraError
from rca_agent.webapp import autorun, db

ENABLED_AT = "2026-09-20T10:00:00Z"


def _issue(key="AUT-20001", itype="Bug", created="2026-09-20T15:34:11.469+0530",
           labels=None, desc="Import fails with 500", env=None, cat="new"):
    return {"key": key, "fields": {
        "summary": f"{key} summary", "issuetype": {"name": itype},
        "status": {"name": "To Do", "statusCategory": {"key": cat}},
        "created": created, "labels": labels or [],
        "description": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": desc}]}]},
        "environment": env,
    }}


def _settings(**over):
    s = {**autorun.DEFAULT_SETTINGS, "enabled": True, "enabled_at": ENABLED_AT}
    s.update(over)
    return s


class _FakeJira:
    def __init__(self, issues=None, error=None):
        self.issues = issues or []
        self.error = error
        self.calls: list[str] = []

    def search(self, jql, max_results=50):
        self.calls.append(jql)
        if self.error:
            raise self.error
        return list(self.issues)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return db


def _poller(jira, store, settings=None, blocking: threading.Event | None = None):
    launched: list[str] = []

    def run_rca(key):
        launched.append(key)
        if blocking is not None:
            blocking.wait(5)
        # emulate the real pipeline finishing: leave the row at rca_ready
        store.save_rca(key, '{"verdict_label": "x"}')

    def sync_issue(issue):
        f = issue["fields"]
        store.upsert_ticket(issue["key"], f["summary"], "", f["created"])

    p = autorun.Poller(run_rca=run_rca, sync_issue=sync_issue, jira_factory=lambda: jira,
                       store=store, has_jira=lambda: True, boot_delay_s=0)
    p.settings_override = settings
    return p, launched


def _wait_threads(prefix="rca-auto-"):
    for t in threading.enumerate():
        if t.name.startswith(prefix):
            t.join(5)


# --- pure helpers --------------------------------------------------------------

def test_build_jql_whitelists_types():
    jql = autorun.build_jql(["Bug", "Incident"], 48)
    assert 'issuetype in ("Bug", "Incident")' in jql
    assert "project = AUT" in jql and "statusCategory != Done" in jql
    assert "created >= -48h" in jql and jql.endswith("ORDER BY created ASC")
    with pytest.raises(ValueError):
        autorun.build_jql(["Bug", 'Bug" OR project = X'], 48)
    with pytest.raises(ValueError):
        autorun.build_jql([], 48)


def test_parse_jira_ts_handles_jira_offsets():
    d = autorun.parse_jira_ts("2026-08-03T16:34:11.469+0530")
    assert d is not None and d.utcoffset() == timedelta(hours=5, minutes=30)
    assert autorun.parse_jira_ts("2026-09-20T10:00:00Z") == datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    assert autorun.parse_jira_ts("garbage") is None
    assert autorun.parse_jira_ts(None) is None


@pytest.mark.parametrize("issue,expected", [
    (_issue(), None),
    (_issue(labels=["QA-Found"]), "label:QA-Found"),
    (_issue(desc="Fails on https://qa3-enterprise.mastersindia-einv.com/reco/x"),
     "host:qa3-enterprise.mastersindia-einv.com"),
    (_issue(desc="seen at qa1-enterprise.mastersindia-einv.com"),
     "host:qa1-enterprise.mastersindia-einv.com"),
    # Jira's attachment CDN must not read as a "staging" environment.
    (_issue(desc="see https://media.staging.atl-paas.net/abc/screenshot.png"), None),
    (_issue(desc="prod: https://enterprise.mastersindia-einv.com/x"), None),
    (_issue(env="UAT server"), "environment:uat"),
])
def test_is_qa_env(issue, expected):
    assert autorun.is_qa_env(issue, _settings()) == expected


def test_is_qa_env_rule_off_when_lists_empty():
    issue = _issue(labels=["qa-found"], desc="https://qa3-enterprise.mastersindia-einv.com/")
    assert autorun.is_qa_env(issue, _settings(exclude_labels=[], exclude_env_keywords=[])) is None


def test_is_eligible_rules():
    s = _settings()
    assert autorun.is_eligible(_issue(), s, None) == (True, "ok")
    assert autorun.is_eligible(_issue(itype="Defect"), s, None)[0] is False
    assert autorun.is_eligible(_issue(cat="done"), s, None)[0] is False
    ok, why = autorun.is_eligible(_issue(created="2026-09-20T09:59:00.000+0000"), s, None)
    assert not ok and "before" in why
    assert autorun.is_eligible(_issue(), _settings(enabled_at=None), None)[0] is False
    assert autorun.is_eligible(_issue(key="OPS-1"), s, None)[0] is False
    assert autorun.is_eligible(_issue(), s, {"status": "pending", "auto_run_at": "x"})[0] is False
    assert autorun.is_eligible(_issue(), s, {"status": "rca_ready", "bot_rca_json": "{}"})[0] is False
    assert autorun.is_eligible(_issue(), s, {"status": "running"})[0] is False
    assert autorun.is_eligible(_issue(), s, {"status": "pending"}) == (True, "ok")
    ok, why = autorun.is_eligible(_issue(labels=["qa-found"]), s, None)
    assert not ok and why.startswith("qa-env")


# --- tick ----------------------------------------------------------------------

def test_tick_disabled_makes_no_jira_call(store):
    jira = _FakeJira([_issue()])
    p, launched = _poller(jira, store)
    assert p.tick(_settings(enabled=False)) == 0
    assert jira.calls == [] and launched == []
    assert p.snapshot()["state"] == "disabled"


def test_tick_launches_once_and_dedupes(store):
    jira = _FakeJira([_issue("AUT-1"), _issue("AUT-2")])
    p, launched = _poller(jira, store)
    assert p.tick(_settings()) == 2
    _wait_threads()
    assert p.tick(_settings()) == 0            # same issues again -> nothing new
    _wait_threads()
    assert sorted(launched) == ["AUT-1", "AUT-2"]
    row = store.get_ticket("AUT-1")
    assert row["trigger_source"] == "auto" and row["auto_run_at"]
    assert row["status"] == "rca_ready"
    snap = p.snapshot()
    assert snap["state"] == "ok" and snap["last_found"] == 2 and snap["last_error"] is None


def test_tick_skips_qa_env_and_counts_it(store):
    jira = _FakeJira([_issue("AUT-1", labels=["qa-found"]),
                      _issue("AUT-2", desc="https://qa3-enterprise.mastersindia-einv.com/"),
                      _issue("AUT-3")])
    p, launched = _poller(jira, store)
    assert p.tick(_settings()) == 1
    _wait_threads()
    assert launched == ["AUT-3"]
    assert p.snapshot()["last_skipped_qa"] == 2
    assert store.get_ticket("AUT-1") is None     # never even synced


def test_tick_skips_ticket_already_run_manually(store):
    store.upsert_ticket("AUT-1", "t", "", "2026-09-20T15:34:11.469+0530")
    assert store.claim_running("AUT-1", "manual")
    jira = _FakeJira([_issue("AUT-1")])
    p, launched = _poller(jira, store)
    assert p.tick(_settings()) == 0 and launched == []


def test_tick_respects_parallel_cap(store):
    gate = threading.Event()
    jira = _FakeJira([_issue("AUT-1"), _issue("AUT-2"), _issue("AUT-3")])
    p, launched = _poller(jira, store, blocking=gate)
    assert p.tick(_settings(max_parallel=2)) == 2
    assert p.tick(_settings(max_parallel=2)) == 0     # both slots busy
    assert p.snapshot()["state"] == "all slots busy"
    gate.set()
    _wait_threads()
    assert p.tick(_settings(max_parallel=2)) == 1
    _wait_threads()
    assert sorted(launched) == ["AUT-1", "AUT-2", "AUT-3"]


def test_tick_respects_daily_cap(store):
    jira = _FakeJira([_issue("AUT-1"), _issue("AUT-2"), _issue("AUT-3")])
    p, launched = _poller(jira, store)
    assert p.tick(_settings(daily_cap=2, max_parallel=4)) == 2
    _wait_threads()
    assert store.count_auto_runs_today() == 2
    calls = len(jira.calls)
    assert p.tick(_settings(daily_cap=2, max_parallel=4)) == 0
    assert len(jira.calls) == calls                  # cap reached -> no Jira call
    assert p.snapshot()["state"] == "daily cap reached"


def test_daily_cap_day_boundary_is_ist():
    # 23:30 IST yesterday == 18:00Z; 00:30 IST today == 19:00Z (same UTC date!)
    now = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)      # 01:30 IST 21 Sep
    assert db.ist_day_start_utc(now) == "2026-09-20T18:30:00Z"


def test_count_auto_runs_today_uses_ist_day(store):
    now = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)
    with store._conn() as con:
        con.execute("INSERT INTO reviews (key, auto_run_at) VALUES ('AUT-1', '2026-09-20T18:00:00Z')")
        con.execute("INSERT INTO reviews (key, auto_run_at) VALUES ('AUT-2', '2026-09-20T19:00:00Z')")
    assert store.count_auto_runs_today(now) == 1


def test_run_once_survives_jira_error_and_recovers(store):
    jira = _FakeJira(error=JiraError("Jira 401 on search"))
    p, launched = _poller(jira, store)
    autorun.save_settings(_settings(), store)
    p.run_once()
    snap = p.snapshot()
    assert snap["state"] == "error" and "Jira 401" in snap["last_error"]
    jira.error = None
    jira.issues = [_issue("AUT-9")]
    p.run_once()
    _wait_threads()
    assert p.snapshot()["last_error"] is None and launched == ["AUT-9"]


def test_tick_reports_missing_jira_config(store):
    p = autorun.Poller(run_rca=lambda k: None, sync_issue=lambda i: None,
                       jira_factory=lambda: None, store=store, has_jira=lambda: False)
    assert p.tick(_settings()) == 0
    assert "Jira not configured" in p.snapshot()["last_error"]


# --- settings ------------------------------------------------------------------

def test_apply_update_validates_and_stamps_watermark():
    now = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
    cur = dict(autorun.DEFAULT_SETTINGS)
    new = autorun.apply_update(cur, {"enabled": True, "allowed_types": ["Bug", "Defect", "Bug"],
                                     "exclude_labels": ["QA-Found", " uat "]}, now=now)
    assert new["enabled"] and new["enabled_at"] == "2026-09-21T08:00:00Z"
    assert new["allowed_types"] == ["Bug", "Defect"]
    assert new["exclude_labels"] == ["qa-found", "uat"]
    # on -> on keeps the original watermark
    again = autorun.apply_update(new, {"enabled": True, "daily_cap": 5}, now=now + timedelta(days=1))
    assert again["enabled_at"] == "2026-09-21T08:00:00Z" and again["daily_cap"] == 5
    for bad in ({"interval_seconds": 5}, {"daily_cap": 999}, {"max_parallel": 0},
                {"allowed_types": ["Story"]}, {"allowed_types": []},
                {"exclude_env_keywords": ["has space"]}, {"max_parallel": True}):
        with pytest.raises(ValueError):
            autorun.apply_update(cur, bad)


def test_settings_roundtrip_merges_defaults(store):
    assert autorun.load_settings(store) == autorun.DEFAULT_SETTINGS
    autorun.save_settings({"enabled": True, "daily_cap": 3}, store)
    s = autorun.load_settings(store)
    assert s["enabled"] is True and s["daily_cap"] == 3
    assert s["allowed_types"] == ["Bug", "Incident"]    # default kept
