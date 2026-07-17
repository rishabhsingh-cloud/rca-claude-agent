"""Shared helpers for the eval harness: paths, the API-key guard, JSONL I/O."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).parent
DATA = _HERE / "data"
RESULTS = _HERE / "results"


def require_api_key() -> None:
    """Hard stop unless ANTHROPIC_API_KEY is set.

    Batch work must run on the API key, not a claude.ai/OAuth subscription profile —
    a backfill would hammer interactive-tier rate limits and fail mid-run. This guard
    is deliberate: it stops the eval from silently drawing down a subscription.
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit(
            "eval: refusing to run — ANTHROPIC_API_KEY is not set.\n"
            "Batch RCA must use the API key (a subscription/OAuth profile would hit "
            "rate limits and fail mid-run). Export ANTHROPIC_API_KEY and re-run."
        )


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"eval: {path} not found — run the earlier step first.")
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")


def latest(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if not matches:
        sys.exit(f"eval: no files matching {pattern} in {directory} — run it first.")
    return matches[-1]
