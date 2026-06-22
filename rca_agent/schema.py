"""The QA-facing verdict — structured, not an essay.

Mirrors the "Output for QA" section of the design: probable root cause, the
evidence chain (real artifacts), regression yes/no + introducing MR, a triage
call, a suggested next action, and a calibrated confidence level.

`Verdict.to_json_schema()` is the contract the agent is asked to emit, and the
deterministic pipeline produces the same shape so both paths are comparable on
the eval set.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class Confidence(str, Enum):
    HIGH = "high"        # full evidence chain confirmed against live code
    MEDIUM = "medium"    # chain mostly built; one link inferred
    LOW = "low"          # candidates only; needs a human


class Triage(str, Enum):
    REAL_BUG = "real_bug"
    CONFIG = "config"
    ENVIRONMENT = "environment"
    LIKELY_DUPLICATE = "likely_duplicate"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass
class EvidenceLink:
    """One verifiable artifact in the symptom -> code -> change chain."""
    kind: str          # "stack_frame" | "file_content" | "blame" | "commit" | "merge_request"
    ref: str           # file:line, sha, MR url/iid — something a human can open
    detail: str        # what it shows / why it matters, in plain language
    url: str = ""      # clickable GitLab link to the artifact (navigation)


@dataclass
class Verdict:
    ticket: str
    probable_root_cause: str
    # One-line TL;DR read first: what's broken, why, and the fix/MR if known.
    headline: str = ""
    # Jargon-free explanation for someone who does NOT know the codebase: what is
    # broken (user-visible), where it comes from, and why — in plain language.
    plain_summary: str = ""
    evidence_chain: list[EvidenceLink] = field(default_factory=list)
    is_regression: bool | None = None
    introducing_mr: str | None = None      # MR url or "!iid"
    triage: Triage = Triage.INSUFFICIENT_EVIDENCE
    confidence: Confidence = Confidence.LOW
    suggested_next_action: str = ""
    candidates: list[str] = field(default_factory=list)  # when evidence is thin
    # Transitive callers of the suspect symbol — what QA should retest.
    blast_radius: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["triage"] = self.triage.value
        d["confidence"] = self.confidence.value
        return d

    @staticmethod
    def to_json_schema() -> dict:
        """JSON Schema the agent is instructed to emit as its final answer."""
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "ticket", "headline", "probable_root_cause", "plain_summary",
                "evidence_chain", "is_regression", "triage", "confidence",
                "suggested_next_action",
            ],
            "properties": {
                "ticket": {"type": "string"},
                "headline": {"type": "string"},
                "probable_root_cause": {"type": "string"},
                "plain_summary": {"type": "string"},
                "evidence_chain": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["kind", "ref", "detail"],
                        "properties": {
                            "kind": {"type": "string", "enum": [
                                "stack_frame", "file_content", "blame",
                                "commit", "merge_request",
                            ]},
                            "ref": {"type": "string"},
                            "detail": {"type": "string"},
                            "url": {"type": "string"},
                        },
                    },
                },
                "is_regression": {"type": ["boolean", "null"]},
                "introducing_mr": {"type": ["string", "null"]},
                "triage": {"type": "string", "enum": [t.value for t in Triage]},
                "confidence": {"type": "string", "enum": [c.value for c in Confidence]},
                "suggested_next_action": {"type": "string"},
                "candidates": {"type": "array", "items": {"type": "string"}},
                "blast_radius": {"type": "array", "items": {"type": "string"}},
                "notes": {"type": "string"},
            },
        }


def _regression_str(v: Verdict) -> str:
    return ("yes" if v.is_regression else "no" if v.is_regression is not None else "unknown")


def _key_links(v: Verdict) -> list[EvidenceLink]:
    """The 1-2 load-bearing links: where the code is + what introduced it."""
    code = next((e for e in v.evidence_chain
                 if e.kind in ("file_content", "stack_frame", "blame")), None)
    mr = next((e for e in v.evidence_chain if e.kind == "merge_request"), None)
    return [e for e in (code, mr) if e is not None]


def render_verdict(v: Verdict, brief: bool = False) -> str:
    """Human-readable rendering. `brief` = headline + plain summary + key links
    only (the QA glance); full = the complete evidence trail."""
    status = (f"Confidence {v.confidence.value.upper()} · {v.triage.value} · "
              f"regression {_regression_str(v)}"
              + (f" ({v.introducing_mr})" if v.introducing_mr else ""))
    head = [f"=== RCA: {v.ticket} ==="]
    if v.headline:
        head.append(v.headline)
    head.append(status)

    if brief:
        lines = list(head)
        if v.plain_summary:
            lines += ["", f"In plain terms: {v.plain_summary}"]
        keys = _key_links(v)
        if keys:
            lines += ["", "Key links:"]
            for e in keys:
                lines.append(f"  - {e.ref}" + (f"  {e.url}" if e.url else ""))
        if len(v.evidence_chain) > len(keys):
            lines.append(f"  (+ {len(v.evidence_chain) - len(keys)} more — see full trail)")
        if v.suggested_next_action:
            lines += ["", f"Next: {v.suggested_next_action}"]
        return "\n".join(lines)

    # Full
    lines = list(head)
    if v.plain_summary:
        lines += ["", "In plain terms:", f"  {v.plain_summary}"]
    lines += ["", "Probable root cause:", f"  {v.probable_root_cause}",
              "", "Where it comes from (follow the trail — click to open):"]
    if v.evidence_chain:
        for i, e in enumerate(v.evidence_chain, 1):
            lines.append(f"  {i}. [{e.kind}] {e.ref}")
            lines.append(f"       {e.detail}")
            if e.url:
                lines.append(f"       open: {e.url}")
    else:
        lines.append("  (none — insufficient evidence)")
    if v.candidates:
        lines += ["", "Candidates (need a human):"] + [f"  - {c}" for c in v.candidates]
    if v.blast_radius:
        lines += ["", "Blast radius (retest these callers):"] + [f"  - {b}" for b in v.blast_radius]
    lines += ["", f"Suggested next action:\n  {v.suggested_next_action}"]
    if v.notes:
        lines += ["", f"Notes: {v.notes}"]
    return "\n".join(lines)


# --- Jira comment (ADF) — short summary visible, full trail collapsed ----------

def _adf_text(s: str, marks: list | None = None) -> dict:
    node = {"type": "text", "text": s or ""}
    if marks:
        node["marks"] = marks
    return node


def _adf_para(*nodes) -> dict:
    return {"type": "paragraph", "content": list(nodes)}


def verdict_to_adf(v: Verdict) -> dict:
    """Render a verdict as a Jira ADF comment: a short, scannable summary up top
    and the full evidence trail tucked inside a collapsible 'RCA details' block.
    QA reads 3 lines; anyone who wants proof expands it."""
    status = (f"Confidence {v.confidence.value.upper()} · {v.triage.value} · "
              f"regression {_regression_str(v)}")

    visible = [
        _adf_para(_adf_text(v.headline or v.probable_root_cause, [{"type": "strong"}])),
        _adf_para(_adf_text(v.plain_summary)),
        _adf_para(_adf_text(status, [{"type": "em"}])),
    ]

    # Collapsible details: the evidence trail with links, blast radius, next step.
    detail: list[dict] = []
    for i, e in enumerate(v.evidence_chain, 1):
        prefix = f"{i}. [{e.kind}] {e.detail} "
        nodes = [_adf_text(prefix)]
        if e.url:
            nodes.append(_adf_text(e.ref or "open", [{"type": "link", "attrs": {"href": e.url}}]))
        else:
            nodes.append(_adf_text(e.ref))
        detail.append(_adf_para(*nodes))
    if v.blast_radius:
        detail.append(_adf_para(_adf_text("Retest: " + "; ".join(v.blast_radius))))
    if v.suggested_next_action:
        detail.append(_adf_para(_adf_text("Next action: " + v.suggested_next_action)))
    detail.append(_adf_para(_adf_text("(Automated RCA — verify before acting.)",
                                      [{"type": "code"}])))

    expand = {"type": "expand", "attrs": {"title": "RCA details — evidence trail"},
              "content": detail}
    return {"type": "doc", "version": 1, "content": visible + [expand]}
