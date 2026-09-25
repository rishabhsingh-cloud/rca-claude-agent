"""SQLite store for ticket reviews and decisions."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "rca_reviews.db"

# The team's day for the Auto-RCA daily cap. Fixed offset, no DST.
IST = timezone(timedelta(hours=5, minutes=30))

# Verdict wording changed from "BUG Accepted" to "Issue Accepted"; RCAs stored
# before that still carry the old label.
LEGACY_VERDICT = {"BUG Accepted": "Issue Accepted"}

# Statuses meaning "a human already decided on this RCA". 'unclear' = the reviewer
# could not understand the RCA (recorded locally, never posted to Jira).
DECIDED_STATUSES = ("accepted", "rejected", "unclear")

# Tick-box reasons offered in the "What was unclear?" section. Stored as a JSON list
# so the Quality tab can count them — the data for improving RCA wording later.
UNCLEAR_REASONS = {
    "too_technical":    "Too technical / jargon",
    "too_long":         "Too long to follow",
    "no_clear_answer":  "No clear answer on what went wrong",
    "next_step_unclear": "Not clear what to do next",
    "contradictory":    "Confusing or contradicts itself",
    "wrong_area":       "Talks about the wrong area / feature",
}


@contextmanager
def _conn():
    """Yield a SQLite connection and ALWAYS close it.

    Trap: `with sqlite3.connect(...) as con` commits/rolls back the transaction
    but does NOT close the connection. The old `return con` here therefore leaked
    one open file handle on every DB call; with the dashboard polling constantly,
    the process eventually hit its fd limit (OSError [Errno 24] Too many open
    files) and stopped accepting connections. Committing still happens via the
    inner `with con`; the `finally` guarantees the handle is released."""
    # timeout: request threads, RCA worker threads and the Auto-RCA poller all write;
    # wait for a busy writer instead of raising "database is locked".
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        # WAL: readers (the dashboard's polling) no longer block on a writer
        # (poller / RCA threads). Persistent per database file; harmless to repeat.
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                key             TEXT PRIMARY KEY,
                title           TEXT,
                description     TEXT,
                status          TEXT DEFAULT 'pending',
                bot_rca_json    TEXT,
                human_rca       TEXT,
                comment_id      TEXT,
                turns_used      INTEGER,
                error           TEXT,
                created_at      TEXT,
                updated_at      TEXT DEFAULT (datetime('now'))
            )
        """)
        # migrate existing DBs that don't have these columns yet
        for col in ("turns_used INTEGER", "error TEXT", "bot_fix_json TEXT",
                    # Generic background-job tracking for the slow synchronous
                    # actions (fix suggestion, accept+post, reject). These let the
                    # POST return instantly and the UI poll for completion, so a
                    # 3-4 min run never sits on an open connection the reverse
                    # proxy will time out. job_kind: which action is in flight;
                    # job_status: running|done|failed; job_result: JSON payload
                    # the poller renders (e.g. the fix dict or {comment_id}).
                    "job_kind TEXT", "job_status TEXT", "job_error TEXT",
                    "job_result TEXT",
                    # A human-written RCA saved locally but NOT yet posted to Jira
                    # (QA can draft now, post later). Independent of bot_rca_json /
                    # status; cleared once the human RCA is actually posted.
                    "human_rca_draft TEXT",
                    # Auto-RCA: who started the last run ('manual' | 'auto'), and the
                    # UTC time of the first AUTOMATIC claim. auto_run_at is set once and
                    # never cleared (not by /reset, not by failure) — it is the poller's
                    # idempotency marker, so a ticket is auto-run at most once, ever.
                    "trigger_source TEXT", "auto_run_at TEXT",
                    # Optional reviewer note for a "Not able to understand" decision
                    # (what was unclear) — feeds RCA-wording improvements.
                    "review_note TEXT", "unclear_reasons TEXT",
                    # Triage list membership. on_dashboard is set once (the ticket was
                    # synced while a Bug/Incident) and never cleared, so a ticket
                    # stays listed after Jira changes its type or closes it.
                    # issue_type / jira_status / jira_done are its latest Jira values.
                    "on_dashboard INTEGER DEFAULT 0", "issue_type TEXT",
                    "jira_status TEXT", "jira_done INTEGER"):
            try:
                con.execute(f"ALTER TABLE reviews ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        # Backfill for rows from before on_dashboard existed: anything already worked
        # on came from the Triage list. Idempotent (only ever sets the flag).
        con.execute("UPDATE reviews SET on_dashboard = 1 WHERE key LIKE 'AUT-%' "
                    "AND (status != 'pending' OR bot_rca_json IS NOT NULL)")
        # Small key/value store for dashboard-editable configuration (Auto-RCA rules).
        con.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Recover rows wedged at 'running' by a crash/restart mid-run: their
        # background thread is gone, so fail them (Retry button) instead of
        # leaving an un-runnable spinner.
        con.execute("UPDATE reviews SET status = 'failed', "
                    "error = 'Interrupted by a server restart — please retry.' "
                    "WHERE status = 'running'")
        # Same for a background action-job whose thread died with the process.
        con.execute("UPDATE reviews SET job_status = 'failed', "
                    "job_error = 'Interrupted by a server restart — please retry.' "
                    "WHERE job_status = 'running'")


def upsert_ticket(key: str, title: str, description: str, created_at: str,
                  issue_type: str | None = None, jira_status: str | None = None,
                  jira_done: bool = False, pin: bool = False) -> None:
    """pin=True puts the ticket on the Triage list for good: on_dashboard only ever
    goes 0 -> 1, so a later sync after a type change or close never removes it."""
    with _conn() as con:
        con.execute("""
            INSERT INTO reviews (key, title, description, created_at,
                                 issue_type, jira_status, jira_done, on_dashboard)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                title = excluded.title,
                description = excluded.description,
                issue_type = COALESCE(excluded.issue_type, issue_type),
                jira_status = COALESCE(excluded.jira_status, jira_status),
                jira_done = CASE WHEN excluded.jira_status IS NULL THEN jira_done
                                 ELSE excluded.jira_done END,
                on_dashboard = MAX(COALESCE(on_dashboard, 0), excluded.on_dashboard)
        """, (key, title, description, created_at, issue_type, jira_status,
              int(bool(jira_done)), int(bool(pin))))


def get_dashboard_tickets(from_date: str = "", to_date: str = "") -> list[dict]:
    """Tickets pinned to the Triage list (see upsert_ticket), optionally bounded by
    created date. Dates are YYYY-MM-DD, inclusive; the caller validates the shape."""
    sql = "SELECT * FROM reviews WHERE on_dashboard = 1 AND key LIKE 'AUT-%'"
    args: list[str] = []
    if from_date:
        sql += " AND substr(created_at, 1, 10) >= ?"
        args.append(from_date)
    if to_date:
        sql += " AND substr(created_at, 1, 10) <= ?"
        args.append(to_date)
    with _conn() as con:
        return [dict(r) for r in con.execute(sql + " ORDER BY created_at DESC", args)]


def get_ticket(key: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM reviews WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None


def get_all_tickets() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM reviews ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_tickets_with_rca() -> list[dict]:
    """Tickets that already have an RCA — the Dev Agent tab's worklist.
    Most-recently-updated first; read from the local DB only (no Jira sync)."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM reviews WHERE bot_rca_json IS NOT NULL "
            "ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def save_rca(key: str, rca_json: str, turns_used: int | None = None) -> None:
    with _conn() as con:
        con.execute("""
            UPDATE reviews SET bot_rca_json = ?, status = 'rca_ready',
            turns_used = ?, error = NULL, updated_at = datetime('now') WHERE key = ?
        """, (rca_json, turns_used, key))


def save_fix(key: str, fix_json: str) -> None:
    """Persist the latest dry-run fix suggestion so it survives a page refresh.
    Kept separate from the RCA; cleared by reset_rca when the RCA is re-run so a
    fix generated for an old verdict never lingers against a new one."""
    with _conn() as con:
        con.execute("UPDATE reviews SET bot_fix_json = ?, updated_at = datetime('now') "
                    "WHERE key = ?", (fix_json, key))


def clear_fix(key: str) -> None:
    """Discard a stored fix suggestion (the reviewer rejected it). Local only."""
    with _conn() as con:
        con.execute("UPDATE reviews SET bot_fix_json = NULL, updated_at = datetime('now') "
                    "WHERE key = ?", (key,))


def save_human_rca_draft(key: str, text: str) -> None:
    """Persist a human-written RCA WITHOUT posting it to Jira, so QA can draft now and
    post later. Local only — never touches Jira or the ticket status."""
    with _conn() as con:
        con.execute("UPDATE reviews SET human_rca_draft = ?, updated_at = datetime('now') "
                    "WHERE key = ?", (text, key))


def clear_human_rca_draft(key: str) -> None:
    """Drop a saved human-RCA draft (e.g. once it's been posted)."""
    with _conn() as con:
        con.execute("UPDATE reviews SET human_rca_draft = NULL, updated_at = datetime('now') "
                    "WHERE key = ?", (key,))


# --- Generic background-job state (fix suggestion / accept+post / reject) -----

def start_job(key: str, kind: str) -> bool:
    """Claim the job slot for `key` and mark it running. Returns False if a job is
    already running for this ticket (so a double-click can't launch two threads)."""
    with _conn() as con:
        cur = con.execute(
            "UPDATE reviews SET job_kind = ?, job_status = 'running', "
            "job_error = NULL, job_result = NULL, updated_at = datetime('now') "
            "WHERE key = ? AND (job_status IS NULL OR job_status != 'running')",
            (kind, key))
        return cur.rowcount > 0


def finish_job(key: str, result: dict | list | None = None) -> None:
    """Mark the in-flight job done, storing an optional JSON result for the poller."""
    with _conn() as con:
        con.execute(
            "UPDATE reviews SET job_status = 'done', job_result = ?, "
            "updated_at = datetime('now') WHERE key = ?",
            (json.dumps(result) if result is not None else None, key))


def fail_job(key: str, error: str) -> None:
    """Mark the in-flight job failed with a human-readable reason for the poller."""
    with _conn() as con:
        con.execute(
            "UPDATE reviews SET job_status = 'failed', job_error = ?, "
            "updated_at = datetime('now') WHERE key = ?", (error, key))


def get_job(key: str) -> dict | None:
    """Current background-job state for a ticket (None if the ticket is unknown)."""
    t = get_ticket(key)
    if t is None:
        return None
    return {
        "kind": t.get("job_kind"),
        "status": t.get("job_status"),
        "error": t.get("job_error"),
        "result": json.loads(t["job_result"]) if t.get("job_result") else None,
    }


def claim_running(key: str, source: str = "manual") -> bool:
    """Atomically claim an RCA run for `key`: flip it to 'running' unless a run is
    already in flight. Returns False when it is (so a double-click, or the Auto-RCA
    poller racing a click, can never launch two investigations for one ticket).
    Same conditional-UPDATE idiom as `start_job`."""
    with _conn() as con:
        cur = con.execute("""
            UPDATE reviews SET status = 'running', error = NULL, trigger_source = ?,
            updated_at = datetime('now') WHERE key = ? AND status != 'running'
        """, (source, key))
        return cur.rowcount > 0


def claim_auto_run(key: str) -> bool:
    """The Auto-RCA poller's claim. Stricter than `claim_running`: only a ticket that
    has never been investigated (no verdict, no earlier automatic attempt, status
    still 'pending') can be taken, and the attempt is stamped in `auto_run_at` so the
    same ticket is never auto-run twice — even after a human /reset."""
    with _conn() as con:
        cur = con.execute("""
            UPDATE reviews SET status = 'running', error = NULL, trigger_source = 'auto',
            auto_run_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
            updated_at = datetime('now')
            WHERE key = ? AND status = 'pending' AND bot_rca_json IS NULL
              AND auto_run_at IS NULL
        """, (key,))
        return cur.rowcount > 0


def mark_running(key: str) -> None:
    """Unconditional legacy setter; prefer `claim_running` (atomic) for new callers."""
    with _conn() as con:
        con.execute("""
            UPDATE reviews SET status = 'running', error = NULL,
            updated_at = datetime('now') WHERE key = ?
        """, (key,))


def ist_day_start_utc(now: datetime | None = None) -> str:
    """Midnight IST of the current (IST) day, as a UTC ISO 'Z' string comparable with
    `auto_run_at`. The daily cap resets at midnight in the team's timezone."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(IST)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def count_auto_runs_today(now: datetime | None = None) -> int:
    """Automatic run ATTEMPTS since midnight IST (successes and failures alike —
    each one cost a Claude run, which is what the cap bounds)."""
    with _conn() as con:
        return con.execute(
            "SELECT COUNT(*) FROM reviews WHERE auto_run_at >= ?",
            (ist_day_start_utc(now),)).fetchone()[0]


def running_auto_keys() -> list[str]:
    """Tickets whose automatic investigation is in flight right now."""
    with _conn() as con:
        rows = con.execute(
            "SELECT key FROM reviews WHERE status = 'running' AND trigger_source = 'auto' "
            "ORDER BY updated_at").fetchall()
        return [r["key"] for r in rows]


# --- Dashboard-editable settings (JSON values) --------------------------------

def get_setting(key: str, default=None):
    with _conn() as con:
        row = con.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return default


def set_setting(key: str, value) -> None:
    with _conn() as con:
        con.execute("""
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
        """, (key, json.dumps(value)))


def mark_failed(key: str, error: str | None = None) -> None:
    """Record a failed/timed-out run — but NEVER regress a ticket that already holds
    a verdict. A re-run that fails must not wipe the previously-good result (the
    AUT-9864 clobber): `mark_running` only flips the status to 'running' and leaves
    `bot_rca_json` intact, so we key the guard on the verdict itself, not the status.

      - no verdict yet (bot_rca_json IS NULL) -> mark 'failed' (genuine failure).
      - a verdict exists                        -> keep it; restore 'rca_ready' so the
        row isn't left stuck at 'running', and stash the reason in `error` as a
        breadcrumb (the /status endpoint only surfaces `error` for failed rows).
    """
    with _conn() as con:
        cur = con.execute("""
            UPDATE reviews SET status = 'failed', error = ?,
            updated_at = datetime('now') WHERE key = ? AND bot_rca_json IS NULL
        """, (error, key))
        if cur.rowcount == 0:
            con.execute("""
                UPDATE reviews SET status = 'rca_ready', error = ?,
                updated_at = datetime('now')
                WHERE key = ? AND bot_rca_json IS NOT NULL
            """, (f"[last re-run did not finish: {error}]" if error else None, key))


def mark_accepted(key: str, comment_id: str) -> None:
    with _conn() as con:
        con.execute("""
            UPDATE reviews SET status = 'accepted', comment_id = ?,
            updated_at = datetime('now') WHERE key = ?
        """, (comment_id, key))


def mark_rejected(key: str, human_rca: str, comment_id: str) -> None:
    with _conn() as con:
        con.execute("""
            UPDATE reviews SET status = 'rejected', human_rca = ?, comment_id = ?,
            updated_at = datetime('now') WHERE key = ?
        """, (human_rca, comment_id, key))


def mark_unclear(key: str, note: str, reasons: list[str]) -> None:
    with _conn() as con:
        con.execute("""
            UPDATE reviews SET status = 'unclear', review_note = ?, unclear_reasons = ?,
            updated_at = datetime('now') WHERE key = ?
        """, (note, json.dumps(reasons), key))


def get_scoreboard() -> dict:
    with _conn() as con:
        total = con.execute(
            "SELECT COUNT(*) FROM reviews WHERE status IN ('accepted','rejected')"
        ).fetchone()[0]
        accepted = con.execute(
            "SELECT COUNT(*) FROM reviews WHERE status = 'accepted'"
        ).fetchone()[0]
        rejected = con.execute(
            "SELECT COUNT(*) FROM reviews WHERE status = 'rejected'"
        ).fetchone()[0]
    rate = round(accepted / total * 10, 1) if total else 0
    return {"total": total, "accepted": accepted, "rejected": rejected,
            "rate": rate, "goal": 9.0}


def get_quality_stats() -> dict:
    """RCA-quality analytics for the Quality tab, from data we already store:
    accept/reject outcomes + the agent's own VERDICT and cause-bucket distribution
    across every ticket that has an RCA."""
    with _conn() as con:
        rows = con.execute("SELECT key, status, bot_rca_json, review_note, unclear_reasons, "
                           "updated_at FROM reviews").fetchall()
    total = len(rows)
    accepted = rejected = unclear = with_rca = 0
    by_verdict: dict[str, int] = {}
    by_cause: dict[str, int] = {}
    by_unclear_reason: dict[str, int] = {k: 0 for k in UNCLEAR_REASONS}
    unclear_notes: list[dict] = []
    for r in rows:
        if r["status"] == "accepted":
            accepted += 1
        elif r["status"] == "rejected":
            rejected += 1
        elif r["status"] == "unclear":
            unclear += 1
            try:
                reasons = json.loads(r["unclear_reasons"] or "[]")
            except (ValueError, TypeError):
                reasons = []
            for k in reasons:
                if k in by_unclear_reason:
                    by_unclear_reason[k] += 1
            unclear_notes.append({"key": r["key"], "note": r["review_note"] or "",
                                  "reasons": [UNCLEAR_REASONS.get(k, k) for k in reasons],
                                  "at": r["updated_at"]})
        if r["bot_rca_json"]:
            with_rca += 1
            try:
                d = json.loads(r["bot_rca_json"])
            except (ValueError, TypeError):
                continue
            vl = d.get("verdict_label")
            # RCAs saved before the rename carry the old wording; count them
            # under the new one so the stats page shows a single bucket.
            vl = LEGACY_VERDICT.get(vl, vl)
            if vl:
                by_verdict[vl] = by_verdict.get(vl, 0) + 1
            for c in (d.get("cause_categories") or []):
                by_cause[c] = by_cause.get(c, 0) + 1
    return {"total": total, "with_rca": with_rca, "reviewed": accepted + rejected + unclear,
            "accepted": accepted, "rejected": rejected, "unclear": unclear,
            "by_verdict": by_verdict, "by_cause": by_cause,
            "unclear_reasons": UNCLEAR_REASONS, "by_unclear_reason": by_unclear_reason,
            # Newest first; every "Not able to understand" with its reasons + note.
            "unclear_notes": sorted(unclear_notes, key=lambda n: n["at"] or "", reverse=True)}
