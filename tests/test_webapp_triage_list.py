"""GET /api/tickets: Bug + Incident only, but a ticket once listed stays listed
after Jira changes its type or closes it (team feedback #1)."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")  # webapp extra; skip where only .[dev] is installed
from fastapi.testclient import TestClient  # noqa: E402

from rca_agent.jira import JiraError
from rca_agent.webapp import app as webapp
from rca_agent.webapp import db


def _issue(key, itype, status="Open", done=False, created="2026-09-10T10:00:00.000+0530"):
    return {"key": key, "fields": {
        "summary": f"{key} title", "description": "", "created": created,
        "issuetype": {"name": itype},
        "status": {"name": status, "statusCategory": {"key": "done" if done else "new"}},
    }}


class _FakeJira:
    """`search` answers the Bug/Incident list query from `listed`, and a
    `key in (...)` refresh from `by_key`."""

    def __init__(self):
        self.listed: list[dict] = []
        self.by_key: dict[str, dict] = {}
        self.fail_refresh = False
        self.jqls: list[str] = []

    def search(self, jql, max_results=50):
        self.jqls.append(jql)
        if jql.startswith("key in"):
            if self.fail_refresh:
                raise JiraError("Jira 503 on search")
            return [i for k, i in self.by_key.items() if k in jql]
        return self.listed


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    fake = _FakeJira()
    monkeypatch.setattr(webapp, "_jira", lambda: fake)
    return TestClient(webapp.app), fake


def test_list_query_is_bug_incident_only(client):
    c, fake = client
    assert c.get("/api/tickets", params={"work_type": "all"}).status_code == 200
    assert "issuetype in (Bug, Incident)" in fake.jqls[0]  # old param is ignored
    assert "statusCategory" not in fake.jqls[0]  # closed Bugs/Incidents show too


def test_ticket_stays_after_type_change_and_close(client):
    c, fake = client
    fake.listed = [_issue("AUT-1", "Bug")]
    assert [t["key"] for t in c.get("/api/tickets").json()] == ["AUT-1"]

    # Jira: AUT-1 became a closed Task, so the list query no longer returns it.
    fake.listed = [_issue("AUT-2", "Incident", created="2026-09-11T10:00:00.000+0530")]
    fake.by_key = {"AUT-1": _issue("AUT-1", "Task", status="Done", done=True)}
    rows = c.get("/api/tickets").json()
    assert [t["key"] for t in rows] == ["AUT-2", "AUT-1"]  # newest first
    moved = rows[1]
    assert (moved["issue_type"], moved["jira_status"], moved["jira_done"]) == ("Task", "Done", 1)


def test_never_listed_task_is_not_pinned(client):
    c, fake = client
    # Opened by key while a Task — not a Bug/Incident, so it never joins the list.
    webapp._sync_issue(_issue("AUT-9", "Task"))
    fake.listed = []
    assert c.get("/api/tickets").json() == []


def test_bug_already_done_when_first_seen_is_pinned(client):
    c, fake = client
    fake.listed = [_issue("AUT-8", "Bug", status="Done", done=True)]
    assert [t["key"] for t in c.get("/api/tickets").json()] == ["AUT-8"]
    # Later retyped to Task: Jira's list no longer has it, but it stays.
    fake.listed = []
    fake.by_key = {"AUT-8": _issue("AUT-8", "Task", status="Done", done=True)}
    assert [t["key"] for t in c.get("/api/tickets").json()] == ["AUT-8"]


def test_refresh_failure_falls_back_to_local_row(client):
    c, fake = client
    fake.listed = [_issue("AUT-1", "Bug")]
    c.get("/api/tickets")
    fake.listed, fake.fail_refresh = [], True
    rows = c.get("/api/tickets").json()
    assert [t["key"] for t in rows] == ["AUT-1"]
    assert rows[0]["issue_type"] == "Bug"  # last-known value
