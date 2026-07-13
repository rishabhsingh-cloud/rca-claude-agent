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
