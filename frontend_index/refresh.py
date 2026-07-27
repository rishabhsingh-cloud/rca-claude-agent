"""Self-refresh the frontend index.

Keeps a PERSISTENT shallow clone of frontend-apps, fetch+resets it to the latest
`enterprise-ts`, and rebuilds the index ONLY if the frontend HEAD changed (SHA gate) —
so this is cheap to run often (daily cron, or as a step in the RCA re-indexer). Stamps
the built-from commit into index_meta.json so the agent can see how fresh the index is.

  .venv/bin/python frontend_index/refresh.py            # rebuild only if changed
  .venv/bin/python frontend_index/refresh.py --force     # always rebuild

Token comes from .env (GITLAB_TOKEN) via a GIT_ASKPASS helper — never printed.
"""
from __future__ import annotations
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

REPO = Path("frontend_index/frontend-apps")
META = Path("frontend_index/index_meta.json")
BRANCH = "enterprise-ts"
REMOTE = "http://oauth2@10.200.11.32/mastersindia/frontend-apps.git"
EXTRACTORS = ["frontend_index/extract_frontend.py", "frontend_index/extract_reco.py"]


def _token() -> str:
    for line in Path(".env").read_text().splitlines():
        if line.startswith("GITLAB_TOKEN="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("GITLAB_TOKEN not found in .env")


def _askpass_env():
    f = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
    f.write('#!/bin/sh\necho "$GLT"\n')
    f.close()
    os.chmod(f.name, 0o700)
    return dict(os.environ, GIT_ASKPASS=f.name, GLT=_token(), GIT_TERMINAL_PROMPT="0"), f.name


def _git(*args, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], text=True, capture_output=True, env=env)


def ensure_latest() -> None:
    env, ask = _askpass_env()
    try:
        if not (REPO / ".git").exists():
            print("cloning frontend-apps (shallow)...")
            r = _git("clone", "--depth", "1", "--single-branch", "--branch", BRANCH,
                     REMOTE, str(REPO), env=env)
        else:
            _git("-C", str(REPO), "fetch", "--depth", "1", "origin", BRANCH, env=env)
            r = _git("-C", str(REPO), "reset", "--hard", f"origin/{BRANCH}", env=env)
        if r.returncode != 0:
            raise SystemExit(f"git failed: {r.stderr.strip()[:200]}")
    finally:
        os.unlink(ask)


def sha_and_date() -> tuple[str, str]:
    s = _git("-C", str(REPO), "rev-parse", "--short", "HEAD").stdout.strip()
    d = _git("-C", str(REPO), "log", "-1", "--format=%cI").stdout.strip()
    return s, d


def _publish() -> None:
    """Copy the built index into the agent's published-index store (index_dir(), honoring
    RCA_INDEX_DIR) — the same place repo_summaries/ and architecture.md live — so the
    deployed agent's find_screen reads it. Kept in sync even on a no-rebuild run."""
    from rca_agent.config import index_dir  # local import: rca_agent must be importable
    dest = index_dir()
    dest.mkdir(parents=True, exist_ok=True)
    published = []
    for name in ("frontend_areas.jsonl", "reco_screens.jsonl", "index_meta.json"):
        src = Path("frontend_index") / name
        if src.exists():
            shutil.copy2(src, dest / name)
            published.append(name)
    print(f"published {published} -> {dest}")


def main() -> None:
    force = "--force" in sys.argv
    ensure_latest()
    sha, commit_date = sha_and_date()
    last = json.loads(META.read_text())["built_from_sha"] if META.exists() else None
    if sha == last and not force:
        print(f"frontend index up to date (sha {sha}, commit {commit_date}); no rebuild.")
    else:
        print(f"frontend changed ({last} -> {sha}); rebuilding index...")
        for ex in EXTRACTORS:
            subprocess.run([sys.executable, ex], check=True)
        META.write_text(json.dumps(
            {"built_from_sha": sha, "frontend_commit_date": commit_date, "branch": BRANCH},
            indent=2) + "\n")
        print(f"index rebuilt; stamped sha {sha} -> {META}")
    _publish()  # always sync the published copy the agent reads


if __name__ == "__main__":
    main()
