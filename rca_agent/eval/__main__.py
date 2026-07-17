"""CLI entrypoint: python -m rca_agent.eval <seed|upload|run> [options].

  seed     Draft ground-truth candidates from resolved Jira tickets
           -> data/ground_truth.draft.jsonl  (you curate -> data/ground_truth.jsonl)
  upload   Push data/ground_truth.jsonl to Phoenix as a Dataset
  run      Run the real RCA agent as a Phoenix EXPERIMENT + score it

Typical flow:
  python -m rca_agent.eval seed --limit 15     # optional: draft from Jira
  # ... hand-write reference_rca into data/ground_truth.jsonl ...
  python -m rca_agent.eval upload
  python -m rca_agent.eval run --experiment baseline

`run` requires ANTHROPIC_API_KEY (batch model work must not use a subscription
profile). Everything is sandboxed from production — see rca_agent/eval/README.md.
"""

from __future__ import annotations

import sys

from . import dataset, run_eval


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("seed", "upload", "run"):
        sys.exit(__doc__)
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "seed":
        dataset.seed_main(rest)
    elif cmd == "upload":
        dataset.upload_main(rest)
    elif cmd == "run":
        run_eval.main(rest)


if __name__ == "__main__":
    main()
