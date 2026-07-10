"""Nightly re-indexer — rebuilds call graphs + repo summaries for all live repos.

Run manually:  python -m rca_agent.reindex
Runs on EC2 every 2 nights via systemd timer (rca-reindex.timer).

Branches are resolved automatically via GitLab default_ref() so the script
doesn't break when branches are renamed.
"""
from __future__ import annotations

import os
import subprocess
import time
from datetime import date
from pathlib import Path

from .config import get_settings
from .gitlab_client import build_client
from .index import build_index

REPOS = [
    "mastersindia/arap-auth-service",
    "mastersindia/background-processes",
    "mastersindia/gst-enterprise-service",
    "mastersindia/gst-prefect-app",
]


def _log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _git_pull_repos() -> None:
    repos_dir = os.getenv("REPOS_DIR", "").strip()
    if not repos_dir:
        _log("git pull: REPOS_DIR not set, skipping")
        return
    for project in REPOS:
        repo_name = project.split("/")[-1]
        repo_path = Path(repos_dir) / repo_name
        if not repo_path.is_dir():
            _log(f"git pull: {repo_name} not cloned, skipping")
            continue
        try:
            result = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=repo_path, capture_output=True, text=True, timeout=60,
            )
            _log(f"git pull {repo_name}: {result.stdout.strip() or 'ok'}")
        except Exception as e:
            _log(f"git pull {repo_name}: FAILED — {e}")


def reindex_all() -> None:
    settings = get_settings()
    client = build_client(settings)
    today = date.today().isoformat()
    failed = []

    _git_pull_repos()
    _log(f"Starting re-index for {len(REPOS)} repos")
    for project in REPOS:
        try:
            ref = client.default_ref(project)
            # Resolve the branch to the exact commit we're indexing and STAMP the
            # graph/summary with it, so the RCA tools can report how stale the map
            # is (graph_sha). Best-effort: if the lookup fails, index unstamped
            # rather than skipping the repo.
            try:
                head = client.get_commit(project, ref)
                sha = head.id if head else None
            except Exception:
                sha = None
            _log(f"{project}: indexing (ref={ref}, sha={(sha or '?')[:8]})...")
            gp, sp = build_index(project, date=today, ref=ref, sha=sha)
            _log(f"{project}: done -> {gp.name}, {sp.name}")
        except Exception as e:
            _log(f"{project}: FAILED — {type(e).__name__}: {str(e)[:120]}")
            failed.append(project)

    if failed:
        _log(f"Re-index complete with {len(failed)} failure(s): {failed}")
    else:
        _log(f"Re-index complete — all {len(REPOS)} repos updated successfully")


if __name__ == "__main__":
    reindex_all()
