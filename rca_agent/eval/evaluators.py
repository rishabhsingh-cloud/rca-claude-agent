"""Phoenix evaluators for the RCA experiment.

PRIMARY (semantic) — `rca_matches`: an LLM judge compares the agent's RCA against
the curated `reference_rca` and rates it correct / partial / wrong / unknown. This
is the real score. Reuses the production-style judge from score.py.

SECONDARY (deterministic, only where the ground-truth row carries the tag) —
`repo_correct`, `cause_bucket_correct`, `regression_correct`: cheap objective
side-checks. They score None ("n/a") when the corresponding tag is absent.

Phoenix binds evaluator params by NAME (docs + client source): `output` = the task's
return value, `expected` = the dataset row's output (reference_rca + tags), plus
`reference` (alias for expected), `input`, `metadata`.

RETURN SHAPE: each evaluator returns a **tuple `(score, label, explanation)`** —
score is a float (or None for "not applicable"), label + explanation are strings.
This is the shape the Phoenix client parses into an EvaluationScore; returning a
plain dict is silently dropped (recorded as a None score) — that was the earlier bug.
"""

from __future__ import annotations

import asyncio
import re

from ..config import get_settings
from .score import _judge  # reuse the production-style LLM judge

# correct -> 1.0, partial -> 0.5, wrong -> 0.0, unknown -> None (unscored)
_RATING_SCORE = {"correct": 1.0, "partial": 0.5, "wrong": 0.0, "unknown": None}


def _verdict(output):
    """Return (verdict_dict, "") on success, or (None, error_message)."""
    if not isinstance(output, dict):
        return None, "task returned no dict"
    if output.get("error"):
        return None, str(output["error"])
    return output.get("verdict") or {}, ""


def rca_matches(output, expected):
    """The core semantic score: does the agent's RCA identify the same root cause
    as the human-written reference_rca? Returns (score, label, explanation)."""
    verdict, err = _verdict(output)
    if verdict is None:
        return (0.0, "error", err[:400])
    reference = (expected or {}).get("reference_rca", "") or ""
    j = asyncio.run(_judge(verdict, reference, get_settings().model))
    rating = j.get("rating", "unknown")
    return (_RATING_SCORE.get(rating), rating, (j.get("reason", "") or "")[:500])


# GitLab URL -> repo name, e.g. .../mastersindia/gst-enterprise-service/-/blob/... -> gst-enterprise-service
_REPO_RE = re.compile(r'/[^/]+/([^/]+)/-/(?:blob|commit|merge_requests|tree)/')


def _routed_repo(verdict) -> str | None:
    """Best-effort: the repo the agent pinned, read from its evidence URLs."""
    for ev in verdict.get("evidence_chain") or []:
        m = _REPO_RE.search(ev.get("url") or "")
        if m:
            return m.group(1)
    return None


def repo_correct(output, expected):
    verdict, err = _verdict(output)
    exp = (expected or {}).get("expected_repo")
    if not exp:
        return (None, "n/a", "no expected_repo tag")
    if verdict is None:
        return (0.0, "error", err[:200])
    got = _routed_repo(verdict)
    ok = bool(got) and got.lower() == str(exp).lower()
    return (1.0 if ok else 0.0, "match" if ok else "mismatch",
            f"expected {exp}, got {got or 'none'}")


def cause_bucket_correct(output, expected):
    verdict, err = _verdict(output)
    exp = (expected or {}).get("expected_bucket")
    if not exp:
        return (None, "n/a", "no expected_bucket tag")
    if verdict is None:
        return (0.0, "error", err[:200])
    got = verdict.get("cause_categories") or []
    ok = exp in got
    return (1.0 if ok else 0.0, "match" if ok else "mismatch",
            f"expected {exp} in {got}")


def regression_correct(output, expected):
    verdict, err = _verdict(output)
    exp = (expected or {}).get("is_regression")
    if exp is None:
        return (None, "n/a", "no is_regression tag")
    if verdict is None:
        return (0.0, "error", err[:200])
    got = verdict.get("is_regression")
    ok = (got == exp)
    return (1.0 if ok else 0.0, "match" if ok else "mismatch",
            f"expected {exp}, got {got}")


ALL_EVALUATORS = [rca_matches, repo_correct, cause_bucket_correct, regression_correct]
