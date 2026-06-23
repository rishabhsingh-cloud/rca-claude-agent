"""Web search tool — Tavily backend.

Useful when code search fails: the error comes from a third-party library,
a known cloud provider issue, or a common Python/framework bug where a web
search short-circuits a long investigation.

Set TAVILY_API_KEY in .env to enable. Returns a clear "not configured" message
if absent so the agent knows to skip it rather than hallucinate.
"""

from __future__ import annotations

import os

_TAVILY_URL = "https://api.tavily.com/search"
_MAX_SNIPPET = 400


def web_search(query: str, max_results: int = 5) -> dict:
    """Search the web and return top results with snippets.

    Returns {"answer": str|None, "results": [{title, url, snippet}]}
    or {"error": "..."} when not configured / network failure.
    """
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return {"error": "Web search not configured — set TAVILY_API_KEY in .env to enable"}

    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed"}

    try:
        r = httpx.post(
            _TAVILY_URL,
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": max_results,
                "include_answer": True,
            },
            timeout=20.0,
        )
        r.raise_for_status()
        data = r.json()
        return {
            "answer": data.get("answer"),
            "results": [
                {
                    "title": h.get("title", ""),
                    "url": h.get("url", ""),
                    "snippet": (h.get("content") or "")[:_MAX_SNIPPET],
                }
                for h in data.get("results", [])
            ],
        }
    except Exception as e:
        return {"error": f"search failed: {type(e).__name__}: {str(e)[:200]}"}
