"""Local ripgrep-based code search over cloned repos.

Requires:
  REPOS_DIR  — path to directory containing cloned repos
               e.g. /home/rishabh/rca-claude-agent/repos  (EC2)
               or   C:/Users/rishabh/Desktop/rca-agent/repos  (local)

Falls back gracefully if REPOS_DIR is not set or repo not found.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_MAX_MATCHES = 30
_TIMEOUT = 15  # seconds


def _repos_dir() -> Path | None:
    d = os.getenv("REPOS_DIR", "").strip()
    if not d:
        return None
    p = Path(d)
    return p if p.is_dir() else None


def _repo_path(project: str) -> Path | None:
    """Map 'mastersindia/arap-auth-service' -> repos/arap-auth-service."""
    base = _repos_dir()
    if not base:
        return None
    repo_name = project.split("/")[-1]
    p = base / repo_name
    return p if p.is_dir() else None


def search_code_local(project: str, query: str, scope: str | None = None) -> dict:
    """Ripgrep search over a cloned repo. Returns file:line matches.

    `scope` (a path prefix like "masters_india_saas/reconcile") restricts the search to
    that subtree when it exists in the repo — used by module profiles to stay inside one
    module (fewer wrong-file distractions, faster). If the scope path isn't present in
    this repo, the whole repo is searched and `scope_applied` comes back False.

    Falls back to {"error": "not available"} if REPOS_DIR not set or
    repo not cloned — caller should fall back to GitLab API search.
    """
    repo = _repo_path(project)
    if not repo:
        return {"error": "local search not available — REPOS_DIR not set or repo not cloned"}

    target, scope_applied = repo, False
    if scope:
        candidate = repo / scope
        if candidate.is_dir():
            target, scope_applied = candidate, True

    try:
        result = subprocess.run(
            ["rg", "--line-number", "--no-heading", "--max-count=3",
             "--max-filesize=1M", "-e", query, str(target)],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except FileNotFoundError:
        return {"error": "ripgrep (rg) not installed"}
    except subprocess.TimeoutExpired:
        return {"error": f"search timed out after {_TIMEOUT}s"}

    matches = []
    for line in result.stdout.splitlines():
        # Format: /path/to/file.py:42:matched line content
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        file_path = parts[0].replace(str(repo) + os.sep, "").replace("\\", "/")
        try:
            line_no = int(parts[1])
        except ValueError:
            continue
        matches.append({
            "file": file_path,
            "line": line_no,
            "content": parts[2].strip(),
        })
        if len(matches) >= _MAX_MATCHES:
            break

    return {"project": project, "query": query, "scope": scope,
            "scope_applied": scope_applied, "matches": matches}
