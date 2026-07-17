"""Ground-truth dataset for the Phoenix eval: load the curated file, upload it to
Phoenix as a Dataset, and (optionally) seed draft candidates from Jira.

The ground truth is a SMALL, hand-curated set — the team just started using the
agent, so there's little labeled data; we grow it over time. Each row is
prose-first, because an RCA is prose, not a rigid schema:

    {"ticket_key":    "AUT-1234",              # required — the ONLY input
     "reference_rca": "<a paragraph or two: the TRUE root cause (+ fix if known)>",
     "expected_repo":   "gst-enterprise-service",  # optional tag
     "expected_bucket": "code",                     # optional tag
     "is_regression":   true,                        # optional tag
     "tags":            ["hsn", "regression"]}       # optional freeform

Only `ticket_key` + `reference_rca` are required. The eval fetches the live ticket
itself (same as prod), so we never hand-format the ticket text.

The real file lives at data/ground_truth.jsonl (gitignored — its prose may quote
customer data). ground_truth.example.jsonl (committed, fake) documents the format.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..config import get_settings
from ..jira import JiraClient
from ..tickets import flatten_adf
from ._common import DATA
from .phoenix_client import client

GROUND_TRUTH = DATA / "ground_truth.jsonl"
DRAFT = DATA / "ground_truth.draft.jsonl"
DEFAULT_DATASET = "rca-ground-truth"


# ---------------------------------------------------------------------------
# Load + validate the curated ground truth
# ---------------------------------------------------------------------------

def load_ground_truth(path: Path = GROUND_TRUTH) -> list[dict]:
    if not path.exists():
        raise SystemExit(
            f"eval: {path} not found.\n"
            f"Create it (format: rca_agent/eval/ground_truth.example.jsonl), or run\n"
            f"  python -m rca_agent.eval seed\n"
            f"to draft candidates from Jira, then curate them into {path.name}.")
    rows, bad = [], []
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError as e:
                bad.append(f"line {i}: invalid JSON ({e})")
                continue
            if not r.get("ticket_key") or not (r.get("reference_rca") or "").strip():
                bad.append(f"line {i}: needs ticket_key + non-empty reference_rca")
                continue
            rows.append(r)
    if bad:
        raise SystemExit("eval: ground truth has problems:\n  " + "\n  ".join(bad))
    if not rows:
        raise SystemExit(f"eval: {path} has no usable rows.")
    return rows


def _to_columns(rows: list[dict]):
    """Split rows into Phoenix's parallel input/output/metadata columns.

    input = what the task receives; output = the reference answer the evaluators
    score against; metadata = freeform (not scored)."""
    inputs = [{"ticket_key": r["ticket_key"]} for r in rows]
    outputs = [{
        "reference_rca": r["reference_rca"],
        "expected_repo": r.get("expected_repo"),
        "expected_bucket": r.get("expected_bucket"),
        "is_regression": r.get("is_regression"),
    } for r in rows]
    metadata = [{"tags": r.get("tags", [])} for r in rows]
    return inputs, outputs, metadata


def upload(name: str, append: bool):
    rows = load_ground_truth()
    inputs, outputs, metadata = _to_columns(rows)
    c = client()
    if append:
        ds = c.datasets.add_examples_to_dataset(
            dataset=name, inputs=inputs, outputs=outputs, metadata=metadata)
        print(f"eval: appended {len(rows)} example(s) to Phoenix dataset '{name}'.")
    else:
        ds = c.datasets.create_dataset(
            name=name, inputs=inputs, outputs=outputs, metadata=metadata,
            dataset_description="Curated RCA ground truth: prose reference_rca + optional tags.")
        print(f"eval: uploaded {len(rows)} example(s) as Phoenix dataset '{name}'.")
    print("Next: python -m rca_agent.eval run")
    return ds


def upload_main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Upload ground_truth.jsonl to Phoenix.")
    p.add_argument("--name", default=DEFAULT_DATASET, help="Phoenix dataset name")
    p.add_argument("--append", action="store_true",
                   help="append rows to an existing dataset instead of creating it")
    a = p.parse_args(argv)
    upload(a.name, a.append)


# ---------------------------------------------------------------------------
# Optional: seed draft candidates from resolved Jira tickets (you then curate)
# ---------------------------------------------------------------------------

def _comment_text(comments: list[dict]) -> str:
    parts = []
    for c in comments:
        body = c.get("body")
        txt = flatten_adf(body) if isinstance(body, dict) else (body or "")
        author = (c.get("author") or {}).get("displayName", "")
        if txt.strip():
            parts.append(f"[{author}] {txt.strip()}")
    return "\n\n".join(parts)


def seed(limit: int, weeks: int) -> None:
    """Draft candidate rows from resolved AUT tickets. We DON'T auto-label — we just
    pull the ticket + its human discussion so YOU can write the true root cause into
    `reference_rca`. Writes data/ground_truth.draft.jsonl (gitignored)."""
    s = get_settings()
    if not s.has_jira:
        raise SystemExit("eval: Jira not configured (JIRA_URL / JIRA_EMAIL / JIRA_TOKEN).")
    jira = JiraClient(s.jira_url, s.jira_email, s.jira_token)
    jql = (f'project = AUT AND issuetype in (Bug, Incident) AND statusCategory = Done '
           f'AND created >= "-{weeks}w" ORDER BY created DESC')
    issues = jira.search(jql, max_results=limit)

    drafts = []
    for i in issues:
        f = i.get("fields", {})
        comments = (f.get("comment") or {}).get("comments", []) or []
        drafts.append({
            "ticket_key": i["key"],
            "reference_rca": "",  # <- YOU fill this from _comments_for_reference below
            "_summary": f.get("summary", ""),
            "_comments_for_reference": _comment_text(comments),
        })

    DRAFT.parent.mkdir(parents=True, exist_ok=True)
    with DRAFT.open("w", encoding="utf-8") as fh:
        for d in drafts:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"eval: wrote {len(drafts)} draft candidate(s) -> {DRAFT}")
    print("Next: for each row, read `_comments_for_reference`, write the true root "
          "cause into `reference_rca`, drop the `_`-prefixed helper fields and any rows "
          f"you don't want, and save as {GROUND_TRUTH.name}. Then run `upload`.")


def seed_main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Draft ground-truth candidates from Jira.")
    p.add_argument("--limit", type=int, default=20, help="max tickets to pull")
    p.add_argument("--weeks", type=int, default=26, help="lookback window (26 ~= 6 months)")
    a = p.parse_args(argv)
    seed(a.limit, a.weeks)
