"""Two-minute demo.

Case 1: a known action, three modes, three outcomes.
Case 2: an action kind VeriFix has never seen - it degrades, and says so.
Case 3: an unrelated link drops - whole-state binding blocks, declared does not.
Case 4 (--mininet): Case 1 on a real Mininet topology. Linux, root, Mininet.
Case 5 (--mininet): the primary recovers before execution. The backup is still
        feasible, so only state binding can block, and only if the binding
        covers the premise the agent acted on.

    python demo/run_demo.py
    sudo python demo/run_demo.py --mininet

The cases are defined in experiments/cases.py. This file only prints them,
and it prints a subset on purpose: results/phase1-demo-2026-11-15.txt is
Phase 1's recorded evidence, and the demo must keep reproducing it.
"""

from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")

from experiments.cases import (
    MODES,
    Trial,
    case_binding_scope,
    case_known,
    case_unknown,
    mininet_trials,
)
from verifix.models import GateMode


def banner(title: str) -> None:
    print("=" * 72)
    print(title)
    print("=" * 72)


def show_modes(trial: Trial) -> None:
    for mode in MODES:
        d = trial.decisions[mode]
        print(f"  {mode.value:<12} {d.verdict.value.upper():<5} "
              f"cov {d.coverage.evaluated}/{d.coverage.total}  {d.reason}")


def print_known(trials: list[Trial]) -> None:
    banner("CASE 1  known action: reroute onto 'backup', which dies before execution")
    show_modes(trials[0])


def print_unknown(trials: list[Trial]) -> None:
    banner("CASE 2  unknown environment: opaque state, unrecognised action kind")
    for trial in trials:
        d = trial.decisions[GateMode.REVALIDATE]
        print(f"  unknown={trial.unknown_policy.value:<7} "
              f"{d.verdict.value.upper():<5} "
              f"cov {d.coverage.evaluated}/{d.coverage.total}  "
              f"weak={d.weakly_verified}")
        print(f"               {d.reason}")


def print_binding_scope(trials: list[Trial]) -> None:
    banner("CASE 3  unrelated link drops: whole-state vs declared binding")
    for trial in trials:
        d = trial.decisions[GateMode.REVALIDATE]
        print(f"  {trial.variant:<12} {d.verdict.value.upper():<5} {d.reason}")


def print_target_drops(trials: list[Trial]) -> None:
    banner("CASE 4  Mininet: s1-s3 dropped between proposal and execution")
    for trial in trials:
        print(f" {trial.variant} binding")
        show_modes(trial)


def print_premise_recovers(trials: list[Trial]) -> None:
    banner("CASE 5  Mininet: primary s1-s2 recovers before the reroute runs")
    for trial in trials:
        once = trial.decisions[GateMode.CHECK_ONCE]
        live = trial.decisions[GateMode.REVALIDATE]
        status = {c.name: c.status.value for c in live.checks}
        print(f"  {trial.variant:<24} check_once {once.verdict.value.upper():<5} "
              f"revalidate {live.verdict.value.upper():<5} "
              f"feasibility={status['feasibility']} "
              f"state_binding={status['state_binding']}")


def print_mininet(trials: list[Trial]) -> None:
    print_target_drops([t for t in trials if t.case == 4])
    print()
    print_premise_recovers([t for t in trials if t.case == 5])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mininet", action="store_true",
                        help="also run Cases 4 and 5 on a real Mininet topology")
    args = parser.parse_args()

    now = time.time()
    for build, show in ((case_known, print_known),
                        (case_unknown, print_unknown),
                        (case_binding_scope, print_binding_scope)):
        show(build(now))
        print()
    if args.mininet:
        print_mininet(mininet_trials())
        print()
    print("The gate never guesses and never crashes. It reports what it could")
    print("actually check, so a thin result is visible instead of looking like a pass.")


if __name__ == "__main__":
    main()
