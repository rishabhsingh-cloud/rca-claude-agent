# MI Agent — accuracy-first RCA for QA

An **accuracy-first root-cause-analysis agent** that reads a Jira bug/incident, investigates the
real system through **read-only tools** (self-hosted GitLab, New Relic, and the app databases),
and returns a **calibrated, QA-facing verdict** — what broke, why, whose fault it is, and what to
do next. A separate **Fix Suggester** agent can then propose a code fix for a human to review.

**Guiding principle: a confidently wrong RCA is worse than none.** When the evidence is thin, the
agent says so and hands off to a human rather than inventing a cause.

It runs live for the QA team as a **human-in-the-loop review dashboard**: the model orchestrates,
but every conclusion must be backed by a real artifact it fetched, and nothing is written to Jira
or GitLab without a person approving it.

---

## Table of contents

1. [Big picture](#big-picture)
2. [Two agents, cleanly separated](#two-agents-cleanly-separated)
3. [How the RCA agent works](#how-the-rca-agent-works)
4. [The verdict](#the-verdict)
5. [Tools](#tools-all-read-only-pii-masked)
6. [Data sources](#data-sources)
7. [The review dashboard](#the-review-dashboard-human-in-the-loop)
8. [The Fix Suggester](#the-fix-suggester)
9. [Tracing & observability (Phoenix)](#tracing--observability-phoenix)
10. [The evaluation harness](#the-evaluation-harness)
11. [Deployment](#deployment)
12. [Configuration](#configuration)
13. [Safety & governance](#safety--governance)
14. [Repository layout](#repository-layout)
15. [Running locally](#running-locally)
16. [Roadmap / known limitations](#roadmap--known-limitations)

---

## Big picture

```
                    ┌─────────────────────────────────────────────────────────┐
   Jira ticket ───▶ │  RCA agent (read-only)                                   │
                    │   orchestrates ~24 mcp__rca__* tools                     │
                    │   GitLab · New Relic · Postgres · MongoDB · web          │
                    └───────────────┬─────────────────────────────────────────┘
                                    │  structured Verdict (schema.py)
                                    ▼
                    ┌─────────────────────────────────────────────────────────┐
                    │  verify.py  — re-checks the verdict's claims against      │
                    │  GitLab; downgrades confidence if they don't resolve      │
                    └───────────────┬─────────────────────────────────────────┘
                                    ▼
        ┌───────────────────────────────────────────────────────────────────────┐
        │  Review dashboard (FastAPI, human-in-the-loop)                          │
        │   Triage · RCA review · Quality · Accept / Reject / Post-to-Jira         │
        │   └▶ optional: Fix Suggester (dry-run diff) → reviewed → draft MR        │
        └───────────────────────────────────────────────────────────────────────┘

   Cross-cutting: Phoenix tracing (every run)  ·  Phoenix eval harness (offline accuracy)
```

There are **two ways** to run an investigation, sharing the same tools + verdict schema:
- **Agent path** ([`agent.py`](rca_agent/agent.py)) — Claude orchestrates the tools; used for fuzzy
  tickets where judgement helps. This is what the dashboard uses.
- **Deterministic path** ([`investigation.py`](rca_agent/investigation.py)) — a trace-first
  pipeline with no model, for the clean "there's a stack trace" case.

---

## Two agents, cleanly separated

| Agent | Role | Access |
|---|---|---|
| **RCA agent** ([`agent.py`](rca_agent/agent.py)) | Investigates a ticket → structured verdict | **Strictly read-only** (~24 tools) |
| **Fix Suggester** ([`fix_agent.py`](rca_agent/fix_agent.py)) | Consumes a verdict → proposes a code fix | **Read-only** exploration (its own tool server); **dry-run** — writes nothing |

They share **no tool server** and **no credentials**: the RCA uses the `mcp__rca__*` server
([`tools.py`](rca_agent/tools.py)); the Fix Suggester has its own minimal `mcp__fix__*` server
([`fix_tools.py`](rca_agent/fix_tools.py)). This keeps the read-only RCA isolated and lets each
evolve independently.

---

## How the RCA agent works

Retrieval order, most deterministic first (confidence is calibrated *down* as it gets fuzzier):

1. **Production evidence first (New Relic).** For any crash / timeout / "not working", pull the
   real production error and logs — ground truth, treated like a stack trace.
2. **Stack trace → blame → introducing MR.** If there's a trace, fetch the exact line at a
   **pinned commit SHA**, `git_blame` it, and find the MR that introduced it → *is it a regression,
   and what changed?*
3. **No trace → localize in layers:** cross-service architecture map → per-repo summary →
   code/symbol search + the call graph.
4. **Exact-error lookup.** Many failures show a generic message ("Due to Wrong Input Data…") while
   the *real* reason is stored in the app's databases. `find_error_reason` checks the known error
   stores and returns the actual reason.
5. **Confirm data hypotheses** against the real (PII-masked) databases before blaming code.

**Hard rules baked into the system prompt** ([`prompts.py`](rca_agent/prompts.py)): don't repeat
guesses from ticket comments; no conclusion without an evidence chain of real fetched artifacts;
**check the timing** (an old, unchanged line can't cause a brand-new symptom); cover *every*
symptom; pin the ref so line numbers can't drift; and **never put customer PII in the verdict.** A
final self-check gate runs before it answers.

---

## The verdict

Structured, not an essay ([`schema.py`](rca_agent/schema.py)). Every RCA carries:

- a **VERDICT** — the one-line QA call, derived automatically from cause + confidence:
  **`BUG Accepted`** (our code/data/infra) · **`Not a BUG`** (customer's own action or a
  government/vendor system) · **`Needs review`** (evidence too thin to commit).
- **cause categories** (a list): `code · data · infrastructure · third_party · user_side · ux · unknown`
- **triage**: `real_bug · config · environment · likely_duplicate · insufficient_evidence`
- **confidence** (calibrated): `high` (chain confirmed against live code) · `medium` (one link
  inferred) · `low` (candidates only — needs a human).
- an **evidence chain** of clickable GitLab links (each link is `kind · ref · detail · url`, where
  kind ∈ `stack_frame · file_content · blame · commit · merge_request`),
- a **plain-language summary** for non-developers, **regression** yes/no + **introducing MR**, a
  **suggested next action**, **blast radius**, and — when evidence is thin — a list of **candidates**
  instead of a false-confident answer.

After the agent answers, [`verify.py`](rca_agent/verify.py) independently re-checks the concrete
claims (do the files / commits / MRs actually exist, via the GitLab API?) and **downgrades
confidence** if more than half fail. It never *fabricates* confidence; it only takes it away.

---

## Tools (all read-only, PII-masked)

The RCA agent's `mcp__rca__*` server exposes ~24 tools ([`tools.py`](rca_agent/tools.py)):

- **Orient:** `parse_stack_trace`, `route_repo`, `get_repo_summary`, `search_architecture`
- **GitLab ground truth:** `fetch_file_lines`, `git_blame`, `get_commit`, `merge_requests_for_commit`
- **Code graph:** `find_callers`, `find_dependents`, `get_subgraph`, `graph_has_edge`, `search_symbols`
- **Code search:** `search_code` (GitLab API), `search_code_local` (ripgrep on clones)
- **New Relic:** `search_nr_errors`, `search_nr_logs`, `query_nr`, `find_request_ids`, `trace_request`
- **App data:** `query_users_db` (Postgres), `query_app_data` (MongoDB), **`find_error_reason`**
- **Web:** `web_search`

---

## Data sources

Everything the agent reads is **read-only** and **PII-masked**. The agent draws on two kinds of
evidence: **code** (what the system is supposed to do) and **runtime/data** (what actually
happened).

| Source | Module | What it provides |
|---|---|---|
| **Self-hosted GitLab** | [`gitlab_client.py`](rca_agent/gitlab_client.py) | code, `git_blame`, commits, MRs — the *code* ground truth |
| **Jira Cloud** | [`jira.py`](rca_agent/jira.py) | the ticket (title/description/attachments); comments dropped so the agent isn't led by prior guesses |
| **New Relic** | [`newrelic.py`](rca_agent/newrelic.py) | production errors, logs, request traces — the *runtime* ground truth |
| **Postgres** (read replica) | [`app_db.py`](rca_agent/app_db.py) | users / orgs / **subscriptions** / import-job records — the *who/what-is-configured* |
| **MongoDB** (read-only) | [`app_mongo.py`](rca_agent/app_mongo.py) | the platform's **error/exception logs** — *what actually broke* |

### `find_error_reason` — the "get the real reason" tool

Many failures show the user a generic message while the *actual* error is written to a
domain-specific **MongoDB** collection — **not** to New Relic or Postgres. We mapped where each
class of error lives and wired them into one lookup ([`error_lookup.py`](rca_agent/error_lookup.py)):
given an identifier from the ticket (GSTIN / return period / import-job id / invoice), it checks all
of them in one call and returns the specific failure reason.

| Store (MongoDB) | Keyed by | Holds |
|---|---|---|
| `gstr1_exceptions` | gstin | GSTR-1 sales-import row errors (e.g. `"inv_typ incorrect"`) |
| `data_retrieval_api_logs` | gstin (+period) | GST-portal / NIC fetch errors; `error_case: "gov"` ⇒ the government side failed (→ *not our bug*) |
| `reco_invoice_error_logs` | gstin | reconciliation / force-match errors |
| `import_logs` | reference_id | the specific rejected import rows (GSM error code + message) |

Queries are indexed + time-capped so even the 100M+ row collections stay fast, and results are
PII-masked. **Note:** for the whole class of "import / GSTR-1 / reconciliation failed" tickets, the
answer lives in Mongo — if Mongo is unreachable the agent is investigating half-blind.

---

## The review dashboard (human-in-the-loop)

A FastAPI app ([`webapp/app.py`](rca_agent/webapp/app.py)) with a SQLite store
([`webapp/db.py`](rca_agent/webapp/db.py)) — the team's daily surface.

- **Triage tab** — lists Jira tickets (filter by date range + **work type**), pick one to run an
  RCA (`POST /api/tickets/{key}/rca`, background thread, ~3–4 min).
- **RCA review** — the verdict, evidence chain, and attachments; **Accept**, **Reject** (with a
  human correction), or **Accept & post to Jira** (add-only comment; never edits/deletes).
- **Quality tab** (`/api/quality`) — accuracy scoreboard: accept/reject counts, breakdown *by
  verdict* and *by cause bucket*.
- **Fix flow** — **Suggest a fix** (dry-run diff), then optionally **Raise MR** (draft) or **Reject
  fix**.

Selected endpoints: `GET /api/tickets`, `POST /api/tickets/{key}/rca`,
`POST /api/tickets/{key}/accept[_and_post]`, `POST /api/tickets/{key}/reject`,
`POST /api/tickets/{key}/suggest_fix`, `POST /api/tickets/{key}/raise_mr`, `GET /api/quality`.

The old **autonomous auto-posting daemon** ([`daemon.py`](rca_agent/daemon.py)) is **retired** in
favor of this human-in-the-loop review.

---

## The Fix Suggester

A **separate** agent that turns a verdict into a proposed code fix, for a human to review.

**Phase 1 — dry-run** ([`fix_agent.py`](rca_agent/fix_agent.py)): explores the one service the RCA
localized to, using its **own read-only tools** pinned to a single commit, proposes minimal
`before → after` snippets applied **in memory by exact string match** (refuses on drift or
non-unique match — no fuzzy patching), and **syntax-checks** each patched file. The dashboard shows
a **diff + rationale + caveats**. It **writes nothing**. Runs on a faster model (`RCA_FIX_MODEL`,
Sonnet) since the RCA stays on Opus.

**Phase 1b — draft MR** ([`fix_mr.py`](rca_agent/fix_mr.py)): turns a *reviewed* suggestion into a
**draft merge request**. This uses a dedicated GitLab bot token (`GITLAB_FIX_TOKEN`) that is
**Developer-scoped** — it can open an MR but structurally *cannot* merge protected `main`; a human
always merges.

---

## Tracing & observability (Phoenix)

Every RCA run can be traced to a **self-hosted [Arize Phoenix](https://phoenix.arize.com/)** so you
can open a run and see exactly where it went — every tool call, model input/output, and the
reasoning ([`trace.py`](rca_agent/trace.py)).

- **Off by default, fully guarded.** Active only when `RCA_TRACE=1` *and* the `[trace]` packages are
  installed *and* Phoenix is reachable. A tracing failure **never** breaks or slows a run — it
  degrades to a no-op.
- **Self-hosted, internal-only.** Traces contain customer data, so Phoenix runs on the box (a
  memory-fenced `phoenix` systemd service) — never a cloud tracing provider.
- **Live at** `https://agents.mastersindia.co/phoenix/` (behind nginx; see [Deployment](#deployment)).
  Production runs land in the `rca-agent` project.

---

## The evaluation harness

*How we know whether the agent is actually right — and whether a change made it better or worse.*
Lives in [`rca_agent/eval/`](rca_agent/eval/) (see its own [README](rca_agent/eval/README.md)).

Before this, every change to the agent (prompt, summary, tool, model) shipped on a hunch. The
harness runs the **production agent unchanged** over a curated set of tickets, scores each RCA, and
records it as a **Phoenix Experiment** you can compare before/after — with the full trace behind
every example.

### Why Phoenix Experiments

We already run Phoenix for tracing, and it has a built-in **Datasets → Experiments → Evaluators**
framework with a UI made for exactly this. Eval runs are recorded there (in a separate **`rca-eval`**
project so they don't clutter live traces) — same dashboard, its own tab.

### Ground truth = a small, hand-curated set

An RCA is **prose**, not a rigid schema, so scoring is **semantic**, and the ground truth is
prose too. One JSON object per line in `data/ground_truth.jsonl` (gitignored — it may quote
customer data; [`ground_truth.example.jsonl`](rca_agent/eval/ground_truth.example.jsonl) shows the
format):

| field | required | meaning |
|---|---|---|
| `ticket_key` | ✅ | the only input; the eval fetches the live ticket itself (same as prod) |
| `reference_rca` | ✅ | free text: the **true** root cause (+ fix if known) |
| `expected_repo` / `expected_bucket` / `is_regression` | — | optional tags for the deterministic side-checks |

### The flow

```bash
python -m rca_agent.eval seed --limit 15   # (optional) draft candidates from resolved Jira tickets
#   ... hand-write the true root cause into data/ground_truth.jsonl ...
python -m rca_agent.eval upload            # push to Phoenix as a Dataset
python -m rca_agent.eval run --experiment baseline   # run the REAL agent + score it
```

- **task** ([`task.py`](rca_agent/eval/task.py)) reuses the production run (`run.py`'s `_one` →
  `run_agent` / `parse_verdict` / `verify_verdict`) **verbatim**, concurrency-capped
  (`EVAL_CONCURRENCY`) for the memory-tight box.
- **scorers** ([`evaluators.py`](rca_agent/eval/evaluators.py)):
  - **`rca_matches`** (primary, LLM judge) → `correct` / `partial` / `wrong` / `unknown` by
    comparing the agent's RCA to `reference_rca`. This is the real score.
  - **`repo_correct` / `cause_bucket_correct` / `regression_correct`** (deterministic) — cheap
    objective checks, scored only where the row carries that tag.

### The improve → re-measure loop

Run a `baseline` experiment → change the agent → run another → Phoenix's **compare view** shows
which tickets improved and which regressed, each linking to its trace so you can see *why*. A real
before/after number instead of a guess.

### Why it can't cheat or hurt prod

- **No answer-leakage.** The task passes **only** the `ticket_key`; `reference_rca` lives in the
  dataset's *output* column and is never given to the agent. The ticket is fetched with
  `drop_all_comments=True`, so the human resolution comment (often the reference) is **not** shown —
  the agent must reach the answer independently.
- **Sandboxed + read-only.** Never opens the webapp DB; uses only read paths of Jira/GitLab.
- **Agent imported unchanged** — it measures reality and mutates nothing.

### Notes

- **Auth:** the agent runs on the box's **Claude subscription** (ambient login), not an API key.
  The eval **warns** (doesn't block) if `ANTHROPIC_API_KEY` is unset — fine for a small set; a large
  batch may hit interactive rate limits.
- ⚠️ **Run from the repo root, do NOT `source .env`.** The app loads `.env` itself
  ([`config.py`](rca_agent/config.py) `_load_dotenv`); `source`-ing it truncates `APP_MONGO_URI` at
  the `&` in its query string (shell backgrounding), silently breaking the agent's Mongo lookups.
- Needs the lightweight client: `pip install -e ".[eval]"` (`arize-phoenix-client`).

---

## Deployment

- **Live on an internal EC2 host** (VPN-only) as user `systemd` services:
  - `rca-webapp` — FastAPI + uvicorn, the review dashboard (`deploy/rca-webapp.service`).
  - `phoenix` — the self-hosted trace/eval viewer, memory-fenced.
  - `rca-reindex` — a nightly one-shot timer that rebuilds code graphs + repo summaries
    (`deploy/rca-reindex.service`).
- **nginx** (behind the corp reverse proxy) serves `agents.mastersindia.co`:
  - `/` → the dashboard (`127.0.0.1:8000`)
  - `/phoenix/` → Phoenix (`127.0.0.1:6006`, prefix stripped; Phoenix runs with
    `PHOENIX_HOST_ROOT_PATH=/phoenix` so its UI asset links resolve).
- **CI/CD** (`.github/workflows/ci.yml`): push to `main` → tests on GitHub cloud → a self-hosted
  runner on EC2 does `git pull` + `pip install -e ".[live,webapp,appdb,trace]"` + restart the
  webapp. *(The `[eval]` extra is deliberately **not** in the deploy install line — the eval client
  is installed manually on the box so a bad dep can't break a prod deploy.)*

---

## Configuration

Backend is selected by `RCA_BACKEND`:
- **`mock`** (default) — on-disk fixtures under `fixtures/`; no network, no credentials. The test
  suite runs here.
- **`live`** — the real GitLab / Jira / New Relic / DB integrations.

Everything else comes from `.env` (see `.env.example` for the full list). Common keys:

| Area | Keys |
|---|---|
| Models | `RCA_MODEL` (Opus), `RCA_FIX_MODEL` (Sonnet) |
| GitLab | `GITLAB_URL`, `GITLAB_TOKEN` (read), `GITLAB_FIX_TOKEN` (Developer-scoped, for draft MRs) |
| Jira | `JIRA_URL`, `JIRA_EMAIL`, `JIRA_TOKEN` |
| Data | `APP_MONGO_URI`, `APP_MONGO_DB`, Postgres URL; New Relic key + account |
| Code search | `REPOS_DIR` (cloned repos for `search_code_local` + `verify.py`) |
| Tracing | `RCA_TRACE=1`, `PHOENIX_COLLECTOR_ENDPOINT`, `PHOENIX_PROJECT` |
| Eval | `PHOENIX_BASE_URL`, `EVAL_PHOENIX_PROJECT`, `EVAL_CONCURRENCY`, `EVAL_MAX_TURNS` |

> **Secrets:** `.env` holds live credentials. Never `cat`/paste it. The read-only Mongo user's
> password and `GITLAB_FIX_TOKEN` are on the rotation list.

---

## Safety & governance

- **RCA is strictly read-only.** No RCA tool writes anywhere.
- **PII never enters the prompt or the output.** DB/log tools mask by column name *and* value
  pattern (GSTIN / PAN / email / phone / …); the prompt forbids copying identifiers from the ticket
  into the verdict. GSTINs may be used as *query filters* but never echoed.
- **Human-in-the-loop.** The team reviews every RCA and clicks Accept / Reject / Accept-and-post;
  posting to Jira only happens on approval (add-only — never edits or deletes a comment).
- **Bounded & safe queries.** Read replicas, SELECT-only + server read-only guard on Postgres,
  find-only on Mongo, per-query time caps, row/step budgets.
- **The Fix Suggester is dry-run and separate** from the RCA, by design; the MR path is
  Developer-scoped (cannot merge protected `main`).
- **Tracing/eval are self-hosted + internal-only** because traces contain customer data.

---

## Repository layout

```
rca_agent/
  agent.py          RCA agent (Agent SDK loop) + verdict parsing
  investigation.py  deterministic trace-first pipeline (no model)
  prompts.py        the accuracy-guardrail system prompt
  schema.py         Verdict / VERDICT label / cause buckets / render + Jira ADF
  tools.py          the read-only RCA tool server (mcp__rca__*)
  verify.py         post-hoc verification of the verdict's claims (via GitLab API)
  fix_agent.py      Fix Suggester (explorer, dry-run) — SEPARATE agent
  fix_tools.py      the Fix Suggester's own read-only tool server (mcp__fix__*)
  fix_mr.py         Phase 1b — raise a DRAFT merge request from a reviewed fix
  error_lookup.py   find_error_reason — one lookup across the domain error stores
  newrelic.py       New Relic (errors, logs, request tracing)
  app_db.py         Postgres access + shared PII masking
  app_mongo.py      MongoDB access (find-only, masked, time-capped)
  gitlab_client.py  GitLab client (mock + live REST), read-only
  jira.py / tickets.py / stack_trace.py   ticket fetch + parsing
  graph*.py         symbol-level call graph (graph / graph_store / graphify_adapter)
  architecture.py / routing.py / summarize.py / index.py / reindex.py   localization + indexing
  search.py         web search (Tavily)
  trace.py          optional Phoenix tracing (guarded no-op unless RCA_TRACE=1)
  daemon.py         retired autonomous loop (kept for reference)
  run.py            CLI entry point
  eval/             the Phoenix evaluation harness (see eval/README.md)
  webapp/           FastAPI review dashboard (app.py + db.py + static/index.html)
deploy/             systemd units (rca-webapp, rca-reindex)
fixtures/           mock backend data (repos, summaries, architecture)
index/              live repo summaries + architecture map
tests/              deterministic tests against fixtures
```

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,live,webapp,appdb]"

# tests (mock backend — no network/credentials)
RCA_BACKEND=mock RCA_INDEX_DIR="$PWD/fixtures" .venv/bin/python -m pytest tests/

# dashboard
.venv/bin/uvicorn rca_agent.webapp.app:app --host 127.0.0.1 --port 8000
```
The live path needs the company VPN, a Claude subscription (or `ANTHROPIC_API_KEY`), and the
integration credentials in `.env`.

---

## Roadmap / known limitations

- **Fix Suggester** is best on **localized** fixes; large cross-file redesigns are a starting point
  at best, and it only sees code it can search within one service. Real sandbox *verification* that
  a fix works is out of scope ("syntax OK" means it parses, not that it's correct).
- **Not every failure is recorded where the agent can read it** — some truly-unhandled errors live
  only in server log files and need an app-side change to surface.
- **Eval is young:** a small hand-curated ground-truth set to start; growing it (and a
  `suggest.py` step that turns failures into ranked agent-change suggestions) is next. A
  "living knowledge loop" — confirmed RCAs feeding back so the agent gets smarter — is planned.
```
