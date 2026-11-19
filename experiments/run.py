"""Run Cases 1-5 and append one CSV row per case, variant and mode.

    python -m experiments.run                       # Cases 1-2, CSV to stdout
    sudo .venv/bin/python -m experiments.run --mininet \\
        --out results/mininet-cases-wsl.csv          # all five, under WSL2

Run from the repository root. The cases themselves live in
experiments/cases.py; this file only turns their decisions into rows.

results/mininet-cases-wsl.csv is written only under WSL2, because Cases 3-5
need Mininet; results/mininet-cases.csv holds the retired VM's runs and is
no longer written. Without --out nothing touches results/, which is how this
runs on plain Windows.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from experiments.cases import MODES, Trial, mininet_trials, static_trials
from verifix.gate import DEFAULT_CHECKS
from verifix.models import Decision

CHECK_NAMES = [check.name for check in DEFAULT_CHECKS]

#: Written for a check the mode never ran. Kept apart from not_applicable:
#: "this check abstained" and "this check wasn't attempted" are different
#: facts, and CHECK_ONCE must not look like it abstained on the live checks.
NOT_RUN = "not_run"

COLUMNS = [
    "run_id", "timestamp", "adapter", "case", "variant", "mode", "binding",
    "unknown_policy", "min_coverage", "action_id", "verdict",
    *CHECK_NAMES,
    "coverage_evaluated", "coverage_total", "weakly_verified", "latency_ms",
    "reason",
]


def check_statuses(decision: Decision) -> dict[str, str]:
    ran = {c.name: c.status.value for c in decision.checks}
    return {name: ran.get(name, NOT_RUN) for name in CHECK_NAMES}


def rows(trials: list[Trial], run_id: str, timestamp: str) -> Iterator[dict[str, object]]:
    for trial in trials:
        for mode in MODES:
            d = trial.decisions[mode]
            yield {
                "run_id": run_id,
                "timestamp": timestamp,
                "adapter": trial.adapter,
                "case": trial.case,
                "variant": trial.variant,
                "mode": mode.value,
                "binding": trial.binding,
                "unknown_policy": trial.unknown_policy.value,
                "min_coverage": trial.min_coverage,
                "action_id": d.action_id,
                "verdict": d.verdict.value,
                **check_statuses(d),
                "coverage_evaluated": d.coverage.evaluated,
                "coverage_total": d.coverage.total,
                "weakly_verified": d.weakly_verified,
                "latency_ms": f"{d.latency_ms:.3f}",
                "reason": d.reason,
            }


def existing_header(path: Path) -> list[str] | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open(newline="", encoding="utf-8") as f:
        return next(csv.reader(f), None)


def append(path: Path, records: list[dict[str, object]]) -> None:
    header = existing_header(path)
    if header is not None and header != COLUMNS:
        # The file is append-only research data. Mixing two schemas in it
        # would only be noticed while writing the paper, so refuse now.
        raise SystemExit(
            f"refusing to append to {path}: its header does not match this "
            f"runner's columns.\n  file:   {header}\n  runner: {COLUMNS}"
        )
    with path.open("a", newline="", encoding="utf-8") as f:
        write(f, records, with_header=header is None)


def write(out: TextIO, records: list[dict[str, object]], with_header: bool) -> None:
    writer = csv.DictWriter(out, fieldnames=COLUMNS)
    if with_header:
        writer.writeheader()
    writer.writerows(records)


def collect(with_mininet: bool) -> list[Trial]:
    trials = static_trials(time.time())
    if with_mininet:
        try:
            trials += mininet_trials()
        except ImportError as exc:
            # Asked for all five cases: silently returning two would make
            # the table look complete when it is not.
            raise SystemExit(f"--mininet given but Mininet is unavailable: {exc}")
    return trials


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mininet", action="store_true",
                        help="also run Cases 3-5 (Linux, root, Mininet)")
    parser.add_argument("--out", type=Path,
                        help="CSV to append to; omitted, rows go to stdout")
    args = parser.parse_args(argv)

    trials = collect(args.mininet)
    run_id = uuid.uuid4().hex[:12]
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records = list(rows(trials, run_id, timestamp))

    if args.out is None:
        write(stdout, records, with_header=True)
    else:
        append(args.out, records)
        print(f"appended {len(records)} rows to {args.out} (run {run_id})",
              file=sys.stderr)


if __name__ == "__main__":
    main()
