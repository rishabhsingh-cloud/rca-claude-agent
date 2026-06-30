"""Post-hoc verification of agent verdict claims against real artifacts.

After the agent emits a verdict we check its concrete claims:
  - File paths (file_content / stack_frame evidence) → exist in a cloned repo
  - Commit SHAs (commit / blame evidence with a URL)  → resolve via GitLab API
  - MR links (merge_request evidence / introducing_mr) → resolve via GitLab API

Checks that can't be performed (no REPOS_DIR env var, no URL to parse from) are
silently skipped and do NOT count against the verdict.  If >50% of checkable
claims fail, confidence is automatically downgraded one step before posting.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .gitlab_client import GitLabClient
from .schema import Confidence, Verdict


@dataclass
class CheckResult:
    passed: bool
    reason: str


@dataclass
class VerificationResult:
    checks: dict[str, CheckResult] = field(default_factory=dict)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.checks.values() if r.passed)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def score(self) -> float:
        return self.passed / self.total if self.total else 1.0

    def downgraded_confidence(self, confidence: Confidence) -> Confidence:
        """Downgrade one step if more than half of verifiable checks failed."""
        if self.total == 0 or self.score >= 0.5:
            return confidence
        order = [Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW]
        idx = next((i for i, c in enumerate(order) if c == confidence), 2)
        return order[min(idx + 1, 2)]

    def as_note(self) -> str:
        if not self.total:
            return ""
        lines = [f"[auto-verify] {self.passed}/{self.total} claims checked out"]
        for ref, r in self.checks.items():
            icon = "+" if r.passed else "x"
            lines.append(f"  {icon} {ref}: {r.reason}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _repos_dir() -> Path | None:
    d = os.environ.get("REPOS_DIR", "")
    p = Path(d) if d else None
    return p if (p and p.exists()) else None


def _file_exists_in_repos(path: str, repos_dir: Path) -> bool:
    for repo in repos_dir.iterdir():
        if repo.is_dir() and (repo / path).exists():
            return True
    return False


def _parse_commit_url(url: str) -> tuple[str, str] | None:
    """Return (project_path, sha) from a GitLab commit URL, or None."""
    m = re.search(r'/([^/]+/[^/]+)/-/commit/([0-9a-f]{7,40})', url)
    return (m.group(1), m.group(2)) if m else None


def _parse_mr_url(url: str) -> tuple[str, int] | None:
    """Return (project_path, mr_iid) from a GitLab MR URL, or None."""
    m = re.search(r'/([^/]+/[^/]+)/-/merge_requests/(\d+)', url)
    return (m.group(1), int(m.group(2))) if m else None


def _strip_line(ref: str) -> str:
    """'path/to/file.py:42' -> 'path/to/file.py'"""
    return ref.rsplit(":", 1)[0] if re.search(r':\d+$', ref) else ref


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def verify_verdict(verdict: Verdict, client: GitLabClient) -> VerificationResult:
    """Check the verdict's concrete claims; return a VerificationResult."""
    result = VerificationResult()
    repos_dir = _repos_dir()
    seen_mr_iids: set[int] = set()

    for ev in verdict.evidence_chain:
        if ev.kind in ("file_content", "stack_frame"):
            path = _strip_line(ev.ref)
            if repos_dir:
                exists = _file_exists_in_repos(path, repos_dir)
                result.checks[path] = CheckResult(
                    passed=exists,
                    reason="found in cloned repos" if exists else "not found in any cloned repo",
                )

        elif ev.kind in ("commit", "blame"):
            if not ev.url:
                continue
            parsed = _parse_commit_url(ev.url)
            if not parsed:
                continue
            project, sha = parsed
            try:
                c = client.get_commit(project, sha)
                result.checks[sha[:8]] = CheckResult(
                    passed=c is not None,
                    reason=(f"commit found: {c.title[:60]}" if c
                            else "commit SHA not found in GitLab"),
                )
            except Exception as e:
                result.checks[sha[:8]] = CheckResult(passed=False, reason=str(e)[:80])

        elif ev.kind == "merge_request":
            if not ev.url:
                continue
            parsed = _parse_mr_url(ev.url)
            if not parsed:
                continue
            project, iid = parsed
            seen_mr_iids.add(iid)
            try:
                mr = client.get_merge_request(project, iid)
                result.checks[f"!{iid}"] = CheckResult(
                    passed=mr is not None,
                    reason=(f"MR found: {mr.title[:60]}" if mr
                            else "MR not found in GitLab"),
                )
            except Exception as e:
                result.checks[f"!{iid}"] = CheckResult(passed=False, reason=str(e)[:80])

    # Also verify introducing_mr if it's a full URL we haven't checked yet
    if verdict.introducing_mr and "/" in verdict.introducing_mr:
        parsed = _parse_mr_url(verdict.introducing_mr)
        if parsed:
            project, iid = parsed
            if iid not in seen_mr_iids:
                try:
                    mr = client.get_merge_request(project, iid)
                    result.checks[f"!{iid}(introducing)"] = CheckResult(
                        passed=mr is not None,
                        reason=(f"MR found: {mr.title[:60]}" if mr
                                else "MR not found in GitLab"),
                    )
                except Exception as e:
                    result.checks[f"!{iid}(introducing)"] = CheckResult(
                        passed=False, reason=str(e)[:80])

    return result
