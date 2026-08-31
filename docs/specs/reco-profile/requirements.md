# Requirements — Reconciliation RCA Profile

## Summary
A reconciliation-specialized run path for the RCA agent ("reco profile"): a reco-tuned
prompt, the reconciliation module summary, reco-scoped code search, and a reco tool set —
selectable per run, and **measured** against the general agent on the `rca-recon` eval set.
Layered on the existing `run_agent` (not a separate agent).

## Motivation
The 14-ticket baseline showed the general agent is strong on `code` causes but
systematically mis-classifies non-code causes (`user_side` / `infrastructure` / `data`) and
often can't name the specific field/record. Reconciliation is a high-value module with
many such cases. Specializing for it — and only shipping changes the eval proves help —
is the fastest path to higher accuracy on real tickets.

## User stories & acceptance criteria

### Story 1 — A reconciliation-tuned investigation
*As a QA engineer, I want reco tickets investigated by a reco-specialized agent so that the
RCA is more accurate than the general agent.*

- **AC1.1** — WHEN a run is invoked with the reco profile, THE SYSTEM SHALL use a system
  prompt that includes the reconciliation data model (buckets, reco types 2A-PR/2B-PR/
  8A-PR/IMS-PR, the GST-side vs purchase-side record model, `match_status`).
- **AC1.2** — WHEN the reco profile is active, THE SYSTEM SHALL make the reconciliation
  module summary (`reconcile.md`) available to the agent at the start of the investigation.
- **AC1.3** — WHEN the reco profile is active, THE SYSTEM SHALL instruct the agent to weigh
  `data` / `user_side` / `config` / `infrastructure` causes before defaulting to `code`,
  and to name the specific field/record involved.
- **AC1.4** — WHEN the reco profile is active, THE SYSTEM SHALL scope code search to the
  reconciliation module rather than the whole repo.

### Story 2 — Measured, non-regressive improvement
*As a developer, I want to measure the reco profile against the general agent so that I only
keep changes that demonstrably improve accuracy.*

- **AC2.1** — WHEN the eval is run with `--profile reco` on `rca-recon`, THE SYSTEM SHALL
  record a Phoenix experiment scored by the same evaluators as the baseline.
- **AC2.2** — The reco profile SHALL be adopted only if it **raises `rca_matches`** on
  `rca-recon` versus the `baseline` experiment, **without regressing** previously-correct
  tickets. (Objective acceptance = the eval delta.)

### Story 3 — Zero impact on production
*As an operator, I want the live RCA path untouched so that this work can't break prod.*

- **AC3.1** — WHEN no profile is specified (the default), THE SYSTEM SHALL behave exactly as
  today — same prompt, tools, and search scope.
- **AC3.2** — THE SYSTEM SHALL reuse the existing `run_agent` loop, verdict schema, and
  tracing (no forked agent, no duplicated logic).

## Constraints / non-functional
- **Additive only:** `profile=None` everywhere except the reco eval; the webapp path unchanged.
- **PII:** no new customer-PII exposure (the reco summary + prompt are PII-free).
- **Reuse:** no second agent process; the eval harness runs it unchanged.

## Out of scope (deferred)
- A dedicated "reco-records inspector" tool (pull an invoice's GST-side + purchase-side
  records). Deferred to a follow-up spec; build only if the eval shows the field-naming gap
  persists after the prompt/summary/scope changes.
- A separate reconciliation agent process or service.
- Auto-selecting the profile by ticket (manual `--profile reco` for now).

## Open questions (for senior review)
- Which reco collections/fields are the highest-signal to name in the prompt data model?
- Do reco tickets route to `gst-enterprise-service` (API/UI) vs `gst-prefect-app` (sync jobs)
  in a predictable enough way to also scope the *repo*, not just the module path?
