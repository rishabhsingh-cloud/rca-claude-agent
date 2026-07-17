# RCA eval harness (on Phoenix)

Run the **real** RCA agent over a small, hand-curated set of tickets, score each RCA
against ground truth, and see it all in the self-hosted **Phoenix** UI — with
before/after experiment comparison and a trace behind every example. Built for the
"measure accuracy + improve the agent" directive.

The scoring is **semantic**: an RCA is a few paragraphs of prose, so an LLM judge
compares *meaning* against a human-written reference — not string/field matching.

## Why it can't hurt production

- **Read-only + sandboxed.** Uses only read paths of Jira/GitLab (`search`/`get`/
  attachments) and the GitLab read client. No `add_comment`, no accept/post, no writes.
  Never opens the webapp's `rca_reviews.db`.
- **Agent imported unchanged.** Calls the production `run_agent` / `parse_verdict` /
  `verify_verdict` as-is (via `run.py`) — it measures reality and mutates nothing.
- **Its own Phoenix project.** Eval runs trace into `rca-eval`, so live prod traces
  (`rca-agent`) stay uncluttered. Same Phoenix server/dashboard, separate tab.
- **Same auth as prod.** Uses the box's ambient Claude auth (the subscription/OAuth
  session), like the live agent. `run` only *warns* if there's no `ANTHROPIC_API_KEY`
  — a *large* batch on a subscription can hit interactive rate limits mid-run, but a
  small eval set is fine.
- **Ground truth is gitignored** (`data/`) — its prose may quote customer data.

## Setup

Runs on the box (EC2), where it can reach Jira/GitLab/DBs **and** the Phoenix server.

```bash
# The eval needs the lightweight Phoenix REST client (not the full server):
/home/rishabh/rca-claude-agent/.venv/bin/pip install -e ".[eval]"

# Load the box's env (Jira/GitLab creds, Claude auth, REPOS_DIR, ...) into the shell,
# so the manually-run CLI + the agent subprocess it spawns both see it:
set -a; source .env; set +a
# PHOENIX_BASE_URL defaults to http://localhost:6006 (the local server on the box).
```

## Usage

```bash
# 1. (optional) draft candidates from resolved Jira tickets to label
python -m rca_agent.eval seed --limit 15
#    -> data/ground_truth.draft.jsonl : for each row, read _comments_for_reference,
#       write the TRUE root cause into reference_rca, drop the _-prefixed fields,
#       and save the curated rows as data/ground_truth.jsonl

# 2. upload the curated set to Phoenix as a Dataset
python -m rca_agent.eval upload            # --append to add to an existing one

# 3. run the real agent as an experiment (minutes per ticket; unattended)
python -m rca_agent.eval run --experiment baseline
```

Then open Phoenix → **Datasets → rca-ground-truth → Experiments** to see per-example
scores, the aggregate, and the trace behind each run.

## Ground-truth format

One JSON object per line in `data/ground_truth.jsonl` (see
[`ground_truth.example.jsonl`](ground_truth.example.jsonl) for a filled example):

| field | required | meaning |
|---|---|---|
| `ticket_key` | ✅ | the only input; the eval fetches the live ticket itself |
| `reference_rca` | ✅ | free-text: the **true** root cause (+ fix if known) |
| `expected_repo` | — | tag for the `repo_correct` side-check |
| `expected_bucket` | — | tag for the `cause_bucket_correct` side-check |
| `is_regression` | — | tag for the `regression_correct` side-check |
| `tags` | — | freeform labels (not scored) |

## Scoring

- **`rca_matches`** (primary, LLM judge) → `correct` / `partial` / `wrong` / `unknown`
  by comparing the agent's RCA to `reference_rca`. This is the real score.
- **`repo_correct` / `cause_bucket_correct` / `regression_correct`** (deterministic) —
  cheap objective checks, scored only where the row carries that tag.

## Improve → re-measure loop

Run a `baseline` experiment → change the agent (prompt, summary, tool lockdown, model)
→ run another experiment → Phoenix's compare view shows which tickets improved and
which regressed, each linking to its trace so you can see *why*. That's a real
before/after number instead of a hunch.

## Known limits (deferred)

- `seed`'s `jira.search` caps at ~100 and doesn't paginate — fine for a pilot.
- The `suggest` step (failures → ranked "change these things in the agent") is the
  next increment, built once there's a real experiment to analyze.
- The judge sees the reference (and, in `seed`, raw comments) — inside the product's
  trust boundary; a masking pass is a follow-up if required.
