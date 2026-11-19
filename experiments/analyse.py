"""Table 2: the Mininet cases, one row per case and variant.

    python -m experiments.analyse                    # latest run in the file
    python -m experiments.analyse --run aed06729d553

Reads results/mininet-cases-wsl.csv and prints a Markdown table. Table 1 (real
agent actions) was withdrawn with NIKA, so this is the only table; ask
before adding another.

Rows are selected by `mode`, never by coverage or weakly_verified: OFF rows
carry 0/0 and weakly_verified=False because the gate returns before
computing coverage, so any filter on those fields would let them in.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import TextIO

from experiments.run import CHECK_NAMES, COLUMNS, NOT_RUN

DEFAULT_CSV = Path("results/mininet-cases-wsl.csv")
TABLE_MODES = ("check_once", "revalidate")

# "Wasn't attempted" and "abstained" must stay distinguishable in print.
STATUS_LABELS = {NOT_RUN: "not run", "not_applicable": "n/a"}

Row = dict[str, str]


def load(path: Path) -> list[Row]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != COLUMNS:
            raise SystemExit(
                f"{path}: header does not match experiments.run.COLUMNS; "
                f"refusing to analyse a schema this code did not write"
            )
        return list(reader)


def select_run(rows: list[Row], run_id: str | None) -> tuple[str, list[Row]]:
    if not rows:
        raise SystemExit("no rows to analyse")
    if run_id is None:
        # ISO-8601 UTC timestamps sort as strings.
        run_id = max(rows, key=lambda r: r["timestamp"])["run_id"]
    chosen = [r for r in rows if r["run_id"] == run_id]
    if not chosen:
        raise SystemExit(f"run {run_id!r} not found")
    return run_id, chosen


def _status(value: str) -> str:
    return STATUS_LABELS.get(value, value)


def table_rows(rows: Iterable[Row]) -> list[list[str]]:
    by_variant: dict[tuple[int, str], dict[str, Row]] = {}
    for r in rows:
        if r["mode"] in TABLE_MODES:
            by_variant.setdefault((int(r["case"]), r["variant"]), {})[r["mode"]] = r

    out: list[list[str]] = []
    for (case, variant), modes in sorted(by_variant.items(), key=lambda kv: kv[0][0]):
        missing = [m for m in TABLE_MODES if m not in modes]
        if missing:
            raise SystemExit(f"case {case} {variant!r}: no rows for {missing}")
        once, live = modes["check_once"], modes["revalidate"]
        out.append([
            str(case), variant, live["binding"], live["unknown_policy"],
            f"{once['verdict'].upper()} ({once['coverage_evaluated']}/{once['coverage_total']})",
            f"{live['verdict'].upper()} ({live['coverage_evaluated']}/{live['coverage_total']})",
            *(f"{_status(once[c])} / {_status(live[c])}" for c in CHECK_NAMES),
            f"{once['latency_ms']} / {live['latency_ms']}",
        ])
    return out


def render(run_id: str, rows: list[Row], out: TextIO) -> None:
    header = [
        "Case", "Variant", "Binding", "Unknown policy",
        "check_once (cov)", "revalidate (cov)",
        *(f"{c} (once / reval)" for c in CHECK_NAMES),
        "latency ms (once / reval)",
    ]
    adapters = sorted({r["adapter"] for r in rows})
    print(f"Table 2: Mininet cases (run {run_id}, adapters: {', '.join(adapters)})",
          file=out)
    print(file=out)
    print("| " + " | ".join(header) + " |", file=out)
    print("|" + "---|" * len(header), file=out)
    for row in table_rows(rows):
        print("| " + " | ".join(row) + " |", file=out)
    print(file=out)
    print("Coverage is evaluated/total over the checks run in that mode. "
          "'not run' means the mode never attempts the check; 'n/a' means the "
          "check ran and abstained. Latency is one decision's wall time inside "
          "Gate.verify, state read included: one sample per cell, not a "
          "distribution.", file=out)


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--run", help="run_id to tabulate; default: the latest")
    args = parser.parse_args(argv)

    run_id, rows = select_run(load(args.csv), args.run)
    render(run_id, rows, stdout)


if __name__ == "__main__":
    main()
