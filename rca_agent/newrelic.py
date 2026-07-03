"""New Relic integration — query errors, logs, and transactions via NerdGraph.

Tools exposed to the agent:
  1. search_nr_errors(service, hours_ago)  — recent TransactionErrors for a service
  2. search_nr_logs(query, hours_ago)      — full-text log search
  3. query_nr(nrql, hours_ago)             — raw NRQL for custom queries
  4. find_request_ids(query, hours_ago)    — business id -> Mi-Requestid(s)
  5. trace_request(request_id, hours_ago)  — follow one request across services

Required env vars:
  NEWRELIC_API_KEY     User API key (NRAK-...)
  NEWRELIC_ACCOUNT_ID  Numeric account ID (e.g. 2965049)
"""

from __future__ import annotations

import os
import re

from .app_db import _mask_value  # reuse the DB tools' PII masking on log lines

_GRAPHQL_URL = "https://api.newrelic.com/graphql"
_MAX_RESULTS = 20

# The join key our services stamp on every log line, e.g.
# "... | Mi-Requestid: cdb9b56b-7699-11f1-8be9-67946b4b04b4 | ..."
_MI_REQ_RE = re.compile(r"Mi-Requestid:\s*([0-9a-fA-F][0-9a-fA-F-]{14,})")


def _like_safe(s: str) -> str:
    """Strip characters that would break out of an NRQL LIKE '%...%' literal."""
    return (s or "").replace("'", "").replace("%", "").replace("\\", "").strip()


def _cfg() -> tuple[str, str] | None:
    key = os.getenv("NEWRELIC_API_KEY", "").strip()
    account = os.getenv("NEWRELIC_ACCOUNT_ID", "").strip()
    return (key, account) if key and account else None


def _not_configured() -> dict:
    return {"error": "New Relic not configured — set NEWRELIC_API_KEY and NEWRELIC_ACCOUNT_ID in .env"}


def _run_nrql(nrql: str) -> dict:
    cfg = _cfg()
    if not cfg:
        return _not_configured()
    key, account = cfg

    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed"}

    escaped = nrql.replace("\\", "\\\\").replace('"', '\\"')
    query = """
    {
      actor {
        account(id: %s) {
          nrql(query: "%s") {
            results
          }
        }
      }
    }
    """ % (account, escaped)

    try:
        r = httpx.post(
            _GRAPHQL_URL,
            json={"query": query},
            headers={"API-Key": key, "Content-Type": "application/json"},
            timeout=20.0,
        )
        r.raise_for_status()
        data = r.json()
        errors = data.get("errors")
        if errors:
            return {"error": str(errors[0].get("message", errors))}
        results = (data.get("data", {})
                       .get("actor", {})
                       .get("account", {})
                       .get("nrql", {})
                       .get("results", []))
        return {"nrql": nrql, "results": results}
    except Exception as e:
        return {"error": f"New Relic query failed: {type(e).__name__}: {str(e)[:200]}"}


def search_nr_errors(service: str, hours_ago: int = 2) -> dict:
    """Fetch recent TransactionErrors for a service.

    Returns error message, stack trace, count, and transaction name.
    Use when the ticket suggests an exception was thrown — this gives you
    the actual production stack trace from New Relic APM.
    """
    nrql = (
        f"SELECT appName, transactionName, `error.class`, `error.message`, "
        f"`request.uri`, `request.method`, `response.status`, host, duration "
        f"FROM TransactionError "
        f"WHERE appName LIKE '%{service}%' "
        f"SINCE {hours_ago} hours ago "
        f"LIMIT {_MAX_RESULTS}"
    )
    return _run_nrql(nrql)


def search_nr_logs(query: str, hours_ago: int = 2) -> dict:
    """Search New Relic logs for a text pattern.

    Use when you have an error message or keyword from the ticket and want
    to find matching log lines from production. Returns log message, timestamp,
    service name, and severity.
    """
    safe = query.replace("'", "\\'")
    nrql = (
        f"SELECT timestamp, message, service.name, level, hostname "
        f"FROM Log "
        f"WHERE message LIKE '%{safe}%' "
        f"SINCE {hours_ago} hours ago "
        f"LIMIT {_MAX_RESULTS} "
        f"ORDER BY timestamp DESC"
    )
    return _run_nrql(nrql)


def query_nr(nrql: str) -> dict:
    """Run a raw NRQL query against New Relic.

    Use for custom lookups: slow transactions, throughput drops, deployment
    markers, custom events, or anything not covered by the shortcuts above.

    Common NRQL patterns:
      Slow transactions: SELECT average(duration) FROM Transaction WHERE appName='svc' SINCE 1 hour ago FACET name
      Deployments:       SELECT * FROM Deployment SINCE 1 day ago LIMIT 10
      Error rate:        SELECT percentage(count(*), WHERE error IS true) FROM Transaction WHERE appName='svc' SINCE 1 hour ago
      Custom events:     SELECT * FROM <EventType> SINCE 1 hour ago LIMIT 20
    """
    return _run_nrql(nrql)


def find_request_ids(query: str, hours_ago: int = 24) -> dict:
    """From a business identifier you already have — a GSTIN, e-way-bill / IRN
    number, document number, an endpoint path, or an error string — find the
    Mi-Requestid(s) of the matching production request(s).

    Use this FIRST when a ticket has no request id: search by what you have, get
    back the request id(s), then pass one to trace_request.
    """
    safe = _like_safe(query)
    if not safe:
        return {"error": "empty query"}
    nrql = (f"SELECT message FROM Log WHERE message LIKE '%{safe}%' "
            f"SINCE {int(hours_ago)} hours ago LIMIT 100")
    res = _run_nrql(nrql)
    if "error" in res:
        return res
    msgs = [(r.get("message") or "") for r in res.get("results", [])]
    rids: list[str] = []
    for m in msgs:
        for rid in _MI_REQ_RE.findall(m):
            if rid not in rids:
                rids.append(rid)
    return {"query": query, "matched_lines": len(msgs), "request_ids": rids,
            "note": ("no request ids found — widen hours_ago, or the identifier "
                     "may not appear in logs") if not rids else ""}


def trace_request(request_id: str, hours_ago: int = 24) -> dict:
    """Follow ONE Mi-Requestid across services (Router -> eDoc -> external/NIC),
    in time order. Returns each hop's log line (service, marker, method, path/url,
    HTTP status_code, timing) so you can see exactly where the request failed.

    Log lines are PII-masked (GSTINs / e-way numbers / etc. redacted).
    """
    safe = _like_safe(request_id)
    if not safe:
        return {"error": "empty request_id"}
    nrql = (f"SELECT entity.name, level, message, timestamp FROM Log "
            f"WHERE message LIKE '%{safe}%' "
            f"SINCE {int(hours_ago)} hours ago ORDER BY timestamp ASC LIMIT 100")
    res = _run_nrql(nrql)
    if "error" in res:
        return res
    trace = []
    for r in res.get("results", []):
        msg = _mask_value(r.get("message") or "")
        trace.append({"service": r.get("entity.name"), "level": r.get("level"),
                      "log": msg[:600]})
    return {"request_id": request_id, "hops": len(trace), "trace": trace,
            "note": "no log lines for that request id (check hours_ago)" if not trace else ""}
