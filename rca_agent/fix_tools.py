"""Read-only code-exploration tools for the FIX agent — a SEPARATE MCP server.

Deliberately independent from the RCA server (tools.py): the fix agent gets its OWN
`mcp__fix__*` server so the two agents share no server instance and can evolve without
affecting each other. This server is READ-ONLY — it exposes GitLab code search + file
reads + the code graph + repo discovery, plus ONE PII-masked data lookup (to see the
stored field structure behind a data/parser bug). No New Relic, no writes.

MULTI-REPO: the real fix often lives in a DIFFERENT service than where the bug surfaced,
so the agent may explore ANY repo the token can read. It does NOT choose a git ref — each
tool resolves (and the client caches) that repo's own default branch, so reads and the
later exact-match apply line up, and a repo whose default branch isn't `main` still works.

The thin @tool wrappers call the same underlying primitives (GitLab client, code graph)
as the RCA tools, so there is no duplicated logic — only a separate, minimal surface.
"""

from __future__ import annotations

import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from .app_mongo import query_mongo as _query_mongo
from .gitlab_client import GitLabClient, GitLabError
from .graph_store import load_repo_graph
from .local_search import search_code_local as _search_code_local
from .routing import read_summary as _read_summary


def _ok(payload) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": json.dumps({"error": msg})}],
            "is_error": True}


def build_fix_server(client: GitLabClient):
    """Create the SDK MCP server exposing the FIX agent's read-only code tools, bound
    to `client`. Returns (server, tool_names) with `mcp__fix__*` identifiers.

    The agent may work across repos; it never passes a git ref — each tool resolves the
    target repo's default branch via `client.default_ref` (cached on the client), so the
    branch is consistent between exploration and the caller's apply step."""

    @tool("list_repos",
          "Discover the repositories the token can read (path + name + description). "
          "Use this to FIND the repo that contains the code your fix needs when the RCA "
          "names a service you don't have the exact path for. Optional `search` filters "
          "by name/path (e.g. 'prefect').",
          {"search": str})
    async def list_repos(args):
        try:
            repos = client.list_projects(args.get("search", ""))
        except GitLabError as e:
            return _err(str(e))
        return _ok({"repos": repos})

    @tool("search_code",
          "Search ONE repo for a literal string / symbol via the GitLab API — the "
          "primary way to FIND code the fix must change (a function name, an error "
          "string, a column access). Returns file:line matches. Searches that repo's "
          "default branch automatically — you do not choose a ref.",
          {"project": str, "query": str})
    async def search_code(args):
        try:
            ref = client.default_ref(args["project"])
            hits = client.search_blobs(args["project"], ref, args["query"])
        except GitLabError as e:
            return _err(str(e))
        return _ok({"project": args["project"], "matches": hits})

    @tool("search_code_local",
          "ripgrep search over locally cloned repos (faster) — falls back to 'not "
          "available' if repos aren't cloned; then use search_code.",
          {"project": str, "query": str})
    async def search_code_local(args):
        result = _search_code_local(args["project"], args["query"])
        return _err(result["error"]) if "error" in result else _ok(result)

    @tool("fetch_file_lines",
          "Read exact lines of a file in a repo (GROUND TRUTH). You MUST read code with "
          "this before proposing an edit to it — copy your `before` snippet verbatim "
          "from here. project e.g. 'mastersindia/gst-prefect-app'. Reads that repo's "
          "default branch automatically — you do not choose a ref.",
          {"project": str, "path": str, "start": int, "end": int})
    async def fetch_file_lines(args):
        try:
            ref = client.default_ref(args["project"])
            sl = client.get_file_lines(args["project"], ref, args["path"],
                                       int(args["start"]), int(args["end"]))
        except GitLabError as e:
            return _err(str(e))
        return _ok({"project": args["project"], "path": sl.path, "ref": sl.ref,
                    "start_line": sl.start_line, "end_line": sl.end_line,
                    "content": sl.render()})

    @tool("find_callers",
          "Code graph (indexed repos only): who CALLS this symbol. Confirm any specific "
          "edge with fetch_file_lines. Returns empty for un-indexed repos — use "
          "search_code there.",
          {"project": str, "symbol": str})
    async def find_callers(args):
        try:
            g = load_repo_graph(args["project"])
        except Exception as e:  # noqa: BLE001 — un-indexed repo
            return _ok({"symbol": args["symbol"], "callers": [],
                        "note": f"no code graph for {args['project']} — use search_code"})
        callers = g.callers_of(args["symbol"])
        return _ok({"symbol": args["symbol"],
                    "callers": [{**n.__dict__, "provenance": p} for n, p in callers]})

    @tool("get_subgraph",
          "Code graph (indexed repos only): callers + callees around a symbol to a depth "
          "(default 1).",
          {"project": str, "symbol": str, "depth": int})
    async def get_subgraph(args):
        try:
            g = load_repo_graph(args["project"])
        except Exception:  # noqa: BLE001
            return _ok({"note": f"no code graph for {args['project']} — use search_code"})
        return _ok(g.get_subgraph(args["symbol"], int(args.get("depth") or 1)))

    @tool("search_symbols",
          "Find functions/classes in an indexed repo's graph whose name/path matches "
          "keywords. Returns candidate file:line to read (indexed repos only).",
          {"project": str, "query": str})
    async def search_symbols(args):
        try:
            g = load_repo_graph(args["project"])
        except Exception:  # noqa: BLE001
            return _ok({"symbols": [],
                        "note": f"no code graph for {args['project']} — use search_code"})
        terms = [t for t in args["query"].lower().split() if len(t) > 2]
        hits = g.search_symbols(terms)
        return _ok({"symbols": [{"qualname": n.qualname, "file": n.file, "line": n.line}
                                for n in hits]})

    @tool("get_repo_summary",
          "Read a repo's RCA summary (purpose, modules, weak points) to orient before "
          "searching code.",
          {"project": str})
    async def get_repo_summary(args):
        text = _read_summary(args["project"])
        return _ok({"summary": text} if text
                   else {"summary": None, "note": "no summary indexed for this repo"})

    @tool("read_app_data",
          "Read-only, PII-MASKED lookup against the app's MongoDB business data — to inspect "
          "the STORED STRUCTURE behind a data/parser bug (the REAL field names the code must "
          "match). E.g. the government field names in `gst_ims_outward_invoices` (imsactn, "
          "ntty, inv_typ, rtn_typ...), `gstr1_exceptions`, or an import document. Customer "
          "VALUES come back masked (<present>/<empty>) but FIELD NAMES + shape are preserved — "
          "exactly what you need to pin a wrong-field-name / parse bug. Pass `collection` and a "
          "JSON `filter` (e.g. {\"gstin\":\"...\",\"rtnprd\":\"042025\"}); optional `projection` "
          "(JSON) and `limit`. find-only ($where/$out/$merge rejected); time-capped.",
          {"collection": str, "filter": str, "projection": str, "limit": int})
    async def read_app_data(args):
        return _ok(_query_mongo(
            args["collection"], args.get("filter") or "{}",
            args.get("projection") or "", int(args.get("limit") or 20)))

    tools = [list_repos, search_code, search_code_local, fetch_file_lines,
             find_callers, get_subgraph, search_symbols, get_repo_summary,
             read_app_data]
    server = create_sdk_mcp_server(name="fix", version="0.1.0", tools=tools)
    tool_names = [
        "mcp__fix__list_repos",
        "mcp__fix__search_code",
        "mcp__fix__search_code_local",
        "mcp__fix__fetch_file_lines",
        "mcp__fix__find_callers",
        "mcp__fix__get_subgraph",
        "mcp__fix__search_symbols",
        "mcp__fix__get_repo_summary",
        "mcp__fix__read_app_data",
    ]
    return server, tool_names
