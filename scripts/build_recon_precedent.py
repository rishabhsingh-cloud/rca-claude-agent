"""Build the "precedent" corpus (RAG) from RESOLVED AUT tickets.

For each resolved ticket, fetch the full thread (JiraClient), distill it into a masked
Q&A row via ONE LLM pass, run a regex PII/amount safety-net, and append the masked row
to a JSONL corpus. Raw ticket content never leaves this process — only masked rows are
written, and stdout shows key/confidence/bucket only (no PII).

Runs locally (needs VPN for Jira + the Claude subscription for the distill calls).
Scoped to project = AUT ONLY. All modules by default (module kept as a per-row field);
pass --module to restrict. Concurrent + resumable: re-running skips keys already in the
output file, so a rate-limit hiccup mid-batch is just a re-run away, and future runs pick
up only newly-resolved tickets (a living corpus).

  # try a few
  .venv/bin/python scripts/build_recon_precedent.py --months 6 --limit 10
  # full 6-month, all modules, ~5 in parallel
  .venv/bin/python scripts/build_recon_precedent.py --months 6 --concurrency 5
  # just reconciliation
  .venv/bin/python scripts/build_recon_precedent.py --months 6 --module Reconcillation

Output: data/recon_precedent.jsonl  (gitignored — masked, but internal data).
Delete the file first to rebuild from scratch; otherwise it appends/skips.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter
from pathlib import Path

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage,
                              TextBlock, query)

from rca_agent.config import get_settings
from rca_agent.jira import JiraClient

MODULE_CF = "customfield_11727"  # Jira "Module" select field

DISTILL_SYSTEM = """You build a PII-masked knowledge-base entry from a RESOLVED Jira ticket for a GST software product. Read the ticket (summary, description, comment thread) and output ONE JSON object:
{"symptom":"1-2 sentences: the reported/observed problem",
 "root_cause":"the TRUE underlying cause per the resolution/discussion; empty string if none stated",
 "cause_confidence":"one of: high | medium | low | none. high=cause clearly established/confirmed (code identified, fix merged); medium=likely cause stated but not fully confirmed; low=only a suspicion/hypothesis or closed without confirming; none=no cause discussed",
 "fix":"what resolved it (code/config/data/user action); empty if not stated",
 "keywords":["short technical search tokens: error codes, exception/class names, endpoints, reco terms like 2A-PR/IMS-PR"],
 "cause_bucket":"one of: code | data | user_side | infra | config",
 "repo":"service most implicated e.g. gst-enterprise-service; empty if unclear",
 "is_regression":true or false,
 "fix_ref":"MR/commit cited as the fix e.g. !3809; empty if none",
 "introducing_mr":"MR/commit that introduced the bug if cited; empty if none"}

CRITICAL - MASK ALL PII AND FINANCIAL AMOUNTS in every text field: organization/company/customer/person name -> [ORG]; GSTIN -> [GSTIN]; email -> [EMAIL]; phone -> [PHONE]; invoice/document/IRN/job/reference number or any long id -> [DOC]; monetary amounts / totals / tax values -> [AMOUNT]. Preserve technical meaning, drop identifying+financial values. NEVER emit a real GSTIN, name, email, phone, document number, or rupee amount. Output ONLY the JSON object."""

_ALL = [(re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]{2}\b"), "[GSTIN]"),
        (re.compile(r"[\w.\-+]+@[\w.\-]+\.\w+"), "[EMAIL]"),
        (re.compile(r"\b(?:\+?91[\-\s]?)?[6-9]\d{9}\b"), "[PHONE]"),
        (re.compile(r"(?i)(?:₹|rs\.?|inr)\s*[\d,]+(?:\.\d{1,2})?"), "[AMOUNT]"),
        (re.compile(r"\b\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?\b"), "[AMOUNT]"),
        (re.compile(r"\b\d{5,}\.\d{1,2}\b"), "[AMOUNT]"),
        (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[IRN]"),
        (re.compile(r"\b\d{8,}\b"), "[DOC]")]
_LIGHT = _ALL[:3]


def _mask(v: str, pats) -> str:
    for p, r in pats:
        v = p.sub(r, v)
    return v


def _parse_json(s: str):
    a, b = s.find("{"), s.rfind("}")
    try:
        return json.loads(s[a:b + 1])
    except Exception:
        return None


async def _distill(key: str, module: str, resolved: str, text: str, max_turns: int) -> str:
    prompt = (f"Ticket {key} (module={module or 'n/a'}, resolved {resolved}).\n"
              f"Content (summary, description, comments):\n\n{text[:12000]}\n\n"
              "Output the JSON object.")
    opts = ClaudeAgentOptions(system_prompt=DISTILL_SYSTEM, model=get_settings().model,
                              permission_mode="default", max_turns=max_turns)
    final = ""
    async for m in query(prompt=prompt, options=opts):
        if isinstance(m, AssistantMessage):
            for b in m.content:
                if isinstance(b, TextBlock):
                    final = b.text
        elif isinstance(m, ResultMessage):
            final = getattr(m, "result", None) or final
    return final


def _row(key: str, module: str, resolved: str, r: dict) -> dict:
    return {"ticket_key": key, "resolved_date": resolved, "module": module,
            "repo": _mask(str(r.get("repo", "")), _LIGHT),
            "cause_bucket": r.get("cause_bucket", ""),
            "cause_confidence": str(r.get("cause_confidence", "")).lower(),
            "is_regression": r.get("is_regression"),
            "fix_ref": r.get("fix_ref", ""), "introducing_mr": r.get("introducing_mr", ""),
            "symptom": _mask(str(r.get("symptom", "")), _ALL),
            "root_cause": _mask(str(r.get("root_cause", "")), _ALL),
            "fix": _mask(str(r.get("fix", "")), _ALL),
            "keywords": [_mask(str(k), _LIGHT) for k in (r.get("keywords") or [])]}


def _fetch_todo(jira: JiraClient, jql: str, limit: int) -> list[dict]:
    """Paginate the AUT search (metadata fields only) -> list of {key, resolved, module}."""
    out, token, pages = [], None, 0
    while pages < 60:
        params = {"jql": jql, "maxResults": 100, "fields": f"resolutiondate,{MODULE_CF}"}
        if token:
            params["nextPageToken"] = token
        r = jira._client.get(f"{jira._base}/search/jql", params=params)
        r.raise_for_status()
        d = r.json()
        for i in d.get("issues", []):
            f = i.get("fields", {})
            mod = f.get(MODULE_CF)
            out.append({"key": i["key"],
                        "resolved": (f.get("resolutiondate") or "")[:10],
                        "module": (mod.get("value") if isinstance(mod, dict) else mod) or ""})
            if limit and len(out) >= limit:
                return out
        token = d.get("nextPageToken")
        pages += 1
        if not token or not d.get("issues"):
            break
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description="Build the AUT precedent corpus from resolved tickets.")
    ap.add_argument("--months", type=int, default=6, help="lookback window on resolution date")
    ap.add_argument("--module", default="", help="restrict to one Module value (default: all)")
    ap.add_argument("--limit", type=int, default=0, help="cap tickets (0 = all)")
    ap.add_argument("--concurrency", type=int, default=5, help="parallel distill calls")
    ap.add_argument("--max-turns", type=int, default=3, help="turn budget per distill (>=2 avoids cut-offs)")
    ap.add_argument("--out", default="data/recon_precedent.jsonl", help="output JSONL path")
    a = ap.parse_args()

    s = get_settings()
    if not s.has_jira:
        raise SystemExit("Jira not configured (JIRA_URL / JIRA_EMAIL / JIRA_TOKEN).")
    jira = JiraClient(s.jira_url, s.jira_email, s.jira_token)

    weeks = round(a.months * 4.345)
    clauses = ['project = AUT', 'issuetype in (Bug, Incident)', 'statusCategory = Done',
               f'resolved >= "-{weeks}w"']
    if a.module:
        clauses.append(f'cf[11727] = "{a.module}"')
    jql = " AND ".join(clauses) + " ORDER BY resolved DESC"

    todo = _fetch_todo(jira, jql, a.limit)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out.exists():
        for line in out.open(encoding="utf-8"):
            try:
                done.add(json.loads(line)["ticket_key"])
            except Exception:
                pass
    todo = [t for t in todo if t["key"] not in done]
    print(f"matched (AUT, {a.months}mo, module={a.module or 'ALL'}): {len(todo) + len(done)} "
          f"| already done: {len(done)} | to ingest now: {len(todo)} "
          f"| concurrency={a.concurrency}", flush=True)

    sem = asyncio.Semaphore(a.concurrency)
    lock = asyncio.Lock()
    counts, n = Counter(), [0]
    fh = out.open("a", encoding="utf-8")

    async def worker(t: dict):
        key = t["key"]
        async with sem:
            try:
                _, text = await asyncio.to_thread(jira.get, key, False, False)
                parsed = _parse_json(await _distill(key, t["module"], t["resolved"], text, a.max_turns))
            except Exception as e:  # noqa: BLE001
                print(f"  {key}: ERROR {type(e).__name__}: {str(e)[:100]} (re-run to retry)", flush=True)
                return
            if not parsed:
                print(f"  {key}: unparseable — skipped (re-run to retry)", flush=True)
                return
            row = _row(key, t["module"], t["resolved"], parsed)
            async with lock:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                counts[row["cause_confidence"] or "empty"] += 1
                n[0] += 1
                print(f"  [{n[0]}/{len(todo)}] {key}: conf={row['cause_confidence']} "
                      f"bucket={row['cause_bucket']} module={row['module'] or '-'}", flush=True)

    try:
        await asyncio.gather(*(worker(t) for t in todo))
    finally:
        fh.close()
    print(f"DONE: wrote {n[0]} new rows -> {out} (total keys now: {len(done) + n[0]})", flush=True)
    print(f"confidence spread (this run): {dict(counts)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
