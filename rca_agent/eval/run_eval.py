"""Run the RCA agent as a Phoenix EXPERIMENT over the ground-truth dataset.

  python -m rca_agent.eval run [--name rca-ground-truth] [--experiment <label>]
                               [--timeout 7200] [--dry-run]

Fetches the dataset from Phoenix, runs the production agent on each ticket
(run_rca_task), scores each with the evaluators, and records everything as an
experiment you inspect + compare in the Phoenix UI (Datasets -> the dataset ->
Experiments). Every example cross-links to its trace.

The compare workflow: run one experiment on the baseline, make a change to the
agent, run another -> Phoenix shows per-example deltas (what improved / regressed).

Uses the box's ambient Claude auth (the subscription session), same as the live
agent — no API key required. Warns (doesn't block) if none is set, since a LARGE
batch on a subscription can hit interactive rate limits mid-run.
"""

from __future__ import annotations

import argparse
import os
import subprocess

from ._common import warn_no_api_key
from .dataset import DEFAULT_DATASET
from .evaluators import ALL_EVALUATORS
from ..profiles import get_profile
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
    warn_no_api_key()
    p = argparse.ArgumentParser(description="Run the RCA agent as a Phoenix experiment.")
    p.add_argument("--name", default=DEFAULT_DATASET, help="Phoenix dataset name")
    p.add_argument("--experiment", default="", help="experiment label (default: git sha)")
    p.add_argument("--timeout", type=int, default=7200,
                   help="per-run ceiling in seconds (RCA runs are ~10 min)")
    p.add_argument("--dry-run", action="store_true",
                   help="run tasks + evals but do NOT record to Phoenix")
    p.add_argument("--profile", default="",
                   help="agent profile to use, e.g. 'reco' (default: the general agent)")
    a = p.parse_args(argv)

    if a.profile and get_profile(a.profile) is None:
        raise SystemExit(f"eval: unknown profile '{a.profile}' (known: reco).")
    if a.profile:
        os.environ["EVAL_PROFILE"] = a.profile

    # Eval runs emit their OWN traces into a separate project so live prod traces
    # (the rca-agent project) stay uncluttered. No-op if tracing isn't installed.
    os.environ.setdefault("RCA_TRACE", "1")
    os.environ["PHOENIX_PROJECT"] = os.getenv("EVAL_PHOENIX_PROJECT", "rca-eval")

    c = client()
    ds = c.datasets.get_dataset(dataset=a.name)
    label = a.experiment or _git_sha()

    print(f"eval: running experiment '{label}' over dataset '{a.name}' "
          f"(profile={a.profile or 'none'}, dry_run={a.dry_run}). "
          f"Each ticket takes minutes — be patient.")
    ran = c.experiments.run_experiment(
        dataset=ds,
        task=run_rca_task,
        evaluators=ALL_EVALUATORS,
        experiment_name=label,
        experiment_metadata={"git_sha": _git_sha(), "profile": a.profile or None},
        timeout=a.timeout,
        dry_run=a.dry_run,
        print_summary=True,
    )
    print(f"\neval: done. Open Phoenix ({base_url()}) -> Datasets -> '{a.name}' -> "
          f"Experiments to see scores + per-example traces.")
    return ran


def rescore_main(argv=None) -> None:
    """Re-run ONLY the evaluators over existing experiment(s) — no agent re-run.
    Use after fixing/adding an evaluator. Takes Phoenix experiment IDs (found in the
    experiment's URL, e.g. .../compare?experimentId=<ID>, or in `run`'s output)."""
    warn_no_api_key()
    p = argparse.ArgumentParser(
        description="Re-score existing experiment(s) with the current evaluators.")
    p.add_argument("experiment_ids", nargs="+", help="Phoenix experiment IDs")
    a = p.parse_args(argv)
    c = client()
    for eid in a.experiment_ids:
        exp = c.experiments.get_experiment(experiment_id=eid)
        print(f"eval: re-scoring experiment {eid} ...")
        c.experiments.evaluate_experiment(
            experiment=exp, evaluators=ALL_EVALUATORS, print_summary=True)
    print(f"\neval: done. Open Phoenix ({base_url()}) to see the updated scores.")


if __name__ == "__main__":
    main()
