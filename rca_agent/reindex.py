"""Nightly re-indexer — rebuilds call graphs + repo summaries for all live repos.

Run manually:  python -m rca_agent.reindex
Runs on EC2 every 2 nights via systemd timer (rca-reindex.timer).

Branches are resolved automatically via GitLab default_ref() so the script
doesn't break when branches are renamed.
"""
from __future__ import annotations

import time
from datetime import date

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


def reindex_all() -> None:
    settings = get_settings()
    client = build_client(settings)
    today = date.today().isoformat()
    failed = []

    _log(f"Starting re-index for {len(REPOS)} repos")
    for project in REPOS:
        try:
            ref = client.default_ref(project)
            _log(f"{project}: indexing (ref={ref})...")
            gp, sp = build_index(project, date=today, ref=ref)
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
