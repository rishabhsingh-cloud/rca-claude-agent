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


def test_mark_failed_records_failure_when_no_verdict(tmp_path, monkeypatch):
    # A genuine failure with nothing to fall back to still records 'failed'.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.upsert_ticket("AUT-1", "t", "d", "2026-07-20")
    db.mark_running("AUT-1")
    db.mark_failed("AUT-1", "Agent could not finish")

    t = db.get_ticket("AUT-1")
    assert t["status"] == "failed" and t["error"] == "Agent could not finish"
