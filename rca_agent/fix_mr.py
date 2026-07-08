"""Phase 1b — raise a DRAFT merge request from a reviewed fix suggestion.

This is the ONLY write step in the whole RCA/fix pipeline, and it is deliberately
narrow and gated:

  - It uses a SEPARATE bot token, `GITLAB_FIX_TOKEN`, which must have **Developer role
    only**: it can push a branch and open an MR, but CANNOT merge protected `main` — so a
    human always merges. Without that token this module is inert ("not configured").
  - It opens a **Draft** MR and never merges, never force-pushes, and never touches local
    repos — everything is via the GitLab API.
  - It is human-triggered (the reviewer clicks "Raise MR" after seeing the diff) and only
    commits edits that actually applied cleanly, re-checked against CURRENT file content.
  - One draft MR per repo the fix touches.
"""

from __future__ import annotations

import os
from urllib.parse import quote

from .config import get_settings
from .gitlab_client import GitLabClient


def _write_cfg() -> tuple[str, str] | None:
    token = os.getenv("GITLAB_FIX_TOKEN", "").strip()
    url = (get_settings().gitlab_url or "").rstrip("/")
    return (url, token) if token and url else None


def is_configured() -> bool:
    return _write_cfg() is not None


def _reapply(raw: str, edits: list) -> tuple[str, list[str]]:
    """Re-apply the reviewed before->after edits to CURRENT content by exact match.
    Returns (new_content, errors). Refuses (per edit) if the snippet no longer matches
    uniquely — so a file that drifted since review is skipped, never guessed."""
    cur, errs = raw, []
    for e in edits:
        before, after = e.get("before", ""), e.get("after", "")
        n = cur.count(before) if before else 0
        if n == 1:
            cur = cur.replace(before, after, 1)
        else:
            errs.append("a snippet no longer matches the current file "
                        f"({'missing' if n == 0 else 'ambiguous'}) — skipped")
    return cur, errs


def raise_mr(ticket_key: str, fix: dict, client: GitLabClient) -> dict:
    """Push a branch + open a DRAFT MR per repo for the applied edits in `fix`
    (a FixSuggestion dict). `client` (read-only) is used to fetch current content;
    writes go through the GITLAB_FIX_TOKEN bot. Returns {results: [...]} or {error}."""
    cfg = _write_cfg()
    if not cfg:
        return {"error": "write access not configured — set GITLAB_FIX_TOKEN (a "
                         "Developer-role GitLab bot token) in .env to enable raising MRs"}
    if not fix.get("fixable"):
        return {"error": "this suggestion is not fixable — nothing to raise"}
    base, token = cfg

    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed"}

    # Group the APPLIED edits by repo (skip files/snippets that didn't apply at review).
    by_project: dict[str, list] = {}
    applied_but_no_repo = False
    for f in fix.get("files", []):
        applied = [e for e in f.get("edits", []) if e.get("applied")]
        if not applied or not f.get("file"):
            continue
        if not f.get("project"):
            applied_but_no_repo = True   # e.g. a stale pre-multi-repo suggestion
            continue
        by_project.setdefault(f["project"], []).append((f["file"], applied))
    if not by_project:
        if applied_but_no_repo:
            return {"error": "this fix has no repository attached to its files — it's "
                             "likely a stale suggestion from before the multi-repo "
                             "change. Re-run the dev agent, then raise the MR."}
        return {"error": "no applied edits to commit"}

    branch = f"ai-fix/{ticket_key.lower()}"
    rationale = (fix.get("rationale") or "").strip()
    results = []
    with httpx.Client(base_url=base + "/api/v4",
                      headers={"PRIVATE-TOKEN": token}, timeout=30.0) as w:
        for project, files in by_project.items():
            pid = quote(project, safe="")
            default = client.default_ref(project)

            # Rebuild each file's new content from CURRENT source + the reviewed edits.
            actions, skipped = [], []
            for path, edits in files:
                try:
                    raw = client.get_file(project, default, path)
                except Exception as e:  # noqa: BLE001
                    skipped.append(f"{path}: could not fetch ({str(e)[:60]})")
                    continue
                new, errs = _reapply(raw, edits)
                if new == raw:
                    skipped.append(f"{path}: {errs[0] if errs else 'no net change'}")
                    continue
                actions.append({"action": "update", "file_path": path, "content": new})
            if not actions:
                results.append({"project": project,
                                "error": "; ".join(skipped) or "nothing to commit"})
                continue

            # 1) branch off the repo's default (ok if it already exists)
            rb = w.post(f"/projects/{pid}/repository/branches",
                        params={"branch": branch, "ref": default})
            if rb.status_code >= 400 and "already exists" not in rb.text.lower():
                results.append({"project": project,
                                "error": f"create branch failed: {rb.status_code} {rb.text[:120]}"})
                continue

            # 2) one commit with all the file updates
            rc = w.post(f"/projects/{pid}/repository/commits", json={
                "branch": branch,
                "commit_message": f"[AI fix] {ticket_key}: {(rationale[:80] or 'suggested fix')}",
                "actions": actions})
            if rc.status_code >= 400:
                results.append({"project": project,
                                "error": f"commit failed: {rc.status_code} {rc.text[:120]}"})
                continue

            # 3) open a DRAFT MR (never merges; Developer role can't merge protected main)
            file_links = "\n".join(
                f"- [{p}]({base}/{project}/-/blob/{branch}/{p})" for p, _ in files)
            desc = (f"**AI-suggested fix for {ticket_key}** — review before merging; do NOT "
                    f"merge without verifying.\n\n{rationale}\n\n"
                    f"**Files changed** (click to view on the fix branch):\n{file_links}\n\n"
                    f"_Generated by the Triage dev-agent (Phase 1b, draft). A human must "
                    f"mark ready and merge._")
            rm = w.post(f"/projects/{pid}/merge_requests", json={
                "source_branch": branch, "target_branch": default,
                "title": f"Draft: [AI fix] {ticket_key} — {(rationale[:60] or 'suggested fix')}",
                "description": desc, "remove_source_branch": True})
            if rm.status_code >= 400:
                results.append({"project": project,
                                "error": f"open MR failed: {rm.status_code} {rm.text[:120]}"})
                continue
            mr = rm.json()
            results.append({"project": project, "branch": branch,
                            "mr_url": mr.get("web_url"), "mr_iid": mr.get("iid"),
                            "skipped": skipped})

    return {"ticket": ticket_key, "results": results}
