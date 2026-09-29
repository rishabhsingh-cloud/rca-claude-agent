"""Auto-post: an Auto-RCA run (with the `auto_post` switch on) posts its RCA to Jira
straight away; a reject deletes that comment, an accept adopts it, "not able to
understand" leaves it. Manual runs never auto-post."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")  # webapp extra; skip where only .[dev] is installed
from fastapi.testclient import TestClient  # noqa: E402

from rca_agent.jira import JiraClient, JiraError
from rca_agent.webapp import app as webapp
from rca_agent.webapp import autorun, db

KEY = "AUT-7"
RCA = '{"verdict_label": "Issue Accepted", "cause_categories": ["code"]}'


class _FakeJira:
    def __init__(self):
        self.calls: list[tuple] = []
        self.post_error: Exception | None = None
        self.delete_error: Exception | None = None

    def post_verdict(self, key, verdict):
        self.calls.append(("post_verdict", key))
        if self.post_error:
            raise self.post_error
        return {"id": "auto-1"}

    def add_comment_adf(self, key, adf):
        self.calls.append(("add_comment_adf", key))
        return {"id": "human-1"}

    def delete_comment(self, key, comment_id):
        self.calls.append(("delete_comment", key, comment_id))
        if self.delete_error:
            raise self.delete_error


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    fake = _FakeJira()
    monkeypatch.setattr(webapp, "_jira", lambda: fake)
    return TestClient(webapp.app), fake


def _set_auto_post(on: bool) -> None:
    autorun.save_settings({**autorun.load_settings(), "auto_post": on})


def _seed(trigger: str = "auto") -> None:
    db.upsert_ticket(KEY, "title", "desc", "2026-09-01T00:00:00.000+0530")
    if trigger == "auto":
        assert db.claim_auto_run(KEY)
    else:
        assert db.claim_running(KEY, "manual")
    db.save_rca(KEY, RCA, 3)


def _seed_posted() -> None:
    _seed("auto")
    db.set_auto_comment(KEY, "auto-1")


# --- posting on completion ------------------------------------------------------

def test_auto_run_posts_when_switch_on(env):
    _, fake = env
    _set_auto_post(True)
    _seed("auto")
    webapp._maybe_auto_post(KEY, object(), fake)
    row = db.get_ticket(KEY)
    assert fake.calls == [("post_verdict", KEY)]
    assert row["auto_comment_id"] == "auto-1" and row["auto_posted_at"]
    assert row["status"] == "rca_ready"          # still awaits human review


def test_no_post_when_switch_off(env):
    _, fake = env
    _set_auto_post(False)
    _seed("auto")
    webapp._maybe_auto_post(KEY, object(), fake)
    assert fake.calls == [] and db.get_ticket(KEY)["auto_comment_id"] is None


def test_manual_run_never_auto_posts(env):
    _, fake = env
    _set_auto_post(True)
    _seed("manual")
    webapp._maybe_auto_post(KEY, object(), fake)
    assert fake.calls == []


def test_post_failure_keeps_rca_and_records_error(env):
    _, fake = env
    _set_auto_post(True)
    fake.post_error = JiraError("Jira 403 posting comment")
    _seed("auto")
    webapp._maybe_auto_post(KEY, object(), fake)
    row = db.get_ticket(KEY)
    assert row["status"] == "rca_ready" and row["auto_comment_id"] is None
    assert "403" in row["auto_post_error"]


def test_auto_post_setting_validation():
    assert autorun.DEFAULT_SETTINGS["auto_post"] is False    # ships OFF
    assert autorun.apply_update({}, {"auto_post": True})["auto_post"] is True
    with pytest.raises(ValueError):
        autorun.apply_update({}, {"auto_post": "yes"})


# --- review decisions -------------------------------------------------------------

@pytest.mark.parametrize("route", ["accept", "accept_and_post"])
def test_accept_adopts_the_auto_comment_without_reposting(env, route):
    c, fake = env
    _seed_posted()
    r = c.post(f"/api/tickets/{KEY}/{route}")
    assert r.status_code == 200 and r.json() == {"status": "accepted"}
    row = db.get_ticket(KEY)
    assert row["status"] == "accepted" and row["comment_id"] == "auto-1"
    assert fake.calls == []


def test_reject_local_deletes_the_auto_comment(env):
    c, fake = env
    _seed_posted()
    r = c.post(f"/api/tickets/{KEY}/reject_local", json={"human_rca": "real cause"})
    assert r.status_code == 200
    row = db.get_ticket(KEY)
    assert fake.calls == [("delete_comment", KEY, "auto-1")]
    assert row["status"] == "rejected" and row["auto_comment_id"] is None


def test_reject_local_refused_when_jira_will_not_delete(env):
    c, fake = env
    fake.delete_error = JiraError("Jira 403 deleting comment")
    _seed_posted()
    r = c.post(f"/api/tickets/{KEY}/reject_local", json={})
    assert r.status_code == 502 and "delete" in r.json()["detail"]
    row = db.get_ticket(KEY)
    assert row["status"] == "rca_ready" and row["auto_comment_id"] == "auto-1"


def test_reject_with_human_rca_deletes_then_posts(env):
    c, fake = env
    _seed_posted()
    r = c.post(f"/api/tickets/{KEY}/reject", json={"human_rca": "real cause"})
    assert r.status_code == 200
    # TestClient returns before the thread may finish; wait on the job row.
    import time
    for _ in range(50):
        if db.get_job(KEY)["status"] != "running":
            break
        time.sleep(0.05)
    assert fake.calls == [("delete_comment", KEY, "auto-1"), ("add_comment_adf", KEY)]
    row = db.get_ticket(KEY)
    assert row["status"] == "rejected" and row["comment_id"] == "human-1"
    assert row["auto_comment_id"] is None


def test_unclear_keeps_the_auto_comment(env):
    c, fake = env
    _seed_posted()
    r = c.post(f"/api/tickets/{KEY}/unclear", json={"note": "too long"})
    assert r.status_code == 200
    assert fake.calls == [] and db.get_ticket(KEY)["auto_comment_id"] == "auto-1"


def test_reset_deletes_an_unreviewed_auto_comment(env):
    c, fake = env
    _seed_posted()
    assert c.post(f"/api/tickets/{KEY}/reset").status_code == 200
    assert fake.calls == [("delete_comment", KEY, "auto-1")]
    assert db.get_ticket(KEY)["auto_comment_id"] is None


def test_reset_keeps_an_accepted_comment_on_jira(env):
    c, fake = env
    _seed_posted()
    c.post(f"/api/tickets/{KEY}/accept")
    assert c.post(f"/api/tickets/{KEY}/reset").status_code == 200
    assert fake.calls == []


def test_status_exposes_auto_post_state(env):
    c, _ = env
    _seed_posted()
    d = c.get(f"/api/tickets/{KEY}/status").json()
    assert d["auto_comment_id"] == "auto-1" and d["auto_post_error"] is None


# --- JiraClient.delete_comment ---------------------------------------------------

@pytest.mark.parametrize("code,ok", [(204, True), (404, True), (403, False)])
def test_delete_comment_status_handling(code, ok):
    import httpx
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        return httpx.Response(code, text="nope")

    jc = JiraClient("https://x.atlassian.net", "e", "t")
    jc._client = httpx.Client(transport=httpx.MockTransport(handler))
    if ok:
        jc.delete_comment("AUT-7", "123")
    else:
        with pytest.raises(JiraError):
            jc.delete_comment("AUT-7", "123")
    assert seen == [("DELETE", "/rest/api/3/issue/AUT-7/comment/123")]
