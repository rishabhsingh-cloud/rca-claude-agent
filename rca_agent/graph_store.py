"""Build / load / cache the per-repo graph.

Resolution order for a project's graph:
  1. A persisted graph map at fixtures/graphs/<project>.json (SHA-stamped) —
     this is what the indexer (Step 6) would publish; load it directly.
  2. Otherwise build it on the fly from the repo's Python sources:
       - mock backend: read fixtures/gitlab/<project>/files/**.py from disk.
       - live backend: not wired yet (the indexer builds + publishes #1).

The persisted artifact lets the RCA agent compare the graph's SHA to current
HEAD and gauge staleness, exactly like the summary provenance stamp.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import FIXTURES_DIR, Settings, index_dir
from .graph import RepoGraph, build_graph_from_sources
from .graphify_adapter import from_graphify_json

# project -> (source-file mtime when cached, graph). The mtime lets a long-lived
# process (the webapp) notice when the nightly re-indexer rewrites the graph JSON
# on disk and reload it, instead of serving the snapshot it loaded at startup.
_cache: dict[str, tuple[float | None, RepoGraph]] = {}


def _graph_artifact(project: str) -> Path:
    return index_dir() / "graphs" / (project.replace("/", "__") + ".json")


def _graphify_export(project: str) -> Path:
    # A graphify `graph.json` exported for this project (multi-language / large
    # repos where the AST engine doesn't apply). Drop a `"directed": true` key
    # in the JSON if it was built with graphify --directed.
    return index_dir() / "graphs" / (project.replace("/", "__") + ".graphify.json")


def _mock_files_dir(project: str) -> Path:
    return FIXTURES_DIR / "gitlab" / project.replace("/", "__") / "files"


def read_mock_sources(project: str) -> dict[str, str]:
    """{repo-relative path: source} for every Python file in the mock repo."""
    base = _mock_files_dir(project)
    out: dict[str, str] = {}
    if not base.exists():
        return out
    for fp in base.rglob("*.py"):
        rel = fp.relative_to(base).as_posix()
        out[rel] = fp.read_text(encoding="utf-8")
    return out


# Auto-generated / vendored paths that pollute a call graph with no useful edges.
_NOISE = ("/migrations/", "/__pycache__/", "/site-packages/", "/.venv/",
          "/venv/", "/node_modules/", "/.tox/")


def read_sources_via_client(client, project: str, ref: str = "main") -> dict[str, str]:
    """{path: source} for the repo's real source files, enumerated + fetched
    through the GitLabClient (live or mock). Skips migrations / caches / vendored
    code so the graph reflects application code, not generated noise."""
    out: dict[str, str] = {}
    for p in client.list_source_files(project, ref):
        if any(n in ("/" + p) for n in _NOISE):
            continue
        out[p] = client.get_file(project, ref, p)
    return out


def build_repo_graph(project: str, client=None, settings: Settings | None = None,
                     sha: str | None = None, ref: str = "main") -> RepoGraph:
    """Build the graph from source. With a client, enumerate + fetch via GitLab
    (live or mock). Without one, read the mock fixture files from disk."""
    files = read_sources_via_client(client, project, ref) if client is not None \
        else read_mock_sources(project)
    return build_graph_from_sources(project, files, sha=sha)


def _artifact_mtime(art: Path, gfy: Path) -> float | None:
    """Modification time of whichever on-disk artifact load_repo_graph would use,
    or None when the graph is built from source (nothing on disk to watch)."""
    src = art if art.exists() else (gfy if gfy.exists() else None)
    return src.stat().st_mtime if src else None


def load_repo_graph(project: str, settings: Settings | None = None) -> RepoGraph:
    """Cached graph for a project, in order of preference:
      1. published AST graph map (directed, precise) — fixtures/graphs/<proj>.json
      2. graphify export (multi-language bridge)      — <proj>.graphify.json
      3. built from source on the fly

    The cache is invalidated when the on-disk artifact's mtime changes, so a
    long-lived process picks up a nightly re-index without a restart.
    """
    art = _graph_artifact(project)
    gfy = _graphify_export(project)
    mtime = _artifact_mtime(art, gfy)

    cached = _cache.get(project)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    if art.exists():
        g = RepoGraph.from_json(json.loads(art.read_text(encoding="utf-8")))
    elif gfy.exists():
        data = json.loads(gfy.read_text(encoding="utf-8"))
        g = from_graphify_json(data, project=project, sha=data.get("sha"),
                               directed=data.get("directed"),
                               path_strip=data.get("path_strip", ""))
    else:
        g = build_repo_graph(project, settings)

    _cache[project] = (mtime, g)
    return g


def clear_cache() -> None:
    _cache.clear()
