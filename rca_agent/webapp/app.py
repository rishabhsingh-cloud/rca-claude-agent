"""FastAPI web app — human-in-the-loop RCA review UI."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..agent import parse_verdict, run_agent
from ..config import get_settings
from ..gitlab_client import build_client
from ..jira import JiraClient
from ..schema import verdict_to_adf
from ..tickets import build_ticket_source
from ..verify import verify_verdict
from . import db as store

app = FastAPI(title="RCA Review")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

store.init_db()


def _jira() -> JiraClient:
    s = get_settings()
    if not s.has_jira:
        raise HTTPException(500, "Jira not configured")
    return JiraClient(s.jira_url, s.jira_email, s.jira_token)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


import re as _re

_DATE_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")


@app.get("/api/tickets")
def list_tickets(from_date: str = "", to_date: str = "", include_resolved: bool = False):
    """Fetch AUT bug tickets from Jira (optionally date-filtered) and sync locally.

    from_date / to_date are YYYY-MM-DD (inclusive). Anything not matching that
    exact shape is ignored, so the values can't be used to inject JQL.
    include_resolved=True drops the 'not Done' filter so closed tickets show too.
    """
    jira = _jira()
    clauses = ["project = AUT", "issuetype = Bug"]
    if not include_resolved:
        clauses.append("statusCategory != Done")
    if _DATE_RE.match(from_date):
        clauses.append(f'created >= "{from_date}"')
    if _DATE_RE.match(to_date):
        # include the whole 'to' day
        clauses.append(f'created <= "{to_date} 23:59"')
    jql = " AND ".join(clauses) + " ORDER BY created DESC"
    issues = jira.search(jql, max_results=100)
    keys = []
    for i in issues:
        key = i["key"]
        fields = i.get("fields", {})
        title = fields.get("summary", "")
        desc = fields.get("description", "") or ""
        if isinstance(desc, dict):
            from ..tickets import flatten_adf
            desc = flatten_adf(desc)
        created_at = fields.get("created", "")
        store.upsert_ticket(key, title, desc, created_at)
        keys.append(key)
    # Return ONLY the tickets just fetched (AUT + this date range) with their
    # stored RCA state — not the whole DB, which still holds old non-AUT rows
    # from earlier syncs.
    return [t for k in keys if (t := store.get_ticket(k))]


# How long a single investigation may run before we give up and mark it failed.
# Cross-service / image tickets are slow; keep this generous but bounded so a
# hung run can never wedge a row at 'running' forever.
RCA_TIMEOUT_SECONDS = 600


def _run_rca_background(key: str) -> None:
    """Run RCA in a background thread — saves result to DB when done."""
    import asyncio
    import traceback
    from ..agent import AgentRunError
    s = get_settings()
    try:
        jira = JiraClient(s.jira_url, s.jira_email, s.jira_token)
        client = build_client(s)
        tkey, text = jira.get(key, drop_all_comments=True)
        attachments = jira.get_all_attachments(key)
        images = attachments["images"] or None
        if attachments["pdfs"]:
            pdf_block = "\n\n".join(
                f"--- attached PDF: {p['filename']} ---\n{p['text']}"
                for p in attachments["pdfs"]
            )
            text = text + "\n\n" + pdf_block
        # Hard ceiling: a hung/runaway agent must not wedge the DB row at
        # 'running' forever — on timeout we fall through to mark_failed.
        raw, turns_used = asyncio.run(
            asyncio.wait_for(
                run_agent(tkey, text, client, s, jira_mcp=False, images=images),
                timeout=RCA_TIMEOUT_SECONDS,
            )
        )
        v = parse_verdict(raw, tkey)
        vr = verify_verdict(v, client)
        if vr.total:
            note = vr.as_note()
            v.notes = (v.notes + "\n\n" + note).strip() if v.notes else note
            v.confidence = vr.downgraded_confidence(v.confidence)
        store.save_rca(key, json.dumps(v.to_dict()), turns_used=turns_used)
    except (asyncio.TimeoutError, TimeoutError):
        mins = RCA_TIMEOUT_SECONDS // 60
        store.mark_failed(key, f"Timed out after {mins} minutes — the investigation "
                               "ran too long. Try again, or check the ticket has "
                               "enough detail to localize.")
    except AgentRunError as e:
        # Infra failure (out of credits, rate limit, overload, no verdict).
        store.mark_failed(key, f"Agent could not finish: {str(e)[:240]}")
    except Exception as e:
        traceback.print_exc()
        store.mark_failed(key, f"{type(e).__name__}: {str(e)[:240]}")


@app.post("/api/tickets/{key}/rca")
def run_rca(key: str):
    """Start RCA in background. Returns immediately — poll /status for result."""
    import threading
    ticket = store.get_ticket(key)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if ticket["status"] == "running":
        return {"status": "already_running"}
    store.mark_running(key)
    t = threading.Thread(target=_run_rca_background, args=(key,), daemon=True)
    t.start()
    return {"status": "started"}


@app.get("/api/tickets/{key}/status")
def rca_status(key: str):
    """Poll this to check if RCA is done."""
    ticket = store.get_ticket(key)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    result = {"status": ticket["status"]}
    if ticket["status"] == "rca_ready" and ticket.get("bot_rca_json"):
        result["verdict"] = json.loads(ticket["bot_rca_json"])
        result["turns_used"] = ticket.get("turns_used")
    elif ticket["status"] == "failed":
        result["error"] = ticket.get("error")
    return result


@app.get("/api/tickets/{key}/raw_attachments")
def raw_attachments(key: str):
    """Debug: return raw attachment metadata from Jira."""
    jira = _jira()
    issue = jira.get_issue(key)
    return issue.get("fields", {}).get("attachment") or []


@app.get("/api/tickets/{key}/attachments")
def get_attachments(key: str):
    """Fetch images and PDFs attached to a Jira ticket."""
    jira = _jira()
    try:
        return jira.get_all_attachments(key)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/tickets/{key}/reset")
def reset_rca(key: str):
    """Clear stored RCA so it can be re-run."""
    with store._conn() as con:
        con.execute("UPDATE reviews SET bot_rca_json=NULL, status='pending' WHERE key=?", (key,))
    return {"status": "reset"}


class AcceptRequest(BaseModel):
    pass


class RejectRequest(BaseModel):
    human_rca: str


@app.post("/api/tickets/{key}/accept")
def accept(key: str):
    """Mark as accepted locally without posting to Jira."""
    ticket = store.get_ticket(key)
    if not ticket or not ticket.get("bot_rca_json"):
        raise HTTPException(400, "No RCA found for this ticket — run RCA first")
    if ticket["status"] in ("accepted", "rejected"):
        raise HTTPException(400, "Already reviewed")
    store.mark_accepted(key, "")
    return {"status": "accepted"}


@app.post("/api/tickets/{key}/accept_and_post")
def accept_and_post(key: str):
    """Post the bot's RCA to Jira and mark as accepted."""
    ticket = store.get_ticket(key)
    if not ticket or not ticket.get("bot_rca_json"):
        raise HTTPException(400, "No RCA found for this ticket — run RCA first")
    if ticket["status"] in ("accepted", "rejected"):
        raise HTTPException(400, "Already reviewed")
    jira = _jira()
    from ..agent import parse_verdict
    v = parse_verdict(ticket["bot_rca_json"], key)
    res = jira.post_verdict(key, v)
    store.mark_accepted(key, res.get("id", ""))
    return {"status": "accepted", "comment_id": res.get("id")}


@app.post("/api/tickets/{key}/reject")
def reject(key: str, body: RejectRequest):
    """Post the human's RCA to Jira and mark as rejected."""
    if not body.human_rca.strip():
        raise HTTPException(400, "Human RCA cannot be empty")
    ticket = store.get_ticket(key)
    if ticket and ticket["status"] in ("accepted", "rejected"):
        raise HTTPException(400, "Already reviewed")
    jira = _jira()
    adf = {
        "type": "doc", "version": 1,
        "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": body.human_rca}
            ]},
            {"type": "paragraph", "content": [
                {"type": "text", "text": "(Human RCA — Automated RCA)",
                 "marks": [{"type": "code"}]}
            ]},
        ]
    }
    res = jira.add_comment_adf(key, adf)
    store.mark_rejected(key, body.human_rca, res.get("id", ""))
    return {"status": "rejected", "comment_id": res.get("id")}


@app.get("/api/scoreboard")
def scoreboard():
    return store.get_scoreboard()
