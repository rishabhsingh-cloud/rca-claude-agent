"""SQLite store for ticket reviews and decisions."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "rca_reviews.db"


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


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
        for col in ("turns_used INTEGER", "error TEXT"):
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


def save_rca(key: str, rca_json: str, turns_used: int | None = None) -> None:
    with _conn() as con:
        con.execute("""
            UPDATE reviews SET bot_rca_json = ?, status = 'rca_ready',
            turns_used = ?, error = NULL, updated_at = datetime('now') WHERE key = ?
        """, (rca_json, turns_used, key))


def mark_running(key: str) -> None:
    with _conn() as con:
        con.execute("""
            UPDATE reviews SET status = 'running', error = NULL,
            updated_at = datetime('now') WHERE key = ?
        """, (key,))


def mark_failed(key: str, error: str | None = None) -> None:
    with _conn() as con:
        con.execute("""
            UPDATE reviews SET status = 'failed', error = ?,
            updated_at = datetime('now') WHERE key = ?
        """, (error, key))


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
