# Triage Agent — accuracy-first RCA for QA

An **accuracy-first root-cause-analysis agent** that reads a Jira bug/incident, investigates
the real system through **read-only tools** (self-hosted GitLab, New Relic, and the app
databases), and returns a **calibrated, QA-facing verdict** — what broke, why, whose fault it
is, and what to do next. A separate **Fix Suggester** agent can then propose a code fix for a
human to review.

Guiding principle: **a confidently wrong RCA is worse than none.** When the evidence is thin,
the agent says so and hands off to a human rather than inventing a cause.

Runs live for the QA team as a **human-in-the-loop review dashboard**; the model orchestrates,
but every conclusion must be backed by a real artifact it fetched, and nothing is written to
Jira or GitLab without a person approving it.

---

## Two agents, cleanly separated

| Agent | Role | Access |
|---|---|---|
| **RCA agent** (`agent.py`) | Investigates a ticket → structured verdict | **Strictly read-only** (24 tools) |
| **Fix Suggester** (`fix_agent.py`) | Consumes a verdict → proposes a code fix | **Read-only** (its own separate tool server); **dry-run** — writes nothing |

They share **no tool server** and **no credentials**: the RCA uses the `mcp__rca__*` server; the
Fix Suggester has its own minimal `mcp__fix__*` server ([`fix_tools.py`](rca_agent/fix_tools.py)).
This keeps the read-only RCA isolated and lets each evolve without affecting the other.

---

## How the RCA agent works

Retrieval order, most deterministic first (calibrate confidence *down* as it gets fuzzier):

1. **Production evidence first (New Relic).** For any crash / timeout / "not working", pull the
   real production error and logs — ground truth, treated like a stack trace.
2. **Stack trace → blame → introducing MR.** If there's a trace, that's ground truth: fetch the
   exact line at a **pinned commit SHA**, `git_blame` it, find the MR that introduced it → *is it
   a regression, and what changed?*
3. **No trace → localize in layers:** cross-service architecture map → per-repo summary →
   code/symbol search + the call graph.
4. **Exact-error lookup.** Many failures show a generic message ("Due to Wrong Input Data…")
   while the *real* reason is stored in the app's databases. One tool
   (`find_error_reason`) checks the known error stores and returns the actual reason.
5. **Confirm data hypotheses** against the real (PII-masked) databases before blaming code.

**Hard rules baked into the system prompt** ([`prompts.py`](rca_agent/prompts.py)): don't repeat
guesses from ticket comments; no conclusion without an evidence chain of real fetched artifacts;
**check the timing** (an old, unchanged line can't cause a brand-new symptom); cover *every*
symptom; pin the ref so line numbers can't drift; and **never put customer PII in the verdict**.
A final self-check gate runs before it answers.

### The verdict

Structured, not an essay ([`schema.py`](rca_agent/schema.py)). Every RCA carries:

- a **VERDICT** — the one-line QA call, derived automatically from the cause + confidence:
  **`BUG Accepted`** (our code/data/infra) · **`Not a BUG`** (customer's own action or a
  government/vendor system) · **`Needs review`** (evidence too thin to commit).
- **cause categories** (a list): `code · data · infrastructure · third_party · user_side · ux · unknown`
- a plain-language summary for non-developers, an **evidence chain** of clickable GitLab links,
  regression yes/no + introducing MR, a suggested next action, blast radius, and a
  **calibrated confidence** (high / medium / low).

After the agent answers, [`verify.py`](rca_agent/verify.py) independently re-checks its concrete
claims (files, commits, MRs actually exist via the GitLab API) and **downgrades confidence** if
they don't.

---

## Tools (all read-only, PII-masked)

The RCA agent's `mcp__rca__*` server exposes 24 tools:

- **Orient:** `parse_stack_trace`, `route_repo`, `get_repo_summary`, `search_architecture`
- **GitLab ground truth:** `fetch_file_lines`, `git_blame`, `get_commit`, `merge_requests_for_commit`
- **Code graph:** `find_callers`, `find_dependents`, `get_subgraph`, `graph_has_edge`, `search_symbols`
- **Code search:** `search_code` (GitLab API), `search_code_local` (ripgrep on clones)
- **New Relic:** `search_nr_errors`, `search_nr_logs`, `query_nr`, `find_request_ids`, `trace_request`
- **App data:** `query_users_db` (Postgres), `query_app_data` (MongoDB), **`find_error_reason`**
- **Web:** `web_search`

`find_error_reason` is the "get the real reason" tool. Many failures show the user a generic
message while the *actual* error is written to a domain-specific **MongoDB** collection — **not**
to New Relic or Postgres. We identified where each class of error lives and wired them into one
lookup: given an identifier from the ticket (GSTIN / return period / import-job id), it checks all
of them in one call and returns the specific failure reason:

| Store (MongoDB) | Holds |
|---|---|
| `gstr1_exceptions` | GSTR-1 sales-import row errors (e.g. `exception: "inv_typ incorrect"`) |
| `data_retrieval_api_logs` | GST-portal / NIC fetch errors, with `error_case: "gov"` = the government side failed (→ *not our bug*) |
| `reco_invoice_error_logs` | reconciliation / force-match errors |
| `import_logs` | the specific rejected import rows (by import-job id) |

Queries are indexed + time-capped so even the 100M+ row collections stay fast, and results are
PII-masked.

---

## The Fix Suggester (Phase 1 — dry-run)

A **separate** agent that turns a verdict into a proposed code fix, for a human to review in the
dashboard. **It writes nothing** — no branch, no MR, no commit.

Flow ([`fix_agent.py`](rca_agent/fix_agent.py)):

1. A human clicks **"Suggest a fix"** on a ticket that has an RCA (always human-initiated).
2. The agent explores the one service the RCA localized to, using its **own read-only tools**
   (`search_code` via the GitLab API, `fetch_file_lines`, the code graph) — pinned to a single
   commit — to find *all* the code the fix needs, even across several files.
3. It proposes minimal `before → after` snippets. These are applied **in memory by exact string
   match** (it refuses if the code drifted or the snippet isn't unique — no fuzzy patching), then
   each patched file is **syntax-checked**.
4. The dashboard shows a **diff per file + rationale + caveats**. A human reviews it.

Runs on a **faster model than the RCA** (Sonnet, via `RCA_FIX_MODEL`) since exploration + a
minimal patch don't need the RCA's model; the RCA stays on Opus.

**Phase 1b (next):** turn the reviewed suggestion into a **draft merge request** — which needs a
dedicated GitLab bot account with a write-scoped, **Developer-only** token (so it can open an MR
but structurally *cannot* merge protected `main` — a human always merges).

---

## Safety & governance

- **RCA is strictly read-only.** No tool writes anywhere.
- **PII never enters the prompt or the output.** DB/log tools mask by column name *and* value
  pattern (GSTIN / PAN / email / phone / …); the prompt forbids copying identifiers from the
  ticket into the verdict. GSTINs may be used as *query filters* but never echoed.
- **Human-in-the-loop.** The team reviews every RCA and clicks Accept / Reject / Accept-and-post;
  posting to Jira only happens on approval (add-only — the app never edits or deletes a comment).
- **Bounded & safe queries.** Read replicas, SELECT-only + server read-only guard on Postgres,
  find-only on Mongo, per-query time caps, row/step budgets.
- **The fix agent is dry-run and separate** from the RCA, by design.

---

## Deployment

- **Live on an internal EC2 host** (VPN-only) as a user `systemd` service, `rca-webapp`
  (FastAPI + uvicorn) — the human review dashboard.
- **CI/CD:** push to `main` → a self-hosted GitHub Actions runner on EC2 → `git pull` +
  `pip install` + restart the service.
- A nightly **re-index timer** rebuilds the code graphs + repo summaries.
- The autonomous auto-posting daemon is **retired** in favor of human-in-the-loop review.

**Live integrations:** self-hosted GitLab (read scope), Jira Cloud (REST), New Relic (NerdGraph),
Postgres (read replica), MongoDB (read-only).

---

## Backends

Selected by `RCA_BACKEND`:
- **`mock`** (default) — on-disk fixtures under `fixtures/`; no network, no credentials. The test suite runs here.
- **`live`** — the real GitLab / Jira / New Relic / DB integrations.

---

## Repository layout

```
rca_agent/
  agent.py          RCA agent (Agent SDK loop) + verdict parsing
  prompts.py        the accuracy-guardrail system prompt
  schema.py         Verdict / VERDICT label / cause buckets / render + Jira ADF
  tools.py          the read-only RCA tool server (mcp__rca__*)
  verify.py         post-hoc verification of the verdict's claims (via GitLab API)
  fix_agent.py      Fix Suggester (explorer, dry-run) — SEPARATE agent
  fix_tools.py      the Fix Suggester's own read-only tool server (mcp__fix__*)
  error_lookup.py   find_error_reason — one lookup across the domain error stores
  newrelic.py       New Relic (errors, logs, request tracing)
  app_db.py         Postgres access + shared PII masking
  app_mongo.py      MongoDB access (find-only, masked, time-capped)
  gitlab_client.py  GitLab client (mock + live REST), read-only
  graph.py / graph_store.py / graphify_adapter.py   symbol-level call graph
  architecture.py / routing.py / summarize.py / index.py   localization + indexing
  webapp/           FastAPI review dashboard (app.py + static/index.html)
tests/              deterministic tests against fixtures
```

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,live,webapp,appdb]"
RCA_BACKEND=mock RCA_INDEX_DIR="$PWD/fixtures" .venv/bin/python -m pytest tests/   # tests
.venv/bin/uvicorn rca_agent.webapp.app:app --host 127.0.0.1 --port 8000            # dashboard
```
The live path needs the company VPN, `ANTHROPIC_API_KEY`, and the integration credentials in `.env`.

---

## Recent additions

- **VERDICT label** (`BUG Accepted` / `Not a BUG` / `Needs review`) shown on the dashboard and the
  Jira comment, with a fixed "Automated RCA — verify before acting" banner.
- **`find_error_reason`** — surfaces the real error behind a generic message from the app's error
  stores (verified: e.g. returns "inv_typ incorrect" instead of "contact support").
- **Verification via the GitLab API**, so RCAs aren't unfairly downgraded on setups without local
  repo clones.
- **The Fix Suggester** (Phase 1, dry-run) with its own separate tool server.

## Known limitations / roadmap

- The Fix Suggester is **dry-run** and best on **localized** fixes; large cross-file redesigns are
  a starting point at best, and it only sees code it can search within one service. Phase 1b adds
  the reviewed → draft-MR path (needs the write-scoped bot token).
- Real sandbox *verification* that a fix works is out of scope (these services are hard to
  sandbox); "syntax OK" means it parses, not that it's correct.
- Not every failure is recorded where the agent can read it — some truly-unhandled errors are only
  in server log files, and need an app-side change to surface.
```
