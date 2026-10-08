"""Token + cost meter for one agent run (RCA or fix), for the dashboard's Cost tab.

The SDK reports the authoritative totals on the final ResultMessage
(`total_cost_usd`, `usage`, `num_turns`, `duration_ms`). A run that never gets
there (soft time budget, max-turns, a hard-timeout cancel) still spent money, so
per-message usage from AssistantMessages is summed as it streams and priced from
a small table as a fallback (`cost_source='estimated'`).

The meter is mutated in place while messages stream, so a caller that passes it
in and then loses the run to a cancel still holds the partial usage.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

# USD per 1M tokens: (input, output, cache read, 5-min cache write). First-party
# API list prices; only used when the SDK's own total is missing. Matched by
# model-id prefix, so dated snapshot ids resolve too.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5":   (10.0, 50.0, 0.25, 12.5),
    "claude-opus-5-5":  (4.0, 20.0, 0.20, 5.0),
    "claude-opus":      (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5":  (2.0, 10.0, 0.20, 2.5),
    "claude-sonnet":    (3.0, 15.0, 0.30, 3.75),
    "claude-haiku":     (1.0, 5.0, 0.10, 1.25),
}


def _price(model: str) -> tuple[float, float, float, float] | None:
    # Longest prefix first, so "claude-opus-5-5" beats the generic "claude-opus".
    for prefix in sorted(PRICES, key=len, reverse=True):
        if (model or "").startswith(prefix):
            return PRICES[prefix]
    return None


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  cache_read_tokens: int, cache_write_tokens: int) -> float | None:
    p = _price(model)
    if p is None:
        return None
    return (input_tokens * p[0] + output_tokens * p[1]
            + cache_read_tokens * p[2] + cache_write_tokens * p[3]) / 1_000_000


def _get(obj, key: str):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class RunUsage:
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    cost_source: str = ""          # 'sdk' | 'estimated' | '' (nothing observed)
    num_turns: int = 0
    duration_ms: int = 0
    _seen: set = field(default_factory=set, repr=False)
    _final: bool = field(default=False, repr=False)
    _start: float = field(default_factory=time.monotonic, repr=False)

    def _set_tokens(self, usage) -> None:
        self.input_tokens = _int(_get(usage, "input_tokens"))
        self.output_tokens = _int(_get(usage, "output_tokens"))
        self.cache_read_tokens = _int(_get(usage, "cache_read_input_tokens"))
        self.cache_write_tokens = _int(_get(usage, "cache_creation_input_tokens"))

    def observe(self, message) -> None:
        """Feed every message from claude_agent_sdk.query(). Never raises."""
        try:
            self._observe(message)
        except Exception:  # noqa: BLE001 — metering must never break a run
            pass

    def _observe(self, message) -> None:
        name = type(message).__name__
        if name == "AssistantMessage":
            if self._final:
                return
            if _get(message, "model"):
                self.model = message.model
            usage = _get(message, "usage")
            if not usage:
                return
            # One API response can arrive as several AssistantMessages (one per
            # content block) carrying the same id and the same usage — count once.
            mid = _get(message, "message_id")
            if mid:
                if mid in self._seen:
                    return
                self._seen.add(mid)
            self.input_tokens += _int(_get(usage, "input_tokens"))
            self.output_tokens += _int(_get(usage, "output_tokens"))
            self.cache_read_tokens += _int(_get(usage, "cache_read_input_tokens"))
            self.cache_write_tokens += _int(_get(usage, "cache_creation_input_tokens"))
            self.num_turns += 1
        elif name == "ResultMessage":
            usage = _get(message, "usage")
            if usage:
                self._set_tokens(usage)
            model_usage = _get(message, "model_usage")
            if isinstance(model_usage, dict) and model_usage:
                # The model that did most of the output is "the" model of the run.
                self.model = max(model_usage, key=lambda m: _int(
                    _get(model_usage[m], "outputTokens")
                    or _get(model_usage[m], "output_tokens")))
            cost = _get(message, "total_cost_usd")
            if cost is not None:
                self.cost_usd = float(cost)
                self.cost_source = "sdk"
            self.num_turns = _int(_get(message, "num_turns")) or self.num_turns
            self.duration_ms = _int(_get(message, "duration_ms")) or self.duration_ms
            self._final = True

    def finish(self) -> "RunUsage":
        """Fill in what the run never reported (no ResultMessage): wall-clock
        duration and an estimated cost from the summed tokens."""
        if not self.duration_ms:
            self.duration_ms = int((time.monotonic() - self._start) * 1000)
        if self.cost_usd is None and (self.input_tokens or self.output_tokens):
            est = estimate_cost(self.model, self.input_tokens, self.output_tokens,
                                self.cache_read_tokens, self.cache_write_tokens)
            if est is not None:
                self.cost_usd = est
                self.cost_source = "estimated"
        return self

    @property
    def observed(self) -> bool:
        return bool(self.cost_source or self.input_tokens or self.output_tokens)

    def to_row(self) -> dict:
        return {k: v for k, v in asdict(self).items() if not k.startswith("_")}
