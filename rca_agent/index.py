"""Indexer — builds the graph map + repo summary from a repo's sources.

This is the deterministic core of the design's SEPARATE indexer agent: it writes
the index (graph map + summary `.md`), so the RCA agent never needs write access.
It anchors to the live file tree (not the old summary) to avoid drift, and stamps
the source SHA for staleness checks.

CLI:
  python -m rca_agent.index acme/billing-service --sha <sha> --date 2026-06-20

For the mock backend it reads sources from fixtures/gitlab/<project>/files/.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import get_settings, index_dir
from .gitlab_client import build_client
from .graph_store import build_repo_graph
from .summarize import generate_summary


def _summary_path(project: str) -> Path:
    return index_dir() / "repo_summaries" / (project.replace("/", "__") + ".md")


def _graph_path(project: str) -> Path:
    return index_dir() / "graphs" / (project.replace("/", "__") + ".json")


def build_index(project: str, sha: str | None = None, date: str = "",
                ref: str = "main") -> tuple[Path, Path]:
    """Build + write the graph map and summary. Returns (graph_path, summary_path).

    Enumerates + fetches sources through the configured GitLab backend (mock or
    live), so live indexing is just `RCA_BACKEND=live` + credentials.
    """
    client = build_client(get_settings())
    graph = build_repo_graph(project, client=client, sha=sha, ref=ref)

    gp = _graph_path(project)
    gp.parent.mkdir(parents=True, exist_ok=True)
    gp.write_text(json.dumps(graph.to_json(), indent=2), encoding="utf-8")

    sp = _summary_path(project)
    existing = sp.read_text(encoding="utf-8") if sp.exists() else None
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(generate_summary(graph, sha=sha, generated_at=date,
                                   existing_md=existing), encoding="utf-8")
    return gp, sp


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rca_agent.index", description="RCA repo indexer")
    ap.add_argument("project", help="GitLab project path, e.g. acme/billing-service")
    ap.add_argument("--sha", help="source SHA the index is built from")
    ap.add_argument("--date", default="", help="generated_at stamp (YYYY-MM-DD)")
    ap.add_argument("--ref", default="main", help="git ref to index (live backend)")
    args = ap.parse_args(argv)

    gp, sp = build_index(args.project, sha=args.sha, date=args.date, ref=args.ref)
    g = json.loads(gp.read_text(encoding="utf-8"))
    print(f"Indexed {args.project}: {len(g['nodes'])} symbols, {len(g['edges'])} edges")
    print(f"  graph   -> {gp}")
    print(f"  summary -> {sp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
