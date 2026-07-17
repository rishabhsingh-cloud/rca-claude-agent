"""Run the REAL RCA agent over the pilot dataset and save verdicts.

Reuses production code verbatim — run_agent + parse_verdict + verify_verdict — so the
eval measures exactly what ships. Each ticket is fetched the same way production does
(drop_all_comments=True, attachments included) so the agent never sees the answer key.

Writes results/run_<timestamp>.jsonl. Touches nothing in production (no webapp DB, no
Jira writes). Requires ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

from ..agent import AgentRunError, parse_verdict, run_agent
from ..config import get_settings
from ..gitlab_client import build_client
from ..jira import JiraClient
from ..verify import verify_verdict
from ._common import DATA, RESULTS, read_jsonl, warn_no_api_key, write_jsonl


def _fetch(jira: JiraClient, key: str) -> tuple[str, list | None]:
    """Production-faithful fetch: ticket text WITHOUT comments, plus attachments."""
    _, text = jira.get(key, drop_all_comments=True)
    att = jira.get_all_attachments(key)
    if att.get("pdfs"):
        text += "\n\n" + "\n\n".join(
            f"--- attached PDF: {p['filename']} ---\n{p['text']}" for p in att["pdfs"])
    return text, (att.get("images") or None)


async def _one(row: dict, jira: JiraClient, gl, settings, max_turns: int) -> dict:
    key = row["key"]
    try:
        text, images = await asyncio.to_thread(_fetch, jira, key)
    except Exception as e:  # noqa: BLE001 — record and move on
        return {"key": key, "error": f"fetch: {type(e).__name__}: {e}"}
    try:
        raw, turns, _tools = await run_agent(key, text, gl, settings,
                                             jira_mcp=False, max_turns=max_turns, images=images)
    except AgentRunError as e:
        return {"key": key, "error": f"agent: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"key": key, "error": f"agent: {type(e).__name__}: {e}"}
    verdict = parse_verdict(raw, key)
    vr = verify_verdict(verdict, gl)
    return {"key": key, "turns_used": turns,
            "verify": {"passed": vr.passed, "total": vr.total},
            "verdict": verdict.to_dict()}


async def _run(rows: list[dict], concurrency: int, max_turns: int) -> list[dict]:
    settings = get_settings()
    gl = build_client(settings)
    jira = JiraClient(settings.jira_url, settings.jira_email, settings.jira_token)
    sem = asyncio.Semaphore(concurrency)

    async def guarded(r):
        async with sem:
            print(f"eval: running RCA for {r['key']} ...")
            res = await _one(r, jira, gl, settings, max_turns)
            print(f"eval: {r['key']} -> " + ("ERROR " + res["error"] if res.get("error")
                  else f"ok ({res['turns_used']} turns)"))
            return res

    return await asyncio.gather(*[guarded(r) for r in rows])


def main(argv=None) -> None:
    warn_no_api_key()
    p = argparse.ArgumentParser(description="Run the RCA agent over the pilot dataset.")
    p.add_argument("--concurrency", type=int, default=2, help="parallel RCA runs (keep small)")
    p.add_argument("--max-turns", type=int, default=60, help="per-run turn budget (production default)")
    p.add_argument("--limit", type=int, default=0, help="cap tickets from the dataset (0 = all)")
    a = p.parse_args(argv)

    rows = read_jsonl(DATA / "pilot.jsonl")
    if a.limit:
        rows = rows[:a.limit]
    print(f"eval: running RCA on {len(rows)} tickets (concurrency={a.concurrency}, "
          f"max_turns={a.max_turns}). Each run is minutes — this takes a while.")

    results = asyncio.run(_run(rows, a.concurrency, a.max_turns))

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RESULTS / f"run_{ts}.jsonl"
    write_jsonl(out, results)
    n_err = sum(1 for r in results if r.get("error"))
    print(f"eval: wrote {len(results)} results ({n_err} errored) -> {out}")


if __name__ == "__main__":
    main()
