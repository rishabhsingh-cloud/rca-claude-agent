"""Deterministic stack-trace / file:line extraction.

This is step 1 of the accuracy hierarchy: if the ticket carries a trace, it is
ground truth — we fetch exactly those files + lines and bypass search entirely.
No model is involved here; it is pure regex over well-known trace formats.

Frames are normalized to a canonical OUTERMOST -> INNERMOST order regardless of
the source language, so `crash_site()` is always the last frame. The crash site
is rarely the root cause itself — it is the deterministic starting point for
blame + graph tracing back to the introducing change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Frame:
    path: str           # e.g. "billing/invoice.py"
    line: int           # 1-based line number
    symbol: str | None  # function/method name if the format provides it
    raw: str            # the original matched line, for the evidence chain

    def ref(self) -> str:
        return f"{self.path}:{self.line}"


# --- Per-language line patterns -------------------------------------------------
# Python:  File "billing/invoice.py", line 87, in compute_total
_PY = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)(?:, in (?P<symbol>\S+))?')
# Java:    at com.acme.Billing.computeTotal(Billing.java:87)
_JAVA = re.compile(r'at\s+(?P<symbol>[\w$.]+)\((?P<file>[\w$]+\.java):(?P<line>\d+)\)')
# JS/TS V8: at computeTotal (/srv/app/billing/invoice.js:87:12)
_JS = re.compile(r'at\s+(?:(?P<symbol>[\w$.<>]+)\s+\()?(?P<path>[^()\s:]+):(?P<line>\d+)(?::\d+)?\)?')
# Generic fallback:  path/to/file.ext:87
_GENERIC = re.compile(r'(?P<path>[\w./\-]+\.[A-Za-z0-9]+):(?P<line>\d+)')

# Extensions trusted as real source paths for the generic fallback (avoids
# matching "v1.2:30", URLs, etc.).
_SOURCE_EXT = (
    ".py", ".java", ".js", ".ts", ".tsx", ".jsx", ".go", ".rb", ".php",
    ".cs", ".cpp", ".c", ".kt", ".scala", ".rs",
)


def _dedupe(frames: list[Frame]) -> list[Frame]:
    seen: set[tuple[str, int]] = set()
    out: list[Frame] = []
    for f in frames:
        key = (f.path, f.line)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def parse_stack_trace(text: str) -> list[Frame]:
    """Extract file:line frames from arbitrary ticket text.

    Dispatches on the first matching language so frame ordering is consistent.
    Returns [] when no trace is present — the caller must then fall back to the
    weaker summaries/graph + search path.
    """
    if not text:
        return []

    # Python — already outermost-first (crash site last). Keep order.
    py = [
        Frame(m.group("path").replace("\\", "/"), int(m.group("line")),
              m.group("symbol"), m.group(0).strip())
        for m in _PY.finditer(text)
    ]
    if py:
        return _dedupe(py)

    # Java — innermost (crash) first. Reverse to canonical order.
    java = [
        Frame(m.group("file"), int(m.group("line")),
              m.group("symbol"), m.group(0).strip())
        for m in _JAVA.finditer(text)
    ]
    if java:
        return _dedupe(list(reversed(java)))

    # JS/TS V8 — innermost first. Reverse to canonical order.
    js = [
        Frame(m.group("path").replace("\\", "/"), int(m.group("line")),
              m.group("symbol"), m.group(0).strip())
        for m in _JS.finditer(text)
        if m.group("path") and m.group("path").endswith(_SOURCE_EXT)
    ]
    if js:
        return _dedupe(list(reversed(js)))

    # Generic fallback — ONLY when the text actually looks like a trace/log dump.
    # Otherwise prose mentions like "dashboard_data.py:36" in a comment or a prior
    # RCA note would masquerade as a stack trace and wrongly trigger trace-first.
    if not re.search(r"traceback|most recent call last|stack ?trace|^\s*at\s",
                     text, re.IGNORECASE | re.MULTILINE):
        return []
    generic = [
        Frame(m.group("path").replace("\\", "/"), int(m.group("line")), None, m.group(0).strip())
        for m in _GENERIC.finditer(text)
        if m.group("path").endswith(_SOURCE_EXT)
    ]
    return _dedupe(generic)


def crash_site(frames: list[Frame]) -> Frame | None:
    """The deepest frame — where the exception was raised (always last after
    normalization)."""
    return frames[-1] if frames else None
