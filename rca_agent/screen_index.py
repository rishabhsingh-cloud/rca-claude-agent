"""find_screen — map a UI screenshot's visible text (plus the ticket description) to the
frontend screen/area it belongs to, and return that area's module(s), API endpoints, and
backend service. This is the bridge from an image ticket to backend code.

Reads the PUBLISHED frontend index (`frontend_areas.jsonl`, built by
frontend_index/refresh.py). Read-only, local, and PII-free — the index is code-derived
(UI labels, route paths, API paths), no customer data.

Matching is deliberately partial-tolerant: token overlap between the query text and each
area's fingerprint (visible labels) + module/route tokens, phrase/substring hits weighted
higher. So a low-res screenshot that only yields a few readable tokens — or the ticket's
own wording — still routes to the right area. Returns the top-K candidates.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import ROOT, index_dir

_AREAS = "frontend_areas.jsonl"
_META = "index_meta.json"


def _index_path() -> Path | None:
    """Published location first (index_dir(), honoring RCA_INDEX_DIR), then a dev fallback
    to the repo-root frontend_index/ where the extractor writes during development."""
    for base in (index_dir(), ROOT / "frontend_index"):
        p = base / _AREAS
        if p.exists():
            return p
    return None


def _index_sha() -> str | None:
    for base in (index_dir(), ROOT / "frontend_index"):
        m = base / _META
        if m.exists():
            try:
                return json.loads(m.read_text(encoding="utf-8")).get("built_from_sha")
            except Exception:  # noqa: BLE001
                return None
    return None


_TOK = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> set[str]:
    return set(_TOK.findall(s.lower()))


def find_screen(text: str, top_k: int = 3) -> dict:
    """Return the frontend areas whose visible-text fingerprint best matches `text`
    (the screenshot's readable text + the ticket description), each with its module(s),
    route prefix, API endpoints, and backend."""
    if not text or not text.strip():
        return {"error": "find_screen: empty text — pass the screenshot's visible text "
                         "plus the ticket title/description"}
    path = _index_path()
    if path is None:
        return {"error": "frontend index not built — no frontend_areas.jsonl "
                         "(run frontend_index/refresh.py)"}

    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    q = _tokens(text)
    low = text.lower()

    scored = []
    for r in rows:
        terms = (r.get("fingerprint_sample") or []) + (r.get("modules") or []) + (r.get("route_prefixes") or [])
        area_tokens: set[str] = set()
        phrase_hits: set[str] = set()
        for term in terms:
            ts = str(term)
            area_tokens |= _tokens(ts)
            tl = ts.lower().strip().lstrip("/")
            if len(tl) >= 6 and tl in low:           # distinctive phrase/substring hit
                phrase_hits.add(ts)
        # coverage: DISTINCT query tokens this area matches (so a token shared by many of an
        # area's terms — e.g. "gstr" across gstr-1/2/3 — counts once, not once per term).
        covered = q & area_tokens
        score = len(covered) + 2 * len(phrase_hits)
        if score:
            matched = sorted(phrase_hits)[:6] + sorted(covered)[:8]
            scored.append((score, r, matched))

    scored.sort(key=lambda x: x[0], reverse=True)
    matches = [{
        "area": r["area"],
        "modules": r.get("modules", []),
        "route_prefixes": r.get("route_prefixes", []),
        "backends": r.get("backends", []),
        "api_calls": r.get("api_calls", []),
        "matched": m,
        "score": s,
    } for s, r, m in scored[:top_k]]

    return {
        "index_sha": _index_sha(),
        "matches": matches,
        "note": ("" if matches else
                 "no screen matched — broaden the visible text, or investigate from the "
                 "ticket description alone"),
    }
