"""Infra / APM metrics tool — Grafana + Prometheus backend.

Exposes two operations to the agent:
  1. query_metrics(promql, hours_ago) — run raw PromQL against Prometheus via
     the Grafana datasource proxy. The agent decides what to query based on the
     bug type (error spikes, OOM, latency, pod restarts, etc.)
  2. get_service_errors(service, hours_ago) — convenience shortcut that runs
     three common error-rate queries for a named service so the agent doesn't
     need to know PromQL for the straightforward "did this service spike?" check.

Required env vars (set in .env):
  GRAFANA_URL          e.g. http://grafana.internal or http://10.200.x.x:3000
  GRAFANA_TOKEN        Grafana service-account token (Viewer role is enough)
  GRAFANA_PROM_UID     The datasource UID of the Prometheus datasource in Grafana
                       (find it: Grafana → Connections → Data Sources → click yours
                        → copy the UID from the URL or the page header)

All three must be set; otherwise every call returns "not configured".
"""

from __future__ import annotations

import os
import time
from typing import Any

_REQUIRED = ("GRAFANA_URL", "GRAFANA_TOKEN", "GRAFANA_PROM_UID")


def _cfg() -> tuple[str, str, str] | None:
    vals = [os.getenv(k, "").strip() for k in _REQUIRED]
    return tuple(vals) if all(vals) else None  # type: ignore[return-value]


def _not_configured() -> dict:
    return {
        "error": (
            "Metrics not configured — set GRAFANA_URL, GRAFANA_TOKEN, "
            "and GRAFANA_PROM_UID in .env to enable"
        )
    }


def _query_prometheus(promql: str, start: float, end: float,
                      step: str = "60s") -> dict[str, Any]:
    """Execute a PromQL range query via the Grafana datasource proxy."""
    cfg = _cfg()
    if not cfg:
        return _not_configured()
    grafana_url, token, prom_uid = cfg

    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed"}

    url = f"{grafana_url.rstrip('/')}/api/datasources/proxy/uid/{prom_uid}/api/v1/query_range"
    try:
        r = httpx.get(
            url,
            params={"query": promql, "start": start, "end": end, "step": step},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("data", {}).get("result", [])
        return {
            "promql": promql,
            "series": [
                {
                    "labels": s.get("metric", {}),
                    "values": [
                        {"ts": int(v[0]), "value": v[1]}
                        for v in (s.get("values") or [])
                    ],
                }
                for s in results
            ],
        }
    except Exception as e:
        return {"error": f"metrics query failed: {type(e).__name__}: {str(e)[:200]}"}


def query_metrics(promql: str, hours_ago: int = 1, step: str = "60s") -> dict:
    """Run a raw PromQL query over the last `hours_ago` hours.

    Use this when you know what metric to look for — error spikes, memory OOM,
    pod restarts, slow DB queries, etc.

    Common PromQL patterns:
      Error rate:    rate(http_requests_total{status=~"5..",job="<svc>"}[5m])
      Latency p99:   histogram_quantile(0.99, rate(http_duration_seconds_bucket{job="<svc>"}[5m]))
      Pod restarts:  kube_pod_container_status_restarts_total{namespace="<ns>"}
      Memory:        container_memory_usage_bytes{container="<svc>"}
    """
    now = time.time()
    return _query_prometheus(promql, start=now - hours_ago * 3600, end=now, step=step)


def get_service_errors(service: str, hours_ago: int = 2) -> dict:
    """Convenience: run three standard error-rate queries for a service name.

    Queries HTTP 5xx rate, total error count, and pod restart count.
    Returns all three results so the agent can see at a glance whether the
    service was misbehaving around the time of the bug.
    """
    if not _cfg():
        return _not_configured()

    now = time.time()
    start = now - hours_ago * 3600
    step = "60s"

    queries = {
        "http_5xx_rate": f'rate(http_requests_total{{status=~"5..",job="{service}"}}[5m])',
        "error_count":   f'increase(http_requests_total{{status=~"5..",job="{service}"}}[{hours_ago}h])',
        "pod_restarts":  f'increase(kube_pod_container_status_restarts_total{{container="{service}"}}[{hours_ago}h])',
    }

    return {
        metric_name: _query_prometheus(promql, start, now, step)
        for metric_name, promql in queries.items()
    }
