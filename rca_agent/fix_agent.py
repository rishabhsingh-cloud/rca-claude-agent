"""Fix Suggester (Phase 1, dry-run) — proposes a code fix for an RCA.

SEPARATE agent from the read-only RCA. Phase 1 writes NOTHING to GitLab.

TESTING MODE (Option A): the fix agent is now a small READ-ONLY EXPLORER. The RCA
only pins the crash site, but the real fix often lives in other files/functions, so
the agent is given the read-only search/fetch tools (search_code via the GitLab API,
fetch_file_lines, find_callers, ...) and lets it gather ALL the code its fix needs
before proposing edits. It then returns `before -> after` snippets (possibly across
several files); we apply them in memory by EXACT match, syntax-check each patched
file, and return per-file diffs for a human to review.

Scope + safety:
  - Read-only exploration within ONE project (the service the RCA localized to).
  - Only edits files it actually READ this session; applies by exact string match
    (refuses snippets that don't match or aren't unique). Nothing is pushed.
  - Bigger, explored fixes are lower-confidence than a one-line guard — surfaced as
    a caveat. The branch-push / draft-MR step is Phase 1b (needs a write-scoped token).
"""

from __future__ import annotations

import difflib
import json
import os
from dataclasses import asdict, dataclass, field

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from .config import Settings
from .fix_tools import build_fix_server
from .gitlab_client import GitLabClient
from .verify import _parse_blob_url

_MAX_EDIT_FILES = 8    # cap on how many distinct files we fetch + apply to
# Exploration budget. Multi-file fixes (read the crash site, trace to the real fix
# across other files, confirm the schema) legitimately run 20+ turns; 18 was cutting
# real runs off mid-compose (error_max_turns -> no JSON -> "no fix"). Sonnet turns are
# fast, so a higher cap costs latency, not much money. Paired with the webapp's
# per-request timeout, which must be >= the wall-clock this allows.
_MAX_TURNS = 30

# The fix agent runs on a FASTER model than the RCA (Opus): code exploration + a
# minimal patch don't need Opus, and speed matters for an interactive button. The
# RCA stays on settings.model; only this agent uses RCA_FIX_MODEL. Override via env.
_DEFAULT_FIX_MODEL = "claude-sonnet-5"


def _fix_model() -> str:
    return os.getenv("RCA_FIX_MODEL") or _DEFAULT_FIX_MODEL

FIX_EXPLORE_SYSTEM = """You are a careful senior engineer proposing a code fix for a
root-cause analysis (RCA). You have READ-ONLY tools to explore code ACROSS REPOSITORIES:
  - mcp__fix__list_repos: discover repositories you can read (find the right repo by name)
  - mcp__fix__search_code: search ONE repo for a string / symbol (GitLab API)
  - mcp__fix__fetch_file_lines: read exact lines of a file in a repo
  - mcp__fix__find_callers / mcp__fix__get_subgraph / mcp__fix__search_symbols: trace code
    (indexed repos only; elsewhere use search_code)
  - mcp__fix__read_app_data: read the app's STORED data (PII-masked) to see the REAL field
    names behind a data/parser bug — e.g. gst_ims_outward_invoices (imsactn, ntty, inv_typ),
    gstr1_exceptions, import docs. Use this when the fix depends on a field name / data shape
    you can't confirm from code alone, instead of guessing or declining.
A bug often SURFACES in one service but the real fix lives in ANOTHER repo. The RCA below
names the code it pinned (with its repo). Start there, then follow the tools to EVERY piece
of code your fix must change — in ANY repo. If the RCA names a service but you lack its exact
path, use list_repos to find it, then search_code inside it. READ code before editing it.

When ready, output ONLY a single JSON object as your FINAL message (no prose, no code fences):
  {"fixable": true, "rationale": "<= 3 sentences", "edits": [
     {"project": "<repo path, e.g. mastersindia/gst-prefect-app>",
      "file": "<repo-relative path>",
      "before": "<verbatim current code>", "after": "<replacement>"} ]}
or:
  {"fixable": false, "reason": "<one sentence>"}

Rules for every edit:
  - `project` = the repo you read the file from (via search_code / fetch_file_lines),
  - `before` copied EXACTLY (whitespace included) from that file's CURRENT content, small but
    UNIQUE within the file (locatable by exact string match),
  - `file` = the repo-relative path.
Only propose edits to files you ACTUALLY READ this session (in whatever repo). A fix may span
several files and several repos. Do not decline just because the fix is large or crosses repos
— only mark fixable:true once you have seen ALL the code you need. Never invent code you cannot see.

BE EFFICIENT: start from the pinned code, read it, trace out only to the code your fix touches.
You have room for ~25 tool calls, but ALWAYS leave a turn to emit the JSON — the worst outcome is
exploring until cut off with no JSON. Once you've seen the code you need (or judge you cannot
locate a required piece), STOP and emit the final JSON: your edits, or
{"fixable": false, "reason": "<what code / which repo you could not locate>"}.
"""


@dataclass
class FileFix:
    file: str
    project: str = ""
    ref: str = ""
    diff: str = ""
    syntax_ok: bool | None = None
    syntax_note: str = ""
    edits: list = field(default_factory=list)   # {"before","after","applied","error"}


@dataclass
class FixSuggestion:
    fixable: bool
    rationale: str = ""
    files: list = field(default_factory=list)   # list[FileFix]
    reason: str = ""
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --- deterministic apply + check (no LLM here, so it's unit-testable) ----------

def _apply_one(raw: str, before: str, after: str) -> tuple[str, str]:
    if not before:
        return raw, "model returned an empty 'before' snippet"
    count = raw.count(before)
    if count == 0:
        return raw, "does not match the current code (it may have drifted) — not applied"
    if count > 1:
        return raw, "'before' snippet is not unique in the file — not applied"
    return raw.replace(before, after, 1), ""


def _check_file(orig: str, new: str, path: str) -> tuple[str, bool | None, str]:
    syntax_ok: bool | None = None
    syntax_note = "syntax check skipped (not a Python file)"
    if path.endswith(".py"):
        try:
            compile(new, path, "exec")
            syntax_ok, syntax_note = True, "patched file parses (py_compile OK)"
        except SyntaxError as e:
            syntax_ok, syntax_note = False, f"patched file has a syntax error: {str(e)[:120]}"
    diff = "".join(difflib.unified_diff(
        orig.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}"))
    return diff, syntax_ok, syntax_note


def _build_files(obj: dict, files_raw: dict) -> list[FileFix]:
    """Group the model's edits by (project, file), apply each by exact match,
    syntax-check. `files_raw` maps (project, path) -> (ref, raw_text) for files we
    could fetch."""
    by_key: dict[tuple, list] = {}
    for ed in obj.get("edits") or []:
        by_key.setdefault((ed.get("project", ""), ed.get("file", "")), []).append(ed)

    out: list[FileFix] = []
    for (project, path), eds in by_key.items():
        if (project, path) not in files_raw:
            out.append(FileFix(file=path, project=project, edits=[
                {"before": e.get("before", ""), "after": e.get("after", ""),
                 "applied": False,
                 "error": "could not fetch this file to apply/verify the change"}
                for e in eds]))
            continue
        ref, raw = files_raw[(project, path)]
        cur, results = raw, []
        for e in eds:
            new, err = _apply_one(cur, e.get("before", ""), e.get("after", ""))
            results.append({"before": e.get("before", ""), "after": e.get("after", ""),
                            "applied": not err, "error": err})
            if not err:
                cur = new
        if cur != raw:
            diff, syntax_ok, note = _check_file(raw, cur, path)
        else:
            diff, syntax_ok, note = "", None, "no snippet applied (all refused)"
        out.append(FileFix(file=path, project=project, ref=ref, diff=diff,
                           syntax_ok=syntax_ok, syntax_note=note, edits=results))
    return out


# --- locate the primary project/ref from the verdict ---------------------------

def _line_from(ref: str, url: str) -> int | None:
    for src in (url, ref):
        if not src:
            continue
        for marker in ("#L", ":"):
            if marker in src:
                tail = src.rsplit(marker, 1)[1]
                digits = "".join(c for c in tail if c.isdigit())
                if digits:
                    return int(digits)
    return None


def _candidate_projects(verdict: dict) -> list[str]:
    """Distinct repos the RCA's code evidence cites (in order). The fix agent starts
    from these but may explore any other repo via list_repos."""
    projs: list[str] = []
    for e in verdict.get("evidence_chain", []):
        if e.get("kind") in ("file_content", "stack_frame", "blame") and e.get("url"):
            parsed = _parse_blob_url(e["url"])
            if parsed and parsed[0] not in projs:
                projs.append(parsed[0])
    return projs


def _caveats(verdict: dict, explored: bool) -> list[str]:
    out = ["AI-suggested fix — review before applying. Nothing has been pushed."]
    if explored:
        out.append("This fix was assembled by exploring the repo and may span code the "
                   "RCA did not originally pin — treat it as a starting point, not a "
                   "trusted patch.")
    if (verdict.get("confidence") or "").lower() == "low":
        out.append("The RCA diagnosis is LOW confidence, so this fix is exploratory.")
    cats = verdict.get("cause_categories") or []
    if cats and not ({"code", "data", "infrastructure"} & set(cats)):
        out.append(f"RCA cause is {cats} (not our code) — any change here is likely a "
                   "robustness/UX improvement, not the actual bug fix.")
    return out


# --- LLM exploration loop ------------------------------------------------------

# The RCA already located the relevant code; these are the pinned kinds whose `ref`
# is a path:line-range the fix agent can read directly with fetch_file_lines instead
# of re-discovering it. Non-code kinds (merge_request, commit) are still useful
# context for WHAT changed, so we list them too but label them separately.
_CODE_EVIDENCE_KINDS = ("file_content", "stack_frame", "blame")


def _evidence_block(verdict: dict) -> str:
    """Format the RCA's evidence_chain so the fix agent reads the code the RCA already
    pinned (path:line-range + why it matters) instead of spending its tool budget
    re-finding it. Returns '' if there is nothing usable."""
    code, context = [], []
    for e in verdict.get("evidence_chain") or []:
        ref, detail = (e.get("ref") or "").strip(), (e.get("detail") or "").strip()
        if not ref:
            continue
        proj = ""
        if e.get("url"):
            parsed = _parse_blob_url(e["url"])
            if parsed:
                proj = parsed[0]
        prefix = f"[{proj}] " if proj else ""
        line = f"- {prefix}{ref}" + (f" — {detail}" if detail else "")
        (code if e.get("kind") in _CODE_EVIDENCE_KINDS else context).append(line)
    if not code and not context:
        return ""
    out = ["\n# Code the RCA already pinned (READ THESE FIRST with fetch_file_lines — "
           "each is a path:line-range on this branch; do not re-discover them):"]
    out.extend(code or ["- (none — the RCA pinned no direct code location)"])
    if context:
        out.append("\n# Related change context (what introduced/surrounds the bug — "
                    "not necessarily where you edit):")
        out.extend(context)
    return "\n".join(out) + "\n"


def _parse_json(text: str) -> dict | None:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        raw = raw[4:] if raw.startswith("json") else raw
        raw = raw.strip().rstrip("`").strip()
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e <= s:
        return None
    try:
        d = json.loads(raw[s:e + 1])
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        return None


async def _explore_and_generate(verdict: dict, candidates: list[str],
                                client: GitLabClient, settings: Settings) -> str:
    server, tool_names = build_fix_server(client)
    options = ClaudeAgentOptions(
        system_prompt=FIX_EXPLORE_SYSTEM,
        mcp_servers={"fix": server},
        allowed_tools=tool_names,       # the fix agent's own read-only server
        model=_fix_model(),             # faster model than the RCA (Opus)
        permission_mode="default",
        max_turns=_MAX_TURNS,
    )
    repos_line = ", ".join(candidates) if candidates else "(none pinned — use list_repos)"
    prompt = (
        f"# RCA\n"
        f"Root cause: {verdict.get('probable_root_cause', '')}\n"
        f"Headline: {verdict.get('headline', '')}\n"
        f"Suggested action: {verdict.get('suggested_next_action', '')}\n\n"
        f"Repos the RCA implicates (START here; the tools handle each repo's branch for "
        f"you — just pass project/path/query, never a ref). You MAY explore other repos "
        f"via list_repos if the fix lives elsewhere: {repos_line}\n"
        f"{_evidence_block(verdict)}\n"
        f"Read the pinned code first, then trace out to any OTHER code the fix must change "
        f"(in any repo). Emit the final JSON — tag each edit with the `project` it belongs to."
    )
    final = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    final = block.text
        elif isinstance(message, ResultMessage):
            subtype = getattr(message, "subtype", None)
            result = getattr(message, "result", None) or ""
            # Running out of exploration steps is NOT a crash — return whatever we
            # have (usually no JSON) so the caller reports a clean "no fix" instead
            # of a 500. Other non-success subtypes (overloaded, out of credits) are
            # real infra failures and should surface.
            if subtype == "error_max_turns":
                return final
            if subtype not in (None, "success") or getattr(message, "is_error", False):
                raise RuntimeError(f"fix agent run failed ({subtype}): {result[:160]}")
            if result:
                final = result
    return final


async def suggest_fix(verdict: dict, client: GitLabClient, settings: Settings) -> FixSuggestion:
    candidates = _candidate_projects(verdict)
    caveats = _caveats(verdict, explored=True)
    if not candidates:
        return FixSuggestion(False, reason="The RCA did not pin any code location "
                             "(file:line), so there is no repo/starting point for a fix.",
                             caveats=_caveats(verdict, explored=False))

    text = await _explore_and_generate(verdict, candidates, client, settings)
    obj = _parse_json(text)
    if not obj:
        return FixSuggestion(False, reason="the fix agent did not return a valid fix "
                             "(it may have run out of exploration steps)", caveats=caveats)
    if not obj.get("fixable"):
        return FixSuggestion(False, reason=obj.get("reason", "no fix proposed"),
                             caveats=caveats)

    # An edit may target any repo. Default a missing `project` to the first candidate
    # (covers single-repo fixes where the model omits it), then fetch each distinct
    # (project, file) at that repo's own default branch and apply by exact match.
    edits = obj.get("edits") or []
    for e in edits:
        if not e.get("project") and candidates:
            e["project"] = candidates[0]

    keys: list[tuple[str, str]] = []
    for e in edits:
        k = (e.get("project", ""), e.get("file", ""))
        if k[1] and k not in keys:
            keys.append(k)

    files_raw: dict[tuple[str, str], tuple[str, str]] = {}
    for proj, path in keys[:_MAX_EDIT_FILES]:
        if not proj:
            continue
        try:
            ref = client.default_ref(proj)
            files_raw[(proj, path)] = (ref, client.get_file(proj, ref, path))
        except Exception:  # noqa: BLE001 — left out -> reported as unfetchable
            pass

    files = _build_files(obj, files_raw)
    if not files:
        return FixSuggestion(False, reason="the fix agent marked it fixable but proposed "
                             "no edits", caveats=caveats)
    return FixSuggestion(True, rationale=obj.get("rationale", ""), files=files, caveats=caveats)
