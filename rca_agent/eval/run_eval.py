"""Run the RCA agent as a Phoenix EXPERIMENT over the ground-truth dataset.

  python -m rca_agent.eval run [--name rca-ground-truth] [--experiment <label>]
                               [--timeout 7200] [--dry-run]

Fetches the dataset from Phoenix, runs the production agent on each ticket
(run_rca_task), scores each with the evaluators, and records everything as an
experiment you inspect + compare in the Phoenix UI (Datasets -> the dataset ->
Experiments). Every example cross-links to its trace.

The compare workflow: run one experiment on the baseline, make a change to the
agent, run another -> Phoenix shows per-example deltas (what improved / regressed).

Requires ANTHROPIC_API_KEY (batch model work must use the API key, not a
subscription/OAuth profile, which would hit interactive rate limits mid-run).
"""

from __future__ import annotations

import argparse
import os
import subprocess

from ._common import require_api_key
from .dataset import DEFAULT_DATASET
from .evaluators import ALL_EVALUATORS
from .phoenix_client import base_url, client
from .task import run_rca_task


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "manual"


def main(argv=None) -> None:
    require_api_key()
    p = argparse.ArgumentParser(description="Run the RCA agent as a Phoenix experiment.")
    p.add_argument("--name", default=DEFAULT_DATASET, help="Phoenix dataset name")
    p.add_argument("--experiment", default="", help="experiment label (default: git sha)")
    p.add_argument("--timeout", type=int, default=7200,
                   help="per-run ceiling in seconds (RCA runs are ~10 min)")
    p.add_argument("--dry-run", action="store_true",
                   help="run tasks + evals but do NOT record to Phoenix")
    a = p.parse_args(argv)

    # Eval runs emit their OWN traces into a separate project so live prod traces
    # (the rca-agent project) stay uncluttered. No-op if tracing isn't installed.
    os.environ.setdefault("RCA_TRACE", "1")
    os.environ["PHOENIX_PROJECT"] = os.getenv("EVAL_PHOENIX_PROJECT", "rca-eval")

    c = client()
    ds = c.datasets.get_dataset(dataset=a.name)
    label = a.experiment or _git_sha()

    print(f"eval: running experiment '{label}' over dataset '{a.name}' "
          f"(dry_run={a.dry_run}). Each ticket takes minutes — be patient.")
    ran = c.experiments.run_experiment(
        dataset=ds,
        task=run_rca_task,
        evaluators=ALL_EVALUATORS,
        experiment_name=label,
        experiment_metadata={"git_sha": _git_sha()},
        timeout=a.timeout,
        dry_run=a.dry_run,
        print_summary=True,
    )
    print(f"\neval: done. Open Phoenix ({base_url()}) -> Datasets -> '{a.name}' -> "
          f"Experiments to see scores + per-example traces.")
    return ran


if __name__ == "__main__":
    main()
