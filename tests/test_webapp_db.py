"""Regression tests for the webapp SQLite store."""
from __future__ import annotations

import sqlite3

import pytest

from rca_agent.webapp import db


def test_conn_commits_and_closes(tmp_path, monkeypatch):
    # Regression: _conn() used to `return sqlite3.connect(...)`, and `with` on a
    # sqlite connection commits but does NOT close it -> one leaked fd per call,
    # which exhausted the process (OSError [Errno 24]) and took the webapp down.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")

    with db._conn() as con:
        con.execute("CREATE TABLE t (x)")
        con.execute("INSERT INTO t VALUES (1)")

    # Commit semantics preserved: a fresh connection sees the row.
    with db._conn() as con2:
        assert con2.execute("SELECT x FROM t").fetchone()[0] == 1

    # ...and the first connection is CLOSED — using it now raises (fd released).
    with pytest.raises(sqlite3.ProgrammingError):
        con.execute("SELECT 1")


def test_background_job_lifecycle(tmp_path, monkeypatch):
    # The slow synchronous endpoints (fix / accept_post / reject) now run in a
    # background thread tracked by these job_* columns, so the POST returns
    # instantly and the reverse proxy can't time out a long-open request.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.upsert_ticket("AUT-1", "t", "d", "2026-07-20")

    # First claim wins; a second while running is refused (no double-launch).
    assert db.start_job("AUT-1", "fix") is True
    assert db.start_job("AUT-1", "fix") is False
    job = db.get_job("AUT-1")
    assert job["kind"] == "fix" and job["status"] == "running"

    db.finish_job("AUT-1", {"fixable": True})
    job = db.get_job("AUT-1")
    assert job["status"] == "done" and job["result"] == {"fixable": True}

    # A finished job frees the slot for the next action.
    assert db.start_job("AUT-1", "accept_post") is True
    db.fail_job("AUT-1", "Jira unreachable")
    job = db.get_job("AUT-1")
    assert job["status"] == "failed" and job["error"] == "Jira unreachable"

    assert db.get_job("MISSING") is None


def test_running_job_recovered_on_restart(tmp_path, monkeypatch):
    # A job whose thread died with the process must not stay 'running' forever.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.upsert_ticket("AUT-2", "t", "d", "2026-07-20")
    db.start_job("AUT-2", "reject")

    db.init_db()  # simulates a service restart

    job = db.get_job("AUT-2")
    assert job["status"] == "failed" and "restart" in job["error"]


def test_reset_clears_job_state(tmp_path, monkeypatch):
    # Re-running an RCA must wipe any leftover job row so a stale fix/decision
    # can't reattach to the fresh verdict.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.upsert_ticket("AUT-3", "t", "d", "2026-07-20")
    db.start_job("AUT-3", "fix")
    db.finish_job("AUT-3", {"fixable": False})

    with db._conn() as con:
        con.execute("UPDATE reviews SET job_kind=NULL, job_status=NULL, "
                    "job_error=NULL, job_result=NULL WHERE key=?", ("AUT-3",))

    assert db.get_job("AUT-3") == {"kind": None, "status": None,
                                   "error": None, "result": None}


def test_mark_failed_never_clobbers_existing_verdict(tmp_path, monkeypatch):
    # AUT-9864: a re-run that times out must NOT wipe a previously-good verdict.
    # mark_running only flips status->running (the verdict stays), so the guard keys
    # on bot_rca_json, not on status.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.upsert_ticket("AUT-9864", "t", "d", "2026-07-20")

    db.save_rca("AUT-9864", '{"triage": "real_bug"}', turns_used=7)  # first run: good
    db.mark_running("AUT-9864")                                       # re-run starts
    db.mark_failed("AUT-9864", "Timed out")                          # re-run times out

    t = db.get_ticket("AUT-9864")
    assert t["status"] == "rca_ready"                 # NOT 'failed'
    assert t["bot_rca_json"] == '{"triage": "real_bug"}'  # verdict preserved
    assert not t["status"] == "running"               # and not left wedged at running


def test_human_rca_draft_saves_without_posting(tmp_path, monkeypatch):
    # QA ask: write your own RCA and SAVE it locally, without posting to Jira or
    # touching the ticket status. Standalone — works with no bot RCA present.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.upsert_ticket("AUT-1", "t", "d", "2026-07-22")

    db.save_human_rca_draft("AUT-1", "my root cause")
    t = db.get_ticket("AUT-1")
    assert t["human_rca_draft"] == "my root cause"
    assert t["status"] == "pending"            # no status change, no posting

    db.save_human_rca_draft("AUT-1", "revised")  # re-save updates in place
    assert db.get_ticket("AUT-1")["human_rca_draft"] == "revised"

    db.clear_human_rca_draft("AUT-1")            # cleared once posted
    assert db.get_ticket("AUT-1")["human_rca_draft"] is None


def test_mark_failed_records_failure_when_no_verdict(tmp_path, monkeypatch):
    # A genuine failure with nothing to fall back to still records 'failed'.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.upsert_ticket("AUT-1", "t", "d", "2026-07-20")
    db.mark_running("AUT-1")
    db.mark_failed("AUT-1", "Agent could not finish")

    t = db.get_ticket("AUT-1")
    assert t["status"] == "failed" and t["error"] == "Agent could not finish"


def test_claim_running_is_atomic_and_claim_auto_run_is_once_only(tmp_path, monkeypatch):
    # Auto-RCA: the poller and the Run button both go through conditional UPDATEs,
    # so exactly one of two concurrent claims wins; and a ticket is auto-run at most
    # once, ever — a human /reset (which keeps auto_run_at) must not re-arm it.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.upsert_ticket("AUT-5", "t", "d", "2026-09-20T10:00:00.000+0530")

    assert db.claim_running("AUT-5", "manual") is True
    assert db.claim_running("AUT-5", "manual") is False       # already running
    assert db.claim_auto_run("AUT-5") is False                # not 'pending'
    db.mark_failed("AUT-5", "boom")                           # back to failed, no verdict

    db.upsert_ticket("AUT-6", "t", "d", "2026-09-20T10:00:00.000+0530")
    assert db.claim_auto_run("AUT-6") is True
    row = db.get_ticket("AUT-6")
    assert row["status"] == "running" and row["trigger_source"] == "auto"
    assert row["auto_run_at"] and db.count_auto_runs_today() == 1
    assert db.claim_auto_run("AUT-6") is False                # in flight
    db.save_rca("AUT-6", "{}")
    assert db.claim_auto_run("AUT-6") is False                # has a verdict

    # /reset semantics: verdict cleared, status pending, auto_run_at KEPT.
    with db._conn() as con:
        con.execute("UPDATE reviews SET bot_rca_json=NULL, status='pending' WHERE key='AUT-6'")
    assert db.claim_auto_run("AUT-6") is False                # never auto-run twice
    assert db.claim_running("AUT-6", "manual") is True        # a human still can
    assert db.running_auto_keys() == []


def test_settings_kv_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    assert db.get_setting("autorun", {"x": 1}) == {"x": 1}
    db.set_setting("autorun", {"enabled": True, "allowed_types": ["Bug"]})
    assert db.get_setting("autorun") == {"enabled": True, "allowed_types": ["Bug"]}
    db.set_setting("autorun", {"enabled": False})
    assert db.get_setting("autorun") == {"enabled": False}


def test_dashboard_pin_is_sticky(tmp_path, monkeypatch):
    # Team feedback #1: a Bug/Incident stays on the Triage list after Jira changes
    # its type or closes it. The flag only ever goes 0 -> 1.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.upsert_ticket("AUT-1", "t", "d", "2026-09-10T10:00:00.000+0530",
                     issue_type="Bug", jira_status="Open", pin=True)
    db.upsert_ticket("AUT-1", "t2", "d", "2026-09-10T10:00:00.000+0530",
                     issue_type="Task", jira_status="Done", jira_done=True, pin=False)
    row = db.get_ticket("AUT-1")
    assert row["on_dashboard"] == 1
    assert (row["issue_type"], row["jira_status"], row["jira_done"]) == ("Task", "Done", 1)
    # A never-pinned ticket is not on the list.
    db.upsert_ticket("AUT-2", "t", "d", "2026-09-10", issue_type="Task", pin=False)
    assert [t["key"] for t in db.get_dashboard_tickets()] == ["AUT-1"]


def test_dashboard_tickets_date_bounds_and_aut_only(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    for key, created in [("AUT-1", "2026-09-01T09:00:00.000+0530"),
                         ("AUT-2", "2026-09-15T23:30:00.000+0530"),
                         ("GST-3", "2026-09-10T09:00:00.000+0530")]:
        db.upsert_ticket(key, "t", "d", created, issue_type="Bug", pin=True)
    keys = [t["key"] for t in db.get_dashboard_tickets("2026-09-05", "2026-09-15")]
    assert keys == ["AUT-2"]  # 'to' day inclusive; non-AUT row skipped


def test_dashboard_backfill_flags_worked_on_rows_only(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.upsert_ticket("AUT-1", "t", "d", "2026-09-01")
    db.upsert_ticket("AUT-2", "t", "d", "2026-09-01")
    db.save_rca("AUT-1", "{}", 1)
    with db._conn() as con:  # simulate a DB from before the column existed
        con.execute("UPDATE reviews SET on_dashboard = 0")
    db.init_db()
    assert [t["key"] for t in db.get_dashboard_tickets()] == ["AUT-1"]
