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


@app.get("/api/tickets")
def list_tickets():
    """Fetch this month's bug tickets from Jira and sync into local DB."""
    jira = _jira()
    jql = ("issuetype = Bug AND statusCategory != Done "
           "AND created >= startOfMonth() ORDER BY created DESC")
    issues = jira.search(jql, max_results=100)
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
    return store.get_all_tickets()


@app.post("/api/tickets/{key}/rca")
async def run_rca(key: str):
    """Trigger RCA on demand for a ticket. Returns the verdict."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    s = get_settings()
    jira = _jira()
    client = build_client(s)
    try:
        tkey, text = jira.get(key, drop_all_comments=True)
        attachments = jira.get_all_attachments(key)
        images = attachments["images"] or None
        # Append extracted PDF text directly into the ticket context
        if attachments["pdfs"]:
            pdf_block = "\n\n".join(
                f"--- attached PDF: {p['filename']} ---\n{p['text']}"
                for p in attachments["pdfs"]
            )
            text = text + "\n\n" + pdf_block
        # Run in a thread with its own event loop — fixes Windows asyncio subprocess issue
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(
            None,
            lambda: asyncio.run(run_agent(tkey, text, client, s, jira_mcp=False, images=images))
        )
        v = parse_verdict(raw, tkey)
        rca_json = json.dumps(v.to_dict())
        store.save_rca(key, rca_json)
        return {"status": "ok", "verdict": v.to_dict()}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, str(e))


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
