"""SQLite store for ticket reviews and decisions."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "rca_reviews.db"


@contextmanager
def _conn():
    """Yield a SQLite connection and ALWAYS close it.

    Trap: `with sqlite3.connect(...) as con` commits/rolls back the transaction
    but does NOT close the connection. The old `return con` here therefore leaked
    one open file handle on every DB call; with the dashboard polling constantly,
    the process eventually hit its fd limit (OSError [Errno 24] Too many open
    files) and stopped accepting connections. Committing still happens via the
    inner `with con`; the `finally` guarantees the handle is released."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
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
                    "human_rca_draft TEXT"):
            try:
                con.execute(f"ALTER TABLE reviews ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
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


def upsert_ticket(key: str, title: str, description: str, created_at: str) -> None:
    with _conn() as con:
        con.execute("""
            INSERT INTO reviews (key, title, description, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                title = excluded.title,
                description = excluded.description
        """, (key, title, description, created_at))


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


def mark_running(key: str) -> None:
    with _conn() as con:
        con.execute("""
            UPDATE reviews SET status = 'running', error = NULL,
            updated_at = datetime('now') WHERE key = ?
        """, (key,))


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
        rows = con.execute("SELECT status, bot_rca_json FROM reviews").fetchall()
    total = len(rows)
    accepted = rejected = with_rca = 0
    by_verdict: dict[str, int] = {}
    by_cause: dict[str, int] = {}
    for r in rows:
        if r["status"] == "accepted":
            accepted += 1
        elif r["status"] == "rejected":
            rejected += 1
        if r["bot_rca_json"]:
            with_rca += 1
            try:
                d = json.loads(r["bot_rca_json"])
            except (ValueError, TypeError):
                continue
            vl = d.get("verdict_label")
            if vl:
                by_verdict[vl] = by_verdict.get(vl, 0) + 1
            for c in (d.get("cause_categories") or []):
                by_cause[c] = by_cause.get(c, 0) + 1
    return {"total": total, "with_rca": with_rca, "reviewed": accepted + rejected,
            "accepted": accepted, "rejected": rejected,
            "by_verdict": by_verdict, "by_cause": by_cause}
