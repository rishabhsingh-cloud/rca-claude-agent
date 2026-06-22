"""route_repo — narrow a vague ticket to candidate repos using local summaries.

This is the cheap, offline first step: it compensates for crude self-hosted
search by shrinking the search space before any GitLab call. It is a HYPOTHESIS
generator only — the repo it picks is confirmed when we actually fetch the
suspect file from GitLab and find the referenced symbol there.

Scoring is deterministic: token overlap between the ticket (incl. any stack-trace
file paths, which are strong signals) and each repo summary. No model involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import index_dir

_WORD = re.compile(r"[A-Za-z0-9_]+")
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "this", "that", "it", "with", "when", "after", "error", "issue", "bug",
}


@dataclass(frozen=True)
class RepoCandidate:
    project: str       # GitLab project path, e.g. "acme/billing-service"
    score: float
    matched: list[str]  # tokens that drove the match (for transparency)
    summary_path: str


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _WORD.findall(text or "") if t.lower() not in _STOP and len(t) > 1}


def _path_tokens(text: str) -> set[str]:
    """Path segments from any file paths in the text — strong routing signal."""
    out: set[str] = set()
    for m in re.finditer(r"[\w./\-]+\.[A-Za-z0-9]+", text or ""):
        for seg in re.split(r"[/\\.]", m.group(0)):
            if seg and seg.lower() not in _STOP and len(seg) > 1:
                out.add(seg.lower())
    return out


def _summaries_dir() -> Path:
    return index_dir() / "repo_summaries"


def read_summary(project: str) -> str | None:
    """The full human-authored RCA summary for a repo, if indexed."""
    fp = _summaries_dir() / (project.replace("/", "__") + ".md")
    return fp.read_text(encoding="utf-8") if fp.exists() else None


def _load_summaries() -> list[tuple[str, str, str]]:
    """(project, summary_text, summary_path) for each fixture summary .md.

    The project path is read from a `project:` line in the summary, falling back
    to the filename with '__' -> '/'.
    """
    out = []
    d = _summaries_dir()
    if not d.exists():
        return out
    for fp in sorted(d.glob("*.md")):
        text = fp.read_text(encoding="utf-8")
        m = re.search(r"(?im)^\s*project:\s*(\S+)", text)
        project = m.group(1) if m else fp.stem.replace("__", "/")
        out.append((project, text, str(fp)))
    return out


def route_repo(ticket_text: str, top_k: int = 3) -> list[RepoCandidate]:
    """Rank candidate repos for a ticket. Path tokens are weighted higher."""
    summaries = _load_summaries()
    if not summaries:
        return []

    tkt = _tokens(ticket_text)
    tkt_paths = _path_tokens(ticket_text)

    ranked: list[RepoCandidate] = []
    for project, text, path in summaries:
        s_tokens = _tokens(text)
        word_hits = tkt & s_tokens
        path_hits = tkt_paths & s_tokens
        # Path-derived overlaps count triple — a trace naming "billing/invoice.py"
        # is a far stronger signal than a shared common word.
        score = len(word_hits) + 3.0 * len(path_hits)
        if score > 0:
            ranked.append(RepoCandidate(
                project=project, score=score,
                matched=sorted(path_hits | word_hits),
                summary_path=path,
            ))

    ranked.sort(key=lambda c: c.score, reverse=True)
    return ranked[:top_k]
