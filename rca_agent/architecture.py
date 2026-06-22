"""Cross-service architecture reference.

A single platform-wide document (not per-repo) describing how the services fit
together: request topology, the symptom→boundary failure table, the inter-service
dependency map (Kafka topics / HTTP proxies), end-to-end flows, and cross-cutting
RCA notes. This is the layer per-repo summaries and code graphs can't provide —
most real bugs surface in one service but originate across a boundary in another.

Lives at `<index_dir>/architecture.md`. Two access paths:
  - orientation_excerpt(): the compact "always-on" map for the system prompt.
  - search_architecture(query): pull the most relevant sections on demand.

Like routing/summaries, this is a HYPOTHESIS source — its file:line refs are from
a static read and must be confirmed against live GitLab before any conclusion.
"""

from __future__ import annotations

import re

from .config import index_dir

# Section headings to surface in the always-on prompt excerpt.
_ORIENTATION_KEYS = (
    "rca quick orientation",
    "inter-project dependencies",
    "appendix",
)


def architecture_path():
    return index_dir() / "architecture.md"


def load_architecture() -> str | None:
    p = architecture_path()
    return p.read_text(encoding="utf-8") if p.exists() else None


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split the doc into (heading, body) on markdown headings (# / ## / ###)."""
    sections: list[tuple[str, str]] = []
    head, lines = None, []
    for line in text.splitlines():
        if re.match(r"^#{1,3} ", line):
            if head is not None:
                sections.append((head, "\n".join(lines)))
            head = line.lstrip("#").strip()
            lines = [line]
        else:
            lines.append(line)
    if head is not None:
        sections.append((head, "\n".join(lines)))
    return sections


def orientation_excerpt() -> str:
    """Compact cross-service map for the system prompt (topology + failure-boundary
    table + dependency map + cross-cutting notes). Empty string if no doc."""
    text = load_architecture()
    if not text:
        return ""
    keep = [body for head, body in _split_sections(text)
            if any(k in head.lower() for k in _ORIENTATION_KEYS)]
    return "\n\n".join(keep).strip()


def search_architecture(query: str, max_sections: int = 3) -> list[dict]:
    """Return the architecture sections most relevant to `query` (by term
    frequency). Sections are truncated so results stay context-friendly."""
    text = load_architecture()
    if not text:
        return []
    terms = [t for t in re.findall(r"[a-z0-9_]+", query.lower()) if len(t) > 2]
    if not terms:
        return []
    scored = []
    for head, body in _split_sections(text):
        hay = body.lower()
        score = sum(hay.count(t) for t in terms)
        if score:
            scored.append((score, head, body))
    scored.sort(key=lambda x: -x[0])
    return [{"heading": h, "text": b[:1800]} for _, h, b in scored[:max_sections]]
