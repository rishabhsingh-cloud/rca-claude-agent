# Tasks — Reconciliation RCA Profile

> **Status (2026-07-22): COMPLETE — profile accepted.** Phases A–D done; profile is live on
> the box (additive, `profile=None` unchanged). `reco-profile-v1` (16/16, scored) was compared
> to baseline and reviewed with the senior, who accepted it. Follow-up work (his suggestion):
> a dataset of UNRESOLVED reconciliation tickets to run the profile against for real triage —
> see `docs/specs/reco-unresolved/`.

> Ordered build list. Each task cites the AC / design element it implements. Check off as
> completed. Do not build outside this list; if a task reveals a gap, update requirements/design first.

## Phase A — Profile plumbing + prompt + summary  (minimal, measurable)
- [x] **A1. Create the `rca_agent/profiles/` package.** `__init__.py` with the `AgentProfile`
  frozen dataclass (`name`, `system_addendum`, `summary`, `search_scope`, `allowed_tools`) +
  a `get_profile(name)` registry. — *design: Core data structure; AC1.1–1.4*
- [x] **A2. Add `rca_agent/profiles/reco_summary.md`** = the curated reconciliation summary
  (copy of the good `reconcile.md`, PII-free). — *AC1.2*
- [x] **A3. Define `RECO_PROFILE`** in `__init__.py`: `system_addendum` = reco data model
  (buckets, reco types, GST-side vs purchase-side, `match_status`) + cause-calibration rules
  (weigh data/user_side/config/infra before code; name the exact field); `summary` = loaded
  `reco_summary.md`; `search_scope="masters_india_saas/reconcile"`. — *AC1.1, AC1.3*
- [x] **A4. `pyproject.toml`:** add `rca_agent.profiles` to `packages`; include `*.md` as
  package data. — *packaging*
- [x] **A5. Reco prompt — `prompts.py` NOT touched.** Add `build_profile_system_prompt(profile, url)`
  in `profiles/` that reuses the *unmodified* `build_system_prompt` output and inserts the reco
  block before the workflow section. — *AC1.1, AC1.3, AC3.1*
- [x] **A6. `agent.py`:** `run_agent(..., profile: AgentProfile | None = None)` — build prompt
  with `profile.system_addendum`; when `profile.summary`, prepend it to the opening user turn;
  use `profile.allowed_tools or <default>`. — *AC1.1–1.3, AC3.1*
- [x] **A7. Verify Phase A:** (a) `python -m py_compile` all touched files; (b) **default-unchanged
  check** — with `profile=None`, prompt + tool set identical to before (guards AC3.1). — *AC3.1*

## Phase B — Scoped code search  (AC1.4)
- [x] **B1. `local_search.py`:** `search_code_local(project, query, scope: str | None = None)` —
  when `scope`, restrict ripgrep to `REPOS_DIR/<repo>/<scope>` and only return matches under it.
- [x] **B2. `tools.py`:** `build_rca_server(client, search_scope: str | None = None)` — pass
  `search_scope` into the `search_code_local` tool.
- [x] **B3. `agent.py`:** pass `profile.search_scope` into `build_rca_server`.
- [x] **B4. Verify Phase B:** reco scope target confirmed on the box —
  `repos/gst-enterprise-service/masters_india_saas/reconcile` exists with 119 files, and
  `search_scope` is threaded through `build_rca_server`; the reco-profile-v1 run used it. — *AC1.4*

## Phase C — Eval wiring  (AC2.1)
- [x] **C1. `eval/run.py`:** thread `profile=None` through `_one(...)` → `run_agent(profile=...)`.
- [x] **C2. `eval/task.py`:** `run_rca_task` resolves the active profile (via `EVAL_PROFILE` env
  or a module var set by `run_eval`) and passes it to `_one`.
- [x] **C3. `eval/run_eval.py`:** add `--profile <name>` arg → `get_profile(name)` → set it for
  the task run (and record it in `experiment_metadata`).
- [x] **C4. Verify Phase C:** `--profile reco` reaches `run_agent` — the run logged
  `profile=reco`, and the spawned Claude subprocess showed the composed reco system prompt.

## Phase D — Measure & accept  (AC2.2)
- [x] **D1. Smoke:** folded into the full run's launch verification — first-ticket subprocess
  showed the reco system prompt + summary in context, and the scope target was present (see C4/B4).
- [x] **D2. Full run:** `reco-profile-v1` complete — 16/16 task runs, 64 evaluations scored.
  Phoenix `Experiment:8` on dataset `rca-recon` (`RGF0YXNldDo0`).
- [x] **D3. Compare in Phoenix:** `baseline` (`Experiment:6`) vs `reco-profile-v1`
  (`Experiment:8`) — reviewed with the senior (2026-07-22).
- [x] **D4. Accept:** profile **ACCEPTED** by the senior. Follow-up he suggested: build a
  dataset of UNRESOLVED reconciliation-module tickets to run the reco profile against for real
  triage (tracked in its own spec — see `docs/specs/reco-unresolved/`, not here).

## Deployment gate (safety)
- [x] **Deployed.** reco-profile code is live on the box (via 113cf09/CD); the `rca-recon`
  baseline finished before the reco-profile-v1 run, so there is a stable comparison point.
  Everything is additive (`profile=None` default) — the live webapp path is unchanged. — *AC3.1, AC3.2*

## Notes
- Steps A + B are the "minimal profile"; adopt/measure before considering new tools (out of scope).
- Every task is reversible and additive; no change to the default agent behavior.
