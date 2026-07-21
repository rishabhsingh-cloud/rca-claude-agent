"""Agent profiles — module-specialized run configurations for `run_agent`.

A profile overrides the general RCA agent's prompt / summary / search-scope / tool
set for one module (reconciliation, etc.). `run_agent(profile=None)` is the
unchanged default path — profiles are purely additive.

Select one by name: `get_profile("reco")`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).parent


@dataclass(frozen=True)
class AgentProfile:
    """A module-specialized run config. All fields optional except `name`."""
    name: str
    system_addendum: str = ""              # appended to the base system prompt
    summary: str = ""                      # module summary, injected into the opening turn
    search_scope: str | None = None        # path prefix to restrict code search (Phase B)
    allowed_tools: list[str] | None = None  # None -> the default full RCA tool set


def _load(filename: str) -> str:
    p = _HERE / filename
    return p.read_text(encoding="utf-8") if p.exists() else ""


# The reco-specific system-prompt addendum: the reconciliation data model + the
# cause-calibration rules the eval baseline showed the general agent needs (it
# over-classifies non-code causes and doesn't name the specific field/record).
_RECO_ADDENDUM = """\
# Reconciliation module — specialized guidance
You are investigating a RECONCILIATION ticket. Reconciliation matches a taxpayer's
purchase/sales register against government portal data (GSTR-2A/2B/8A/IMS; GSTR-1 vs
Sales; E-Invoice/E-Way vs Sales). Reason in this module's terms.

## Data model
- Reco TYPES: 2A-PR, 2B-PR, 8A-PR, IMS-PR (purchase side); GSTR1-Sale, EInvoice-Sale,
  Eway-GSTR1 (sale side).
- Buckets: Matched, Mismatched, NI2A (not in 2A), INA (not in purchase), PM (probable
  match), plus ITC-reversed and combined variants.
- TWO-SIDED records: results are stored on a GST/portal side AND a purchase/sales ("PR")
  side, keyed by `match_status` (e.g. A=accepted, IVM=mismatch, M_PR, M_R, CMBD-PR). A
  very common bug class is the two sides going OUT OF SYNC — one side updated while the
  other keeps a stale status/period — which then breaks delink, reports, or re-reco.
- No Django models: reco data lives in MongoDB (e.g. reco_cmn_purchase_histories,
  reconcile_2b_histories with pr_data, reco_purchase2b_histories), accessed via raw
  PyMongo. Use `find_error_reason` / `query_app_data` to inspect the ACTUAL records.

## Cause calibration (reconciliation-specific — do this before concluding `code`)
Recon issues are frequently NOT a code logic bug. Weigh, in order:
- `data`: a stale or wrong STORED field on a reco record (a status, a return period, a
  flag) that a re-reco or a one-off DB update fixes. Many recon tickets are resolved by a
  DATA PATCH, not a code change. Name the EXACT field and record involved.
- `user_side`: the user took an action AFTER the reco ran (an IMS action, a re-upload), so
  the reco holds a pre-action snapshot; re-executing picks it up. Working-as-designed.
- `infrastructure`: reading while a reco job is still processing (intermediate state) or
  read-replica lag can make on-screen values differ from a report — not a code defect.
- `third_party`: the NIC/GST portal excluded/rejected a record (EWB Part-B not generated
  in time, cancelled invoice, etc.) — not our bug.
Only after ruling these out, treat it as `code`. Whenever you claim a field/status is
wrong, CONFIRM it by inspecting the actual reco records (both the GST side and the
purchase side) with query_app_data — do not reason in the abstract."""


RECO_PROFILE = AgentProfile(
    name="reco",
    system_addendum=_RECO_ADDENDUM,
    summary=_load("reco_summary.md"),
    search_scope="masters_india_saas/reconcile",
)


_PROFILES = {"reco": RECO_PROFILE}


def get_profile(name: str | None) -> AgentProfile | None:
    """Look up a profile by name; None (or an unknown name) -> no profile."""
    return _PROFILES.get(name) if name else None


# Anchor in the general prompt we insert the profile block *before* (a stable heading
# early in the workflow, after the intro/arch orientation). If the general prompt ever
# drops it, we fall back to prepending so the guidance is never lost.
_INSERT_ANCHOR = "# Fetching the ticket"


def build_profile_system_prompt(profile: AgentProfile, gitlab_url: str | None = None) -> str:
    """The profile's OWN system prompt, built by reusing the general prompt UNCHANGED
    (`prompts.build_system_prompt`) and inserting the profile's guidance into it. All
    composition happens here — `prompts.py` is never modified — so the reco prompt is a
    separate artifact that still inherits the shared RCA rules / tools / output schema.
    """
    from ..prompts import build_system_prompt  # local import: keeps this package standalone
    base = build_system_prompt(gitlab_url)
    block = (profile.system_addendum or "").strip()
    if not block:
        return base
    if _INSERT_ANCHOR in base:
        return base.replace(_INSERT_ANCHOR, f"{block}\n\n{_INSERT_ANCHOR}", 1)
    return f"{block}\n\n{base}"  # fallback: anchor gone -> prepend so guidance survives
