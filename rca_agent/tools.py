"""Custom in-process MCP tools for the RCA agent (Claude Agent SDK).

These wrap the SAME deterministic primitives the offline pipeline uses, so the
agent and the eval baseline share one source of truth. Tools return compact JSON
text so the model gets structured, machine-checkable evidence.

Tool naming once registered under server "rca": mcp__rca__<tool_name>.

Routing/graph tools generate HYPOTHESES (a fetch list); the GitLab tools return
GROUND TRUTH. The system prompt instructs the agent to confirm every conclusion
against the GitLab tools before stating it.

Importing this module requires `claude-agent-sdk`. The offline pipeline
(investigation.py) does not import it, so the mock path needs no SDK install.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from claude_agent_sdk import create_sdk_mcp_server, tool

from .app_db import query_postgres as _query_postgres
from .app_mongo import query_mongo as _query_mongo
from .error_lookup import find_error_reason as _find_error_reason
from .architecture import search_architecture as _search_arch
from .gitlab_client import GitLabClient, GitLabError
from .graph_store import load_repo_graph
from .newrelic import find_request_ids as _find_request_ids
from .newrelic import query_nr as _query_nr
from .newrelic import search_nr_errors as _search_nr_errors
from .newrelic import search_nr_logs as _search_nr_logs
from .newrelic import trace_request as _trace_request
from .routing import read_summary as _read_summary
from .routing import route_repo as _route_repo
from .local_search import search_code_local as _search_code_local
from .search import web_search as _web_search
from .stack_trace import parse_stack_trace as _parse_trace


def _ok(payload) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": json.dumps({"error": msg})}],
            "is_error": True}


def build_rca_server(client: GitLabClient):
    """Create the SDK MCP server exposing the RCA tools, bound to `client`.

    Returns (server, tool_names) where tool_names are the fully-qualified
    `mcp__rca__*` identifiers to put in ClaudeAgentOptions.allowed_tools.
    """

    @tool("parse_stack_trace",
          "Extract file:line frames from ticket text. The LAST frame is the "
          "crash site. If frames are returned, this is the deterministic "
          "trace-first path — use it before any search.",
          {"ticket_text": str})
    async def parse_stack_trace(args):
        frames = _parse_trace(args["ticket_text"])
        return _ok({"frames": [asdict(f) for f in frames],
                    "crash_site": asdict(frames[-1]) if frames else None})

    @tool("route_repo",
          "Rank candidate GitLab repos for a ticket using local repo summaries. "
          "A HYPOTHESIS only — confirm by fetching the suspect file from the repo.",
          {"ticket_text": str})
    async def route_repo(args):
        cands = _route_repo(args["ticket_text"])
        return _ok({"candidates": [asdict(c) for c in cands]})

    @tool("fetch_file_lines",
          "Fetch live GitLab file content around a line (the verification pass). "
          "GROUND TRUTH. project e.g. 'acme/billing-service'.",
          {"project": str, "path": str, "start": int, "end": int, "ref": str})
    async def fetch_file_lines(args):
        ref = args.get("ref") or client.default_ref(args["project"])
        try:
            sl = client.get_file_lines(args["project"], ref, args["path"],
                                       int(args["start"]), int(args["end"]))
        except GitLabError as e:
            return _err(str(e))
        return _ok({"path": sl.path, "ref": sl.ref, "start_line": sl.start_line,
                    "end_line": sl.end_line, "content": sl.render()})

    @tool("git_blame",
          "Blame a single line: returns the commit that last changed it. This "
          "answers the regression question. GROUND TRUTH.",
          {"project": str, "path": str, "line": int, "ref": str})
    async def git_blame(args):
        ref = args.get("ref") or client.default_ref(args["project"])
        c = client.blame_line(args["project"], ref, args["path"], int(args["line"]))
        if not c:
            return _ok({"commit": None, "note": "no blame data for that line"})
        return _ok({"commit": asdict(c)})

    @tool("get_commit",
          "Fetch commit metadata (author, date, message) by SHA. GROUND TRUTH.",
          {"project": str, "sha": str})
    async def get_commit(args):
        c = client.get_commit(args["project"], args["sha"])
        return _ok({"commit": asdict(c) if c else None})

    @tool("merge_requests_for_commit",
          "List merge requests that introduced a commit — the introducing MR for "
          "a regression. GROUND TRUTH.",
          {"project": str, "sha": str})
    async def merge_requests_for_commit(args):
        mrs = client.merge_requests_for_commit(args["project"], args["sha"])
        return _ok({"merge_requests": [asdict(m) for m in mrs]})

    @tool("find_callers",
          "Code graph: who CALLS this symbol (direct callers). Use to trace where "
          "bad input came from and to size what to retest. The graph is a map — "
          "confirm any specific edge with fetch_file_lines before asserting it.",
          {"project": str, "symbol": str})
    async def find_callers(args):
        g = load_repo_graph(args["project"])
        callers = g.callers_of(args["symbol"])
        return _ok({"symbol": args["symbol"], "graph_sha": g.sha,
                    "callers": [{**n.__dict__, "provenance": p} for n, p in callers]})

    @tool("find_dependents",
          "Code graph: which files IMPORT this module/file. Coarser than callers; "
          "use for cross-file/module blast radius.",
          {"project": str, "module": str})
    async def find_dependents(args):
        g = load_repo_graph(args["project"])
        return _ok({"module": args["module"], "graph_sha": g.sha,
                    "dependents": g.dependents_of(args["module"])})

    @tool("get_subgraph",
          "Code graph: callers + callees around a symbol to a given depth. Returns "
          "nodes and edges for tracing. depth defaults to 1.",
          {"project": str, "symbol": str, "depth": int})
    async def get_subgraph(args):
        g = load_repo_graph(args["project"])
        return _ok({**g.get_subgraph(args["symbol"], int(args.get("depth") or 1)),
                    "graph_sha": g.sha})

    @tool("graph_has_edge",
          "Code graph guardrail: does a caller->callee edge actually exist? Call "
          "this before asserting 'A calls B'. If false, do NOT claim the edge.",
          {"project": str, "caller": str, "callee": str})
    async def graph_has_edge(args):
        g = load_repo_graph(args["project"])
        return _ok({"caller": args["caller"], "callee": args["callee"], "graph_sha": g.sha,
                    "has_edge": g.has_edge(args["caller"], args["callee"])})

    @tool("search_symbols",
          "NO-TRACE localizer: find functions/classes in the indexed graph whose "
          "name or path matches keywords from the ticket (the failing action, a "
          "UI label, an error phrase). Use when there is no stack trace. Returns "
          "candidate file:line to fetch + blame.",
          {"project": str, "query": str})
    async def search_symbols(args):
        g = load_repo_graph(args["project"])
        terms = [t for t in args["query"].lower().split() if len(t) > 2]
        hits = g.search_symbols(terms)
        return _ok({"graph_sha": g.sha,
                    "symbols": [{"qualname": n.qualname, "file": n.file, "line": n.line}
                                for n in hits]})

    @tool("search_code",
          "NO-TRACE localizer: live GitLab code search over the repo for a literal "
          "string (an error message, a UI label, a function name). GROUND TRUTH — "
          "pinpoints the file:line where that text lives.",
          {"project": str, "query": str, "ref": str})
    async def search_code(args):
        ref = args.get("ref") or client.default_ref(args["project"])
        try:
            hits = client.search_blobs(args["project"], ref, args["query"])
        except GitLabError as e:
            return _err(str(e))
        return _ok({"matches": hits})

    @tool("search_code_local",
          "NO-TRACE localizer: ripgrep search over locally cloned repos — faster "
          "and more reliable than GitLab API search. Use this BEFORE search_code. "
          "Falls back to 'not available' if repos are not cloned (then use search_code).",
          {"project": str, "query": str})
    async def search_code_local(args):
        result = _search_code_local(args["project"], args["query"])
        if "error" in result:
            return _err(result["error"])
        return _ok(result)

    @tool("search_architecture",
          "Cross-service platform map: search the architecture reference for where "
          "a symptom originates — service boundaries, Kafka topics, HTTP proxies, "
          "end-to-end flows, status codes. Use FIRST for cross-service / no-trace "
          "tickets to pick the right service+boundary before diving into one repo. "
          "Refs are hypotheses — confirm against live code.",
          {"query": str})
    async def search_architecture(args):
        return _ok({"sections": _search_arch(args["query"])})

    @tool("get_repo_summary",
          "Read the human-authored RCA summary for a repo: purpose, modules, known "
          "weak points, and a symptom->cause table. Use to orient within a repo "
          "before searching code.",
          {"project": str})
    async def get_repo_summary(args):
        text = _read_summary(args["project"])
        return _ok({"summary": text} if text
                   else {"summary": None, "note": "no summary indexed for this repo"})

    @tool("web_search",
          "Search the web for an error message, library bug, or known issue. Use "
          "when code search fails — e.g. the error comes from a third-party library, "
          "a cloud provider outage, or a common framework bug. Returns top results "
          "with snippets. Requires TAVILY_API_KEY — returns 'not configured' if absent.",
          {"query": str, "max_results": int})
    async def web_search(args):
        return _ok(_web_search(args["query"], int(args.get("max_results") or 5)))

    @tool("search_nr_errors",
          "Search New Relic APM for recent TransactionErrors for a service. "
          "Returns production stack traces, error class, message, and count. "
          "Use when the ticket describes an exception or crash — this gives you "
          "the actual production trace without needing server access.",
          {"service": str, "hours_ago": int})
    async def search_nr_errors(args):
        return _ok(_search_nr_errors(args["service"], int(args.get("hours_ago") or 2)))

    @tool("search_nr_logs",
          "Search New Relic logs for a keyword or error string. "
          "Returns matching log lines with timestamp, service, and severity. "
          "Use to find what was happening on the server at the time of the bug.",
          {"query": str, "hours_ago": int})
    async def search_nr_logs(args):
        return _ok(_search_nr_logs(args["query"], int(args.get("hours_ago") or 2)))

    @tool("query_nr",
          "Run a raw NRQL query against New Relic for custom lookups: slow "
          "transactions, error rates, deployments, throughput drops, custom events. "
          "Use when the shortcuts above don't cover what you need.",
          {"nrql": str})
    async def query_nr(args):
        return _ok(_query_nr(args["nrql"]))

    @tool("find_request_ids",
          "PRODUCTION LOG LOOKUP (New Relic). From a business identifier you "
          "already have — a GSTIN, e-way-bill / IRN number, document number, an "
          "endpoint path, or an error string — find the Mi-Requestid(s) of the "
          "matching request(s). Use this FIRST when a ticket has no request id, "
          "then pass a returned id to trace_request. hours_ago defaults to 24.",
          {"query": str, "hours_ago": int})
    async def find_request_ids(args):
        return _ok(_find_request_ids(args["query"], int(args.get("hours_ago") or 24)))

    @tool("trace_request",
          "PRODUCTION REQUEST TRACE (New Relic). Follow ONE Mi-Requestid across "
          "services (Router -> eDoc -> external/NIC) in time order. Returns each "
          "hop: service, method, path/url, HTTP status_code, timing, NIC source — "
          "GROUND TRUTH for WHERE a request actually failed. Log lines are "
          "PII-masked. Get the id from find_request_ids first.",
          {"request_id": str, "hours_ago": int})
    async def trace_request(args):
        return _ok(_trace_request(args["request_id"], int(args.get("hours_ago") or 24)))

    @tool("query_users_db",
          "Read-only Postgres lookup against the users/organizations data — the "
          "GROUND TRUTH for account/identity questions (is this org registered? is "
          "a user's plan/config field null or wrong?). Use to CONFIRM a data-cause "
          "hypothesis instead of guessing. SELECT only; results are PII-masked "
          "(customer names/emails/GSTINs come back redacted — you see field "
          "presence/null-ness, not raw values), so reason about SHAPE not content. "
          "Returns 'not configured' if no DB creds — then skip it.",
          {"sql": str})
    async def query_users_db(args):
        return _ok(_query_postgres(args["sql"]))

    @tool("find_error_reason",
          "EXACT-ERROR LOOKUP (read-only, GROUND TRUTH). Fetch the REAL reason "
          "behind a generic failure message ('Due to Wrong Input Data', 'contact "
          "support', 'something went wrong'). The true error is NOT in New Relic or "
          "Postgres — it is in domain-specific Mongo stores, and this ONE tool checks "
          "them all: GSTR-1 import exceptions, GST-portal/NIC fetch errors, "
          "reconciliation errors, and rejected import rows. Use it for ANY import / "
          "portal-fetch / reconciliation failure ticket BEFORE concluding the real "
          "error is unavailable. Pass whatever you have: `gstin`, `ret_period` (e.g. "
          "'062026'), `reference_id` (import job id) — at least one of gstin/"
          "reference_id is required. Using a GSTIN as a lookup arg is allowed (it will "
          "not appear in your verdict). Results are PII-masked; error_case 'gov' means "
          "the government/NIC portal failed (third_party, not our bug).",
          {"gstin": str, "ret_period": str, "reference_id": str})
    async def find_error_reason(args):
        return _ok(_find_error_reason(
            args.get("gstin", ""), args.get("ret_period", ""),
            args.get("reference_id", "")))

    @tool("query_app_data",
          "Read-only MongoDB lookup against the business documents (GSTR-3B "
          "returns, e-invoice/e-way-bill docs, import jobs, autofill snapshots) — "
          "the GROUND TRUTH for confirming a data-cause hypothesis (is this "
          "snapshot doc missing? is a field on this return null?). Pass `collection` "
          "and a JSON `filter` (e.g. {\"gstin\":\"...\",\"ret_period\":\"052026\"}); "
          "optional `projection` (JSON) and `limit`. find-only — no writes, and "
          "$where/$out/$merge are rejected. Results are PII-masked (raw customer "
          "values redacted to <present>/<empty>), so reason about presence/shape. "
          "Returns 'not configured' if no DB creds — then skip it.",
          {"collection": str, "filter": str, "projection": str, "limit": int})
    async def query_app_data(args):
        return _ok(_query_mongo(
            args["collection"],
            args.get("filter") or "{}",
            args.get("projection") or "",
            int(args.get("limit") or 20),
        ))

    tools = [parse_stack_trace, route_repo, fetch_file_lines, git_blame,
             get_commit, merge_requests_for_commit,
             find_callers, find_dependents, get_subgraph, graph_has_edge,
             search_symbols, search_code, search_code_local, search_architecture, get_repo_summary,
             web_search,
             search_nr_errors, search_nr_logs, query_nr,
             find_request_ids, trace_request, query_users_db,
             find_error_reason, query_app_data]
    server = create_sdk_mcp_server(name="rca", version="0.1.0", tools=tools)
    tool_names = [
        "mcp__rca__parse_stack_trace",
        "mcp__rca__route_repo",
        "mcp__rca__fetch_file_lines",
        "mcp__rca__git_blame",
        "mcp__rca__get_commit",
        "mcp__rca__merge_requests_for_commit",
        "mcp__rca__find_callers",
        "mcp__rca__find_dependents",
        "mcp__rca__get_subgraph",
        "mcp__rca__graph_has_edge",
        "mcp__rca__search_symbols",
        "mcp__rca__search_code",
        "mcp__rca__search_code_local",
        "mcp__rca__search_architecture",
        "mcp__rca__get_repo_summary",
        "mcp__rca__web_search",
        "mcp__rca__search_nr_errors",
        "mcp__rca__search_nr_logs",
        "mcp__rca__query_nr",
        "mcp__rca__find_request_ids",
        "mcp__rca__trace_request",
        "mcp__rca__query_users_db",
        "mcp__rca__find_error_reason",
        "mcp__rca__query_app_data",
    ]
    return server, tool_names
