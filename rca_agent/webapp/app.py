"""FastAPI web app — human-in-the-loop RCA review UI."""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..agent import blame_dropped_note, parse_verdict, run_agent
from ..config import get_settings
from ..gitlab_client import build_client
from ..jira import JiraClient, JiraError
from ..schema import verdict_to_adf
from ..tickets import build_ticket_source
from ..verify import verify_verdict
from . import autorun
from . import db as store
from .autorun import WORK_TYPES as _WORK_TYPES


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start the Auto-RCA poller thread with the server and stop it on shutdown.

    RCA_AUTORUN_POLLER=0 keeps the thread out of this process entirely (local dev,
    tests). That is the process switch; the FEATURE switch is the `enabled` flag the
    dashboard panel stores in SQLite, re-read every tick — so turning Auto-RCA on or
    off never needs a restart. Single uvicorn worker only: N workers = N pollers."""
    p = None
    if os.getenv("RCA_AUTORUN_POLLER", "1") != "0":
        p = autorun.Poller(run_rca=_run_rca_background, sync_issue=_sync_issue,
                           jira_factory=_jira)
        autorun.poller = p
        p.start()
    try:
        yield
    finally:
        if p is not None:
            p.stop()
            autorun.poller = None


app = FastAPI(title="RCA Review", lifespan=lifespan)
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

# _WORK_TYPES (imported from autorun): the work types the AUT project defines.
# Whitelisted so a value picked in the UI dropdown is interpolated into JQL only if
# it's a known type — the value can never be used to inject arbitrary JQL.


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
    # Return ONLY the tickets just fetched (AUT + this date range) with their
    # stored RCA state — not the whole DB, which still holds old non-AUT rows
    # from earlier syncs.
    return [t for i in issues if (t := _sync_issue(i))]


def _sync_issue(issue: dict) -> dict | None:
    """Store one Jira issue (title/description/created) locally and return the
    stored row, which carries the RCA state the UI renders."""
    key = issue["key"]
    fields = issue.get("fields", {})
    title = fields.get("summary", "")
    desc = fields.get("description", "") or ""
    if isinstance(desc, dict):
        from ..tickets import flatten_adf
        desc = flatten_adf(desc)
    created_at = fields.get("created", "")
    store.upsert_ticket(key, title, desc, created_at)
    return store.get_ticket(key)


# A ticket key typed into the search box. Bare digits are accepted as shorthand
# ("10001" -> AUT-10001) because the dashboard is AUT-only.
_TICKET_KEY_RE = _re.compile(r"^(?:AUT-)?(\d{1,7})$", _re.IGNORECASE)


def normalize_ticket_key(text: str) -> str | None:
    """'10001' / 'aut-10001' / ' AUT-10001 ' -> 'AUT-10001'; anything else -> None."""
    m = _TICKET_KEY_RE.match((text or "").strip())
    return f"AUT-{m.group(1)}" if m else None


@app.get("/api/tickets/{key}")
def get_ticket_by_key(key: str):
    """Open ONE ticket straight from Jira by key, ignoring the list filters.

    The list endpoint is bounded by work type, date range, the resolved toggle
    and a 100-newest cap, so an older or resolved ticket can be impossible to
    reach through it (AUT-10001 was the motivating case). This path has none of
    those bounds: any AUT key the Jira token can read is fetched, synced locally
    and returned in the same shape as a list row. 404 if Jira has no such issue."""
    norm = normalize_ticket_key(key)
    if not norm:
        raise HTTPException(400, "Not a ticket key (expected AUT-<number>)")
    try:
        issue = _jira().get_issue(norm)
    except JiraError as e:
        if "not found" in str(e):
            raise HTTPException(404, f"{norm} not found in Jira")
        raise HTTPException(502, f"Jira lookup failed: {e}")
    return _sync_issue(issue)


@app.get("/api/rca_tickets")
def list_rca_tickets():
    """The Dev Agent tab's worklist: tickets that already have an RCA, read from
    the local DB only (no Jira call). Clicking one lets a human run the fix agent."""
    return store.get_tickets_with_rca()


# Two-tier time bound on a single investigation:
#   SOFT budget — passed into run_agent, which self-enforces it at a turn boundary
#     and RETURNS whatever verdict it has. A run that finishes a little late (the
#     classic "605s vs 600s" case) is kept, not discarded.
#   HARD ceiling — a generous outer asyncio.wait_for that CANCELS the run. This is a
#     last resort for a genuinely wedged await (a run stuck mid-tool that the soft
#     budget's between-turns check can never reach), so a row can't wedge at
#     'running' forever. It should sit well above the soft budget so normal overruns
#     never hit it. Both are env-tunable.
RCA_TIMEOUT_SECONDS = int(os.getenv("RCA_TIMEOUT_SECONDS", "600"))
RCA_HARD_TIMEOUT_SECONDS = int(os.getenv("RCA_HARD_TIMEOUT_SECONDS", "900"))


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
        # Soft budget lives INSIDE run_agent (it stops at a turn boundary and returns
        # its verdict — a run that finishes slightly late is kept). The outer wait_for
        # is only the generous HARD ceiling for a genuinely wedged await; it cancels,
        # so it must stay well above the soft budget.
        raw, turns_used, tools_used = asyncio.run(
            asyncio.wait_for(
                run_agent(tkey, text, client, s, jira_mcp=False, images=images,
                          time_budget_s=RCA_TIMEOUT_SECONDS),
                timeout=RCA_HARD_TIMEOUT_SECONDS,
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
        mins = RCA_HARD_TIMEOUT_SECONDS // 60
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
    # Atomic claim: a double-click, or the Auto-RCA poller racing this click, can't
    # both pass a read-then-write check and launch two investigations.
    if not store.claim_running(key, "manual"):
        return {"status": "already_running"}
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
    # Always surface any saved (unposted) human RCA draft so the UI can restore it.
    if ticket.get("human_rca_draft"):
        result["human_rca_draft"] = ticket["human_rca_draft"]
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


@app.get("/api/tickets/{key}/fix_branches")
def fix_branches_endpoint(key: str):
    """Per-repo branch lists for the reviewed fix, so the UI can offer a target-branch
    picker before raising the MR. Returns {projects: {<project>: {default, branches}}}.
    `default` is the pre-selected target (the integration branch = client.default_ref)."""
    ticket = store.get_ticket(key)
    if not ticket or not ticket.get("bot_fix_json"):
        raise HTTPException(400, "No fix suggestion found — run the dev agent first")
    fix = json.loads(ticket["bot_fix_json"])
    projects = []
    for f in fix.get("files", []):
        p = f.get("project")
        if p and p not in projects and any(e.get("applied") for e in f.get("edits", [])):
            projects.append(p)
    client = _gl()
    out: dict[str, dict] = {}
    for p in projects:
        default = client.default_ref(p)
        try:
            branches = client.list_branches(p, limit=200)
        except Exception as e:  # noqa: BLE001 — degrade to just the default on any API error
            branches = [default]
        if default not in branches:
            branches = [default] + branches
        out[p] = {"default": default, "branches": branches}
    return {"projects": out}


class RaiseMrRequest(BaseModel):
    # project -> target branch the MR should merge into. Omitted repos fall back to
    # the integration default inside raise_mr().
    targets: dict[str, str] = {}


@app.post("/api/tickets/{key}/raise_mr")
def raise_mr_endpoint(key: str, req: RaiseMrRequest | None = None):
    """Phase 1b (WRITE): push a branch + open a DRAFT MR for the reviewed fix. Uses the
    separate GITLAB_FIX_TOKEN bot (Developer role — cannot merge). Human-initiated.
    Optional body {targets: {project: branch}} chooses each repo's merge target."""
    from ..fix_mr import raise_mr
    ticket = store.get_ticket(key)
    if not ticket or not ticket.get("bot_fix_json"):
        raise HTTPException(400, "No fix suggestion found — run the dev agent first")
    fix = json.loads(ticket["bot_fix_json"])
    s = get_settings()
    client = _gl()
    res = raise_mr(key, fix, client, targets=(req.targets if req else None))
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


class SaveHumanRcaRequest(BaseModel):
    text: str


class RejectLocalRequest(BaseModel):
    human_rca: str = ""


class UnclearRequest(BaseModel):
    note: str = ""
    reasons: list[str] = []


def _refuse_if_posting(ticket: dict) -> None:
    """A local decision must not race a Jira post still running in the background —
    the thread would overwrite it when it finishes (`mark_accepted` / `mark_rejected`).
    The UI disables the buttons while polling, but a page refresh re-enables them."""
    if (ticket.get("job_status") == "running"
            and ticket.get("job_kind") in ("accept_post", "reject")):
        raise HTTPException(409, "A Jira post for this ticket is still running — wait for it")


@app.post("/api/tickets/{key}/save_human_rca")
def save_human_rca(key: str, body: SaveHumanRcaRequest):
    """Save a human-written RCA locally WITHOUT posting to Jira (QA drafts now, posts
    later). Standalone — works whether or not the bot produced an RCA, and does not
    change the ticket status. Re-save to update; posting happens via /reject."""
    ticket = store.get_ticket(key)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if ticket["status"] in store.DECIDED_STATUSES:
        raise HTTPException(400, "Already reviewed — nothing to draft")
    store.save_human_rca_draft(key, body.text)
    return {"status": "saved"}


@app.post("/api/tickets/{key}/accept")
def accept(key: str):
    """Mark as accepted locally without posting to Jira."""
    ticket = store.get_ticket(key)
    if not ticket or not ticket.get("bot_rca_json"):
        raise HTTPException(400, "No RCA found for this ticket — run RCA first")
    if ticket["status"] in store.DECIDED_STATUSES:
        raise HTTPException(400, "Already reviewed")
    _refuse_if_posting(ticket)
    store.mark_accepted(key, "")
    return {"status": "accepted"}


@app.post("/api/tickets/{key}/reject_local")
def reject_local(key: str, body: RejectLocalRequest):
    """Mark the bot's RCA as wrong locally WITHOUT posting to Jira (mirror of /accept).
    Whatever the reviewer typed in "Your RCA" is kept as the record (optional): it
    becomes `human_rca` and is also stored as the draft, since the draft is only
    cleared once a human RCA is actually posted — so it re-surfaces after a re-run."""
    ticket = store.get_ticket(key)
    if not ticket or not ticket.get("bot_rca_json"):
        raise HTTPException(400, "No RCA found for this ticket — run RCA first")
    if ticket["status"] in store.DECIDED_STATUSES:
        raise HTTPException(400, "Already reviewed")
    _refuse_if_posting(ticket)
    text = body.human_rca.strip()
    if text:
        store.save_human_rca_draft(key, text)
    store.mark_rejected(key, text, "")
    return {"status": "rejected"}


@app.post("/api/tickets/{key}/unclear")
def mark_unclear(key: str, body: UnclearRequest):
    """"Not able to understand": the reviewer could not follow the bot's RCA. Recorded
    locally only (never posted to Jira) with WHAT was unclear — tick-box reasons
    (db.UNCLEAR_REASONS) and/or a free-text note, at least one of them — so the Quality
    tab can surface RCAs whose wording needs work. Same guards as /reject_local;
    Re-run reopens it like any other decision."""
    note = body.note.strip()
    bad = [r for r in body.reasons if r not in store.UNCLEAR_REASONS]
    if bad:
        raise HTTPException(400, f"Unknown reason(s): {bad}")
    reasons = list(dict.fromkeys(body.reasons))  # de-dupe, keep order
    if not note and not reasons:
        raise HTTPException(400, "Tick at least one reason or write what was unclear")
    ticket = store.get_ticket(key)
    if not ticket or not ticket.get("bot_rca_json"):
        raise HTTPException(400, "No RCA found for this ticket — run RCA first")
    if ticket["status"] in store.DECIDED_STATUSES:
        raise HTTPException(400, "Already reviewed")
    _refuse_if_posting(ticket)
    store.mark_unclear(key, note, reasons)
    return {"status": "unclear"}


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
    if ticket["status"] in store.DECIDED_STATUSES:
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
        store.clear_human_rca_draft(key)  # posted now — drop the local draft
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
    if ticket and ticket["status"] in store.DECIDED_STATUSES:
        raise HTTPException(400, "Already reviewed")
    if not store.start_job(key, "reject"):
        return {"status": "already_running"}
    threading.Thread(target=_reject_background, args=(key, body.human_rca),
                     daemon=True).start()
    return {"status": "started"}


# --- Auto-RCA (poller) settings + status ---------------------------------------

class AutorunSettingsUpdate(BaseModel):
    """Partial update from the dashboard panel; every field optional."""
    enabled: bool | None = None
    interval_seconds: int | None = None
    allowed_types: list[str] | None = None
    max_parallel: int | None = None
    daily_cap: int | None = None
    exclude_labels: list[str] | None = None
    exclude_env_keywords: list[str] | None = None


def _autorun_payload() -> dict:
    s = autorun.load_settings()
    return {**s, "work_types": list(_WORK_TYPES), "limits": autorun.LIMITS}


@app.get("/api/autorun/settings")
def autorun_settings():
    return _autorun_payload()


@app.put("/api/autorun/settings")
def update_autorun_settings(body: AutorunSettingsUpdate):
    """Validate + store the rules. Flipping `enabled` off->on stamps `enabled_at`,
    the watermark: only tickets created after that moment are ever auto-run."""
    current = autorun.load_settings()
    try:
        new = autorun.apply_update(current, body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    autorun.save_settings(new)
    return _autorun_payload()


@app.get("/api/autorun/status")
def autorun_status():
    """Live poller state for the panel's status line."""
    s = autorun.load_settings()
    p = autorun.poller
    snap = p.snapshot() if p is not None else {"poller_alive": False, "state": "not started"}
    return {**snap,
            "enabled": s["enabled"], "enabled_at": s.get("enabled_at"),
            "interval_seconds": s["interval_seconds"], "max_parallel": s["max_parallel"],
            "daily_cap": s["daily_cap"],
            "runs_today": store.count_auto_runs_today(),
            "in_flight": store.running_auto_keys()}


@app.post("/api/autorun/poll_now")
def autorun_poll_now():
    """Run one poll immediately (the panel's "Poll now" — verification/demo)."""
    p = autorun.poller
    if p is None:
        raise HTTPException(409, "Auto-RCA poller is not running in this process "
                                 "(RCA_AUTORUN_POLLER=0?)")
    p.run_once()
    return autorun_status()


@app.get("/api/unclear_reasons")
def unclear_reasons():
    """Tick-box reasons for the "What was unclear?" section ({id: label})."""
    return store.UNCLEAR_REASONS


@app.get("/api/scoreboard")
def scoreboard():
    return store.get_scoreboard()


@app.get("/api/quality")
def quality():
    """RCA-quality analytics for the Quality tab (accept/reject + VERDICT + cause)."""
    return store.get_quality_stats()
