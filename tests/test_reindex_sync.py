"""Regression test for the re-indexer's git-sync (reindex._sync_repo).

Guards the single-branch-clone bug (fixed in c49aa10): the box clones track only
their dev branch, so a naive `fetch origin master` + `checkout master` failed
"pathspec did not match", silently left the clone on the dev branch, and
search_code_local kept reading QA while everything else read prod.

This builds a clone set up EXACTLY that way (single-branch, shallow, on a dev
branch, with no origin/master) and asserts the sync actually moves the working
tree onto the prod branch. Uses a local file:// remote so git honours
--single-branch. Skips cleanly if git isn't installed.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from rca_agent import reindex

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _run(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _make_single_branch_clone(tmp_path: Path) -> Path:
    """Bare remote with a dev branch 'qa-master' and prod branch 'master', then a
    SINGLE-BRANCH shallow clone of 'qa-master' — mirroring the box clones."""
    remote = tmp_path / "origin.git"
    _run(tmp_path, "init", "-q", "--bare", str(remote))
    remote_url = remote.as_uri()  # file:// so --single-branch is honoured

    seed = tmp_path / "seed"
    _run(tmp_path, "clone", "-q", remote_url, str(seed))
    _run(seed, "config", "user.email", "t@example.com")
    _run(seed, "config", "user.name", "t")
    _run(seed, "checkout", "-q", "-b", "qa-master")
    (seed / "app.py").write_text("QA\n")
    _run(seed, "add", ".")
    _run(seed, "commit", "-qm", "qa")
    _run(seed, "checkout", "-q", "-b", "master")
    (seed / "app.py").write_text("PROD\n")
    _run(seed, "add", ".")
    _run(seed, "commit", "-qm", "prod")
    _run(seed, "push", "-q", "origin", "qa-master", "master")

    clone = tmp_path / "clone"
    _run(tmp_path, "clone", "-q", "--depth", "1", "--single-branch",
         "--branch", "qa-master", remote_url, str(clone))
    return clone


def test_sync_repo_moves_single_branch_clone_to_prod(tmp_path):
    clone = _make_single_branch_clone(tmp_path)

    # Preconditions that made the old code fail: on the dev branch, no origin/master.
    assert (clone / "app.py").read_text() == "QA\n"
    assert _run(clone, "rev-parse", "--abbrev-ref", "HEAD") == "qa-master"
    absent = subprocess.run(["git", "rev-parse", "--verify", "-q", "origin/master"],
                            cwd=clone, capture_output=True, text=True)
    assert absent.returncode != 0, "origin/master must NOT exist in a single-branch clone"

    head = reindex._sync_repo(clone, "master")

    # The working tree and HEAD must now be prod.
    assert (clone / "app.py").read_text() == "PROD\n"
    assert _run(clone, "rev-parse", "--abbrev-ref", "HEAD") == "master"
    assert head and _run(clone, "rev-parse", "--short", "HEAD") == head


def test_sync_repo_raises_on_missing_branch(tmp_path):
    # A branch that doesn't exist on the remote must RAISE (so the caller logs
    # FAILED loudly) rather than silently no-op.
    clone = _make_single_branch_clone(tmp_path)
    with pytest.raises(Exception):
        reindex._sync_repo(clone, "no-such-branch")
