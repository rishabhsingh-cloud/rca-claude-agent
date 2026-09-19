"""GET /api/tickets/{key}: open one ticket straight from Jira, bypassing the
list's filters and its 100-newest cap (the AUT-10001 case)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rca_agent.jira import JiraError
from rca_agent.webapp import app as webapp
from rca_agent.webapp import db


class _FakeJira:
    def __init__(self, issues: dict[str, dict]):
        self.issues = issues
        self.calls: list[str] = []

    def get_issue(self, key: str) -> dict:
        self.calls.append(key)
        if key not in self.issues:
            raise JiraError(f"issue not found: {key}")
        return self.issues[key]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    fake = _FakeJira({
        "AUT-10001": {
            "key": "AUT-10001",
            "fields": {
                "summary": "Advance-role user unable to Accept in Reconciliation",
                "description": {"type": "doc", "version": 1, "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "steps"}]}]},
                "created": "2026-08-03T16:34:11.469+0530",
            },
        },
    })
    monkeypatch.setattr(webapp, "_jira", lambda: fake)
    return TestClient(webapp.app), fake


@pytest.mark.parametrize("typed,expected", [
    ("AUT-10001", "AUT-10001"),
    ("aut-10001", "AUT-10001"),
    ("  10001 ", "AUT-10001"),
    ("reconciliation", None),
    ("AUT-", None),
    ("", None),
    ("XYZ-1", None),
])
def test_normalize_ticket_key(typed, expected):
    assert webapp.normalize_ticket_key(typed) == expected


def test_lookup_by_key_syncs_and_returns_list_row_shape(client):
    c, fake = client
    r = c.get("/api/tickets/AUT-10001")
    assert r.status_code == 200
    row = r.json()
    assert row["key"] == "AUT-10001"
    assert row["title"].startswith("Advance-role user")
    assert row["description"].strip() == "steps"  # ADF flattened to text
    assert row["status"] == "pending"             # fresh ticket, no RCA yet
    assert fake.calls == ["AUT-10001"]
    # Persisted, so later /rca, /status, etc. on this key work as for any list row.
    assert db.get_ticket("AUT-10001")["title"] == row["title"]


def test_bare_number_is_treated_as_aut_key(client):
    c, fake = client
    r = c.get("/api/tickets/10001")
    assert r.status_code == 200
    assert r.json()["key"] == "AUT-10001"
    assert fake.calls == ["AUT-10001"]


def test_unknown_key_is_404(client):
    c, _ = client
    r = c.get("/api/tickets/AUT-9999999")
    assert r.status_code == 404
    assert "AUT-9999999" in r.json()["detail"]


def test_non_key_text_is_400_and_never_hits_jira(client):
    c, fake = client
    r = c.get("/api/tickets/not-a-key")
    assert r.status_code == 400
    assert fake.calls == []


def test_lookup_keeps_existing_rca_state(client):
    c, _ = client
    # A ticket already reviewed locally keeps its state when re-fetched by key;
    # upsert only refreshes title/description.
    db.upsert_ticket("AUT-10001", "old title", "old desc", "")
    db.mark_running("AUT-10001")
    r = c.get("/api/tickets/AUT-10001")
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    assert r.json()["title"].startswith("Advance-role user")
