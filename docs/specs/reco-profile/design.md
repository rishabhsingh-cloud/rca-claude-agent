# Design — Reconciliation RCA Profile

> Traces to `requirements.md`. Each section notes the AC(s) it satisfies.

## Approach (→ AC3.1, AC3.2)
Add an optional **`profile`** argument to the existing `run_agent`. `profile=None` → today's
behavior, unchanged. A profile carries the reco-specific overrides. No new agent, no forked
loop — we reuse the loop, verdict parsing, tracing, and eval harness. Everything is additive.
**The general system prompt (`prompts.py`) is never modified** — a profile gets its *own*
system prompt, composed in `profiles/` by reusing `build_system_prompt`'s output and
inserting the profile block (constraint from review).

## Core data structure
```python
# rca_agent/profiles/__init__.py   (new package: rca_agent.profiles)
@dataclass(frozen=True)
class AgentProfile:
    name: str
    system_addendum: str          # appended to the base system prompt
    summary: str = ""             # module summary, injected into the opening turn
    search_scope: str | None = None   # path prefix to restrict code search, e.g. "masters_india_saas/reconcile"
    allowed_tools: list[str] | None = None   # None -> the default full RCA tool set

RECO_PROFILE = AgentProfile(
    name="reco",
    system_addendum=<reco data-model + cause-calibration text>,
    summary=<contents of rca_agent/profiles/reco_summary.md>,
    search_scope="masters_india_saas/reconcile",
)
```
The reco summary lives in-repo at **`rca_agent/profiles/reco_summary.md`** (a copy of the
curated `reconcile.md`; PII-free codebase doc). It's loaded at import.

## Component changes (each → AC)

| File | Change | Satisfies |
|---|---|---|
| `rca_agent/profiles/` *(new package)* | `__init__.py`: `AgentProfile` + `RECO_PROFILE` + `get_profile(name)` + **`build_profile_system_prompt()`** (reuses the *unmodified* general prompt and inserts the reco block before the workflow section) | AC1.1–1.4 |
| `rca_agent/profiles/reco_summary.md` *(new)* | the reconciliation module summary (curated `reconcile.md`) | AC1.2 |
| `pyproject.toml` | add `rca_agent.profiles` to `packages`; ship the `.md` as package data | (packaging) |
| `prompts.py` | **NOT modified** (hard constraint) — reco's prompt is composed *from* its unmodified output, in `profiles/` | — |
| `agent.py` | `run_agent(..., profile=None)`: `system_prompt = build_profile_system_prompt(profile, url)` when a profile is set, else the plain `build_system_prompt(url)`; prepend `profile.summary` to the opening turn; `profile.allowed_tools or default`; (Phase B) pass `profile.search_scope` to `build_rca_server` | AC1.1–1.4, AC3.1 |
| `tools.py` | `build_rca_server(client, search_scope=None)` → pass scope into the `search_code_local` tool | AC1.4 |
| `local_search.py` | `search_code_local(project, query, scope=None)` → when `scope`, restrict ripgrep to `REPOS_DIR/<repo>/<scope>` and only return matches under it | AC1.4 |
| `eval/run_eval.py` | `--profile reco` arg → resolve to `RECO_PROFILE`, set for the task run | AC2.1 |
| `eval/task.py`, `eval/run.py` | thread `profile` into `_one(...)` → `run_agent(profile=...)` | AC2.1 |

## Data flow (a reco eval run)
```
eval run --name rca-recon --experiment reco-profile-v1 --profile reco
  → run_eval resolves RECO_PROFILE, passes it to the task
  → task.run_rca_task(example) → run._one(row, ..., profile=RECO_PROFILE)
  → run_agent(..., profile=RECO_PROFILE)
        system_prompt = build_profile_system_prompt   (general prompt UNMODIFIED + reco block inserted)  (AC1.1, AC1.3)
        opening turn   = reco summary + ticket text    (AC1.2)
        tools          = default set, search scoped to reco module   (AC1.4)
  → same loop / verdict / verify / tracing as always   (AC3.2)
  → Phoenix experiment scored by the same evaluators    (AC2.1)
```

## Key decisions & trade-offs
1. **Profile object, not a subclass/fork.** *Why:* reuse of the tested loop + eval + tracing;
   one code path to maintain; `profile=None` guarantees prod is untouched (AC3.1/3.2). *Cost:*
   `run_agent` grows one optional param — acceptable.
2. **Reco summary injected into the opening user turn, not `get_repo_summary`.** *Why:*
   guarantees the agent always has it (no dependence on it choosing to call the tool), and
   keeps the system prompt stable. *Trade-off:* ~16 KB of fixed context per run — fine.
   `get_repo_summary` still works for anything else.
3. **Search scoping = a path-prefix filter on `search_code_local`.** *Why:* smallest change;
   works regardless of which repo the reco code lives in (filters by the module path). *Open:*
   whether to also scope the *repo* clone — deferred (see open question in requirements).
4. **Profile selected explicitly via `--profile`,** not auto-detected from the ticket. *Why:*
   keeps the eval deterministic and the change minimal; auto-routing is a later concern.

## Traceability (AC → where satisfied)
- AC1.1 reco data model → `profiles.build_profile_system_prompt` inserts `RECO_PROFILE.system_addendum` into the **unmodified** general prompt (prompts.py untouched)
- AC1.2 reco summary up front → `agent.py` opening-turn injection + `reco_summary.md`
- AC1.3 cause calibration → `RECO_PROFILE.system_addendum`, composed in via `build_profile_system_prompt`
- AC1.4 scoped search → `local_search.py` + `tools.py` scope plumbing
- AC2.1 measured experiment → `eval/run_eval.py --profile` + task threading
- AC2.2 accepted only on positive delta → **process**, not code: compare `baseline` vs
  `reco-profile-*` in Phoenix before adopting
- AC3.1 default unchanged → `profile=None` default across the call chain
- AC3.2 reuse, no fork → single `run_agent`, unchanged loop/verdict/tracing

## Testing / verification
- **Unit-ish smoke:** run 1 reco ticket with `--profile reco`; assert (via the trace/tools_used)
  that the reco summary is in context and code search stayed under `masters_india_saas/reconcile`.
- **Default-unchanged check:** a run with no profile produces the same tool set + prompt as before
  (guards AC3.1).
- **Acceptance (AC2.2):** Phoenix compare `baseline` vs `reco-profile-v1` on `rca-recon`;
  adopt only if `rca_matches` rises with no regressions.

## Risks
- The 16 KB summary in every run adds context/latency — acceptable, and offset by the
  smaller search scope. Revisit if latency regresses.
- Path-scoped search could miss cross-module causes; the reco summary's "delegates to" notes
  mitigate. If the eval shows misses, widen scope.
