"""Score a run against ground truth and surface failure patterns.

For each ticket, an LLM judge compares the agent's RCA (headline + probable root cause
+ suggested action) against the ground-truth human comments, and rates it
correct / partial / wrong / unknown. Alongside that we record cheap structured signals
(verify pass rate, confidence, whether a repo and introducing MR were pinned).

The aggregate + the per-ticket reasons are what tell us WHAT to change in the agent.
Reads the latest results/run_*.jsonl by default; writes results/score_<timestamp>.jsonl.
Requires ANTHROPIC_API_KEY.

Note: the judge prompt includes ticket comments, which may contain customer PII. That
stays between this process and the model (same trust boundary as the RCA agent itself);
the terminal report and committed files never print raw comment text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from ..config import get_settings
from ._common import DATA, RESULTS, latest, read_jsonl, require_api_key, write_jsonl

JUDGE_SYSTEM = """You grade an automated root-cause analysis (RCA) against ground truth.
The ground truth is the human discussion/resolution recorded on the ticket. Decide
whether the RCA's probable root cause matches what actually turned out to be the cause.

Output ONLY a single JSON object, no prose, no code fences:
  {"rating": "correct" | "partial" | "wrong" | "unknown", "reason": "<= 2 sentences"}
- correct: the RCA identifies the same root cause the humans landed on.
- partial: right area/component but wrong mechanism, or right cause stated with low
  confidence among wrong alternatives.
- wrong: the RCA blames something the ground truth contradicts.
- unknown: the ground truth does not actually reveal the real cause, so you cannot judge.
Judge only against the ground truth — do not use outside assumptions."""


def _parse_json(text: str) -> dict:
    raw = text.strip()
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e <= s:
        return {"rating": "unknown", "reason": "judge returned no JSON"}
    try:
        d = json.loads(raw[s:e + 1])
        return d if isinstance(d, dict) else {"rating": "unknown", "reason": "bad judge output"}
    except json.JSONDecodeError:
        return {"rating": "unknown", "reason": "unparseable judge output"}


async def _judge(verdict: dict, ground_truth: str, model: str) -> dict:
    if not ground_truth.strip():
        return {"rating": "unknown", "reason": "no ground-truth comments on this ticket"}
    prompt = (
        "# Agent RCA\n"
        f"Headline: {verdict.get('headline', '')}\n"
        f"Probable root cause: {verdict.get('probable_root_cause', '')}\n"
        f"Suggested action: {verdict.get('suggested_next_action', '')}\n\n"
        "# Ground truth (human comments on the resolved ticket)\n"
        f"{ground_truth}\n\n"
        "Grade the RCA. Output only the JSON object."
    )
    options = ClaudeAgentOptions(system_prompt=JUDGE_SYSTEM, model=model,
                                 permission_mode="default", max_turns=1)
    final = ""
    async for m in query(prompt=prompt, options=options):
        if isinstance(m, AssistantMessage):
            for b in m.content:
                if isinstance(b, TextBlock):
                    final = b.text
        elif isinstance(m, ResultMessage):
            final = getattr(m, "result", None) or final
    return _parse_json(final)


def _first_repo(verdict: dict) -> str:
    for ev in verdict.get("evidence_chain") or []:
        if ev.get("kind") in ("file_content", "stack_frame", "blame") and ev.get("url"):
            return ev["url"]
    return ""


async def _score(results: list[dict], gt: dict, model: str, concurrency: int) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)

    async def one(r):
        key = r["key"]
        if r.get("error"):
            return {"key": key, "rating": "error", "reason": r["error"]}
        v = r["verdict"]
        async with sem:
            j = await _judge(v, gt.get(key, {}).get("ground_truth_comments", ""), model)
        return {
            "key": key,
            "rating": j.get("rating", "unknown"),
            "reason": j.get("reason", ""),
            "confidence": v.get("confidence"),
            "cause_categories": v.get("cause_categories"),
            "verify": r.get("verify"),
            "pinned_repo": bool(_first_repo(v)),
            "introducing_mr": v.get("introducing_mr"),
        }

    return await asyncio.gather(*[one(r) for r in results])


def _report(scored: list[dict]) -> None:
    counts = Counter(s["rating"] for s in scored)
    n = len(scored)
    print("\n=== eval score ===")
    for rating in ("correct", "partial", "wrong", "unknown", "error"):
        if counts.get(rating):
            print(f"  {rating:8}: {counts[rating]:>3}  ({counts[rating] / n:.0%})")
    print(f"  {'total':8}: {n:>3}")
    # Per-ticket lines for anything not clearly correct — these are the patterns to act on.
    flagged = [s for s in scored if s["rating"] in ("wrong", "partial", "error")]
    if flagged:
        print("\n--- needs attention (drives agent changes) ---")
        for s in flagged:
            print(f"  {s['key']} [{s['rating']}] conf={s.get('confidence')} "
                  f"verify={s.get('verify')}: {s.get('reason', '')[:160]}")


def main(argv=None) -> None:
    require_api_key()
    p = argparse.ArgumentParser(description="Score an RCA run against ground truth.")
    p.add_argument("--run-file", default="", help="results/run_*.jsonl (default: latest)")
    p.add_argument("--concurrency", type=int, default=3, help="parallel judge calls")
    a = p.parse_args(argv)

    run_file = latest(RESULTS, "run_*.jsonl") if not a.run_file else __import__("pathlib").Path(a.run_file)
    results = read_jsonl(run_file)
    gt = {r["key"]: r for r in read_jsonl(DATA / "pilot.jsonl")}
    model = get_settings().model

    print(f"eval: scoring {len(results)} results from {run_file.name} ...")
    scored = asyncio.run(_score(results, gt, model, a.concurrency))

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RESULTS / f"score_{ts}.jsonl"
    write_jsonl(out, scored)
    _report(scored)
    print(f"\neval: wrote per-ticket scores -> {out}")


if __name__ == "__main__":
    main()
