"""FastAPI web app — human-in-the-loop RCA review UI."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..agent import blame_dropped_note, parse_verdict, run_agent
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


# One long-lived client per process, reused across requests and the RCA background
# thread. Both wrap a thread-safe httpx.Client with its own connection pool — creating
# a fresh one per request (the old behavior) leaked a pool/socket on every call and
# eventually exhausted the process's fd limit. httpx.Client is safe to share.
_jira_client: JiraClient | None = None
_gl_client = None


def _jira() -> JiraClient:
    global _jira_client
    s = get_settings()
    if not s.has_jira:
        raise HTTPException(500, "Jira not configured")
    if _jira_client is None:
        _jira_client = JiraClient(s.jira_url, s.jira_email, s.jira_token)
    return _jira_client


def _gl():
    """Cached GitLab client (mock or live), reused across requests."""
    global _gl_client
    if _gl_client is None:
        _gl_client = build_client(get_settings())
    return _gl_client


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


import re as _re

_DATE_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The work types (Jira issue types) the AUT project defines. Whitelisted so a
# value picked in the UI dropdown is interpolated into JQL only if it's a known
# type — the value can never be used to inject arbitrary JQL.
_WORK_TYPES = ("New Feature", "Task", "Bug", "Epic", "Subtask",
               "Enhancement", "Maintenance", "Incident", "Defect")


@app.get("/api/tickets")
def list_tickets(from_date: str = "", to_date: str = "", include_resolved: bool = False,
                 work_type: str = "bug_incident"):
    """Fetch AUT tickets from Jira (optionally date-filtered) and sync locally.

    from_date / to_date are YYYY-MM-DD (inclusive). Anything not matching that
    exact shape is ignored, so the values can't be used to inject JQL.
    include_resolved=True drops the 'not Done' filter so closed tickets show too.
    work_type selects the issue type(s): "bug_incident" (default) = Bug + Incident,
    "all" = every type, or one exact name from _WORK_TYPES. Unknown values fall
    back to the default, so the param can't inject JQL.
    """
    jira = _jira()
    clauses = ["project = AUT"]
    if work_type == "all":
        pass  # no issuetype filter — every work type
    elif work_type in _WORK_TYPES:
        clauses.append(f'issuetype = "{work_type}"')
    else:  # "bug_incident" default, and any unrecognized value
        clauses.append("issuetype in (Bug, Incident)")
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


@app.get("/api/rca_tickets")
def list_rca_tickets():
    """The Dev Agent tab's worklist: tickets that already have an RCA, read from
    the local DB only (no Jira call). Clicking one lets a human run the fix agent."""
    return store.get_tickets_with_rca()


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
        jira = _jira()
        client = _gl()
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
        raw, turns_used, tools_used = asyncio.run(
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
        # Guard: blame gathered but not recorded (introducing commit lost, is_regression unset).
        bnote = blame_dropped_note(v, tools_used)
        if bnote:
            v.notes = (v.notes + "\n\n" + bnote).strip() if v.notes else bnote
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


def _suggest_fix_background(key: str) -> None:
    """Run the (3-4 min) fix agent in a thread and record the result on the job row.
    Started by the /suggest_fix endpoint, which returns immediately so the reverse
    proxy never times out a long-open request."""
    import asyncio
    import traceback
    from ..fix_agent import suggest_fix
    try:
        ticket = store.get_ticket(key)
        verdict = json.loads(ticket["bot_rca_json"])
        s = get_settings()
        client = _gl()
        # Multi-file fixes legitimately explore for ~4 min; keep this comfortably
        # above the fix agent's own exploration budget (_MAX_TURNS) so a real run
        # isn't killed mid-flight and reported as a timeout.
        sug = asyncio.run(asyncio.wait_for(suggest_fix(verdict, client, s), timeout=420))
        result = sug.to_dict()
        # Persist so the suggestion survives a page refresh (it's a ~3-4 min run to
        # regenerate). Cleared by reset_rca when the RCA is re-run.
        store.save_fix(key, json.dumps(result))
        store.finish_job(key, result)
    except (asyncio.TimeoutError, TimeoutError):
        store.fail_job(key, "Fix suggestion timed out after 7 minutes.")
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI
        traceback.print_exc()
        store.fail_job(key, f"{type(e).__name__}: {str(e)[:200]}")


@app.get("/api/tickets/{key}/job")
def job_status(key: str):
    """Poll this for the outcome of a background action (fix / accept_post / reject).
    Returns {kind, status: running|done|failed, error, result}."""
    job = store.get_job(key)
    if job is None:
        raise HTTPException(404, "Ticket not found")
    return job


@app.post("/api/tickets/{key}/suggest_fix")
def suggest_fix_endpoint(key: str):
    """Phase 1 (dry-run): start a background fix suggestion for a completed RCA.
    Returns immediately; poll GET /job for the diff + rationale + syntax check.
    Writes NOTHING to GitLab. Human-initiated."""
    import threading
    ticket = store.get_ticket(key)
    if not ticket or not ticket.get("bot_rca_json"):
        raise HTTPException(400, "No RCA found for this ticket — run RCA first")
    if not store.start_job(key, "fix"):
        return {"status": "already_running"}
    threading.Thread(target=_suggest_fix_background, args=(key,), daemon=True).start()
    return {"status": "started"}


@app.post("/api/tickets/{key}/raise_mr")
def raise_mr_endpoint(key: str):
    """Phase 1b (WRITE): push a branch + open a DRAFT MR for the reviewed fix. Uses the
    separate GITLAB_FIX_TOKEN bot (Developer role — cannot merge). Human-initiated."""
    from ..fix_mr import raise_mr
    ticket = store.get_ticket(key)
    if not ticket or not ticket.get("bot_fix_json"):
        raise HTTPException(400, "No fix suggestion found — run the dev agent first")
    fix = json.loads(ticket["bot_fix_json"])
    s = get_settings()
    client = _gl()
    res = raise_mr(key, fix, client)
    if res.get("error"):
        raise HTTPException(400, res["error"])
    # Persist the MR result inside the stored fix so the UI still shows it after refresh.
    fix["mr"] = res
    store.save_fix(key, json.dumps(fix))
    return res


@app.post("/api/tickets/{key}/reject_fix")
def reject_fix_endpoint(key: str):
    """Discard the stored dry-run fix suggestion (reviewer rejected it). Local only —
    writes nothing to GitLab, and does not touch any MR that may already be open."""
    if not store.get_ticket(key):
        raise HTTPException(404, "Ticket not found")
    store.clear_fix(key)
    return {"status": "cleared"}


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
    """Clear stored RCA (and any fix built on it) so it can be re-run."""
    with store._conn() as con:
        con.execute("UPDATE reviews SET bot_rca_json=NULL, bot_fix_json=NULL, "
                    "status='pending', job_kind=NULL, job_status=NULL, "
                    "job_error=NULL, job_result=NULL WHERE key=?", (key,))
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


def _accept_and_post_background(key: str) -> None:
    """Post the bot's RCA to Jira and mark accepted, in a thread. A slow Jira call
    can outlast the reverse proxy's read timeout, so the endpoint returns
    immediately and the UI polls GET /job for the outcome."""
    import traceback
    from ..agent import parse_verdict
    try:
        ticket = store.get_ticket(key)
        jira = _jira()
        v = parse_verdict(ticket["bot_rca_json"], key)
        res = jira.post_verdict(key, v)
        store.mark_accepted(key, res.get("id", ""))
        store.finish_job(key, {"comment_id": res.get("id")})
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI
        traceback.print_exc()
        store.fail_job(key, f"{type(e).__name__}: {str(e)[:200]}")


@app.post("/api/tickets/{key}/accept_and_post")
def accept_and_post(key: str):
    """Start posting the bot's RCA to Jira; returns immediately. Poll GET /job."""
    import threading
    ticket = store.get_ticket(key)
    if not ticket or not ticket.get("bot_rca_json"):
        raise HTTPException(400, "No RCA found for this ticket — run RCA first")
    if ticket["status"] in ("accepted", "rejected"):
        raise HTTPException(400, "Already reviewed")
    if not store.start_job(key, "accept_post"):
        return {"status": "already_running"}
    threading.Thread(target=_accept_and_post_background, args=(key,), daemon=True).start()
    return {"status": "started"}


def _reject_background(key: str, human_rca: str) -> None:
    """Post the human's RCA to Jira and mark rejected, in a thread (see
    _accept_and_post_background for why this is backgrounded)."""
    import traceback
    try:
        jira = _jira()
        adf = {
            "type": "doc", "version": 1,
            "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": human_rca}
                ]},
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "(Human RCA — Automated RCA)",
                     "marks": [{"type": "code"}]}
                ]},
            ]
        }
        res = jira.add_comment_adf(key, adf)
        store.mark_rejected(key, human_rca, res.get("id", ""))
        store.finish_job(key, {"comment_id": res.get("id")})
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI
        traceback.print_exc()
        store.fail_job(key, f"{type(e).__name__}: {str(e)[:200]}")


@app.post("/api/tickets/{key}/reject")
def reject(key: str, body: RejectRequest):
    """Start posting the human's RCA to Jira; returns immediately. Poll GET /job."""
    import threading
    if not body.human_rca.strip():
        raise HTTPException(400, "Human RCA cannot be empty")
    ticket = store.get_ticket(key)
    if ticket and ticket["status"] in ("accepted", "rejected"):
        raise HTTPException(400, "Already reviewed")
    if not store.start_job(key, "reject"):
        return {"status": "already_running"}
    threading.Thread(target=_reject_background, args=(key, body.human_rca),
                     daemon=True).start()
    return {"status": "started"}


@app.get("/api/scoreboard")
def scoreboard():
    return store.get_scoreboard()


@app.get("/api/quality")
def quality():
    """RCA-quality analytics for the Quality tab (accept/reject + VERDICT + cause)."""
    return store.get_quality_stats()
