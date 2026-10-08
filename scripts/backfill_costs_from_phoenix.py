"""One-off: import past agent-run costs from Phoenix traces into the Cost tab ledger.

Live per-run cost recording only starts when the Cost tab ships. Runs before that
are still in Phoenix (tracing has been on since 2026-07-15, no retention limit):
every `ClaudeAgentSDK.query` span carries the SDK's own total cost, token counts
and model. This copies ONLY those numbers, the span times and a ticket key
(regex-matched from the prompt) into `agent_runs`. No prompt text is stored.

Classification:
  * span under an eval-harness trace (`Task: run_rca_task` root) -> kind 'eval'
  * prompt starts with the fix agent's "# RCA" header           -> kind 'fix'
  * otherwise                                                     -> kind 'rca'
  * no AUT-#### key found in the prompt                           -> kind 'unattributed'
    (still counted in totals, just not tied to a ticket)

Idempotent: each span is keyed `phoenix:<span_id>` (UNIQUE source_ref), so a
second run inserts nothing new.

Run on the box (Phoenix DB and the dashboard DB both live there):
    .venv/bin/python scripts/backfill_costs_from_phoenix.py --dry-run
    .venv/bin/python scripts/backfill_costs_from_phoenix.py
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rca_agent.usage import estimate_cost  # noqa: E402
from rca_agent.webapp import db  # noqa: E402

KEY_RE = re.compile(r"\bAUT-\d+\b")
EVAL_ROOT = "Task: run_rca_task"

_SQL = """
SELECT s.span_id, s.start_time, s.end_time, s.status_code,
       json_extract(s.attributes, '$.llm.cost.total')                            AS cost,
       json_extract(s.attributes, '$.llm.model_name')                            AS model,
       json_extract(s.attributes, '$.llm.token_count.prompt')                    AS inp,
       json_extract(s.attributes, '$.llm.token_count.completion')                AS outp,
       json_extract(s.attributes, '$.llm.token_count.prompt_details.cache_read') AS cread,
       json_extract(s.attributes, '$.llm.token_count.prompt_details.cache_write') AS cwrite,
       json_extract(s.attributes, '$.input.value')                               AS input,
       (SELECT r.name FROM spans r
         WHERE r.trace_rowid = s.trace_rowid AND r.parent_id IS NULL LIMIT 1)    AS root
FROM spans s
WHERE s.name = 'ClaudeAgentSDK.query'
"""


def _iso(ts: str | None) -> str | None:
    """Phoenix stores UTC as 'YYYY-MM-DD HH:MM:SS.ffffff' -> 'YYYY-MM-DDTHH:MM:SSZ'."""
    return f"{ts[:19].replace(' ', 'T')}Z" if ts else None


def _ms(a: str | None, b: str | None) -> int | None:
    from datetime import datetime
    try:
        return int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() * 1000)
    except (TypeError, ValueError):
        return None


def to_row(r: sqlite3.Row) -> dict:
    text = r["input"] or ""
    keys = KEY_RE.findall(text)
    if r["root"] == EVAL_ROOT:
        kind = "eval"
    elif text.lstrip().startswith("# RCA"):
        kind = "fix"
    else:
        kind = "rca" if keys else "unattributed"
    inp, outp = int(r["inp"] or 0), int(r["outp"] or 0)
    cread, cwrite = int(r["cread"] or 0), int(r["cwrite"] or 0)
    cost, source = r["cost"], "phoenix"
    if cost is None and (inp or outp):
        cost, source = estimate_cost(r["model"] or "", inp, outp, cread, cwrite), "estimated"
    return {
        "ticket_key": keys[0] if keys else None,
        "kind": kind,
        "trigger": "backfill",
        "status": "failed" if (r["status_code"] or "").upper() == "ERROR" else "ok",
        "model": r["model"],
        "input_tokens": inp, "output_tokens": outp,
        "cache_read_tokens": cread, "cache_write_tokens": cwrite,
        "cost_usd": float(cost) if cost is not None else None,
        "cost_source": source if cost is not None else None,
        "duration_ms": _ms(r["start_time"], r["end_time"]),
        "started_at": _iso(r["start_time"]),
        "finished_at": _iso(r["end_time"]),
        "source_ref": f"phoenix:{r['span_id']}",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--phoenix-db", default=str(Path.home() / ".phoenix" / "phoenix.db"))
    ap.add_argument("--dry-run", action="store_true", help="print a summary, write nothing")
    args = ap.parse_args()

    src = sqlite3.connect(f"file:{args.phoenix_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = [to_row(r) for r in src.execute(_SQL)]
    src.close()

    by_kind = Counter(r["kind"] for r in rows)
    cost = sum(r["cost_usd"] or 0 for r in rows)
    print(f"{len(rows)} spans · ${cost:,.2f} · by kind: {dict(by_kind)}")
    print(f"no cost on {sum(r['cost_usd'] is None for r in rows)} span(s)")
    if args.dry_run:
        return
    db.init_db()
    added = sum(db.record_run(**r) for r in rows)
    print(f"inserted {added} new run(s) ({len(rows) - added} already imported)")


if __name__ == "__main__":
    main()
