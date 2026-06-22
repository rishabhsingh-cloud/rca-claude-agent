"""Deterministic repo-summary generator.

Produces the canonical summary `.md` from a repo's graph + file tree. Anchored to
DETERMINISTIC inputs (the symbols actually present), never to a previous summary
— this prevents the compounding drift the design warns about. The auto block is
machine-owned; an existing human block is preserved verbatim.

The summary answers "which repo / which area" for routing; the graph answers
"how do these connect". Both come from the same AST pass, so they can't drift
apart.
"""

from __future__ import annotations

import re

from .graph import RepoGraph

AUTO_START = "<!-- AUTO-GENERATED BLOCK (indexer owns this; do not hand-edit) -->"
AUTO_END = "<!-- END AUTO-GENERATED BLOCK -->"
HUMAN_START = "<!-- HUMAN BLOCK (agent never touches) -->"
HUMAN_END = "<!-- END HUMAN BLOCK -->"

_STOP = {"self", "cls", "the", "and", "for", "init"}


def extract_human_block(existing_md: str | None) -> str | None:
    """Pull the human-maintained block out of an existing summary, if any."""
    if not existing_md:
        return None
    m = re.search(re.escape(HUMAN_START) + r"(.*?)" + re.escape(HUMAN_END),
                  existing_md, re.DOTALL)
    return m.group(1).strip() if m else None


def _keywords(graph: RepoGraph) -> list[str]:
    words: set[str] = set()
    for n in graph.nodes.values():
        for part in re.split(r"[_.]", n.name):
            p = part.lower()
            if len(p) > 2 and p not in _STOP:
                words.add(p)
        for seg in re.split(r"[/.]", n.file):
            s = seg.removesuffix("py").strip(".").lower()
            if len(s) > 2 and s not in _STOP:
                words.add(s)
    return sorted(words)


def generate_summary(graph: RepoGraph, sha: str | None = None,
                     generated_at: str = "", existing_md: str | None = None) -> str:
    """Render the canonical summary markdown for `graph`."""
    repo_name = graph.project.split("/")[-1]

    # Group symbols by file -> areas.
    by_file: dict[str, list] = {}
    for n in graph.nodes.values():
        by_file.setdefault(n.file, []).append(n)

    area_lines = []
    for f in sorted(by_file):
        syms = sorted(by_file[f], key=lambda n: n.line)
        names = ", ".join(f"`{s.qualname}`" for s in syms)
        area_lines.append(f"- `{f}` — {names}")

    # Entry points: functions/methods nothing else in-repo calls (handlers/API).
    called = {e.dst for e in graph.edges}
    entry = sorted(
        n.qualname for n in graph.nodes.values()
        if n.kind in ("function", "method") and n.id not in called
    )

    auto = [
        AUTO_START,
        f"project: {graph.project}",
        f"sha: {sha or graph.sha or 'unknown'}",
        f"generated_at: {generated_at or 'unknown'}",
        "",
        f"# {repo_name}",
        "",
        "Auto-generated structural summary (symbols + areas from the code graph).",
        "",
        "## Areas",
        *(area_lines or ["- (no Python symbols found)"]),
        "",
        "## Entry points",
        *([f"- `{e}`" for e in entry] or ["- (none)"]),
        "",
        "## Keywords",
        " ".join(_keywords(graph)) or "(none)",
        "",
        AUTO_END,
    ]

    human = extract_human_block(existing_md)
    parts = ["\n".join(auto)]
    if human:
        parts += ["", HUMAN_START, human, HUMAN_END]
    return "\n".join(parts) + "\n"
