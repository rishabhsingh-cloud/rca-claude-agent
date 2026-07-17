"""Shared helpers for the eval harness: paths, the auth warning, JSONL I/O."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).parent
DATA = _HERE / "data"
RESULTS = _HERE / "results"


def warn_no_api_key() -> None:
    """Warn (do NOT block) when ANTHROPIC_API_KEY is unset.

    Production runs the agent on a Claude subscription (OAuth session), not an API
    key, and the agent subprocess inherits that ambient auth — so a key is NOT
    required. We only warn, because a LARGE batch on a subscription can hit
    interactive-tier rate limits mid-run; for a small eval set it's fine.
    """
    if os.getenv("ANTHROPIC_API_KEY"):
        return
    print(
        "eval: no ANTHROPIC_API_KEY — using the ambient Claude auth (subscription). "
        "Fine for a small set; a large batch may hit interactive rate limits mid-run.",
        file=sys.stderr,
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
