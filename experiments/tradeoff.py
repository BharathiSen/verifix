"""Binding-scope trade-off: false blocks against stale actions let through (Phase 9).

    python -m experiments.tradeoff                      # every run in the file
    python -m experiments.tradeoff --run 481762640ab5
    python -m experiments.tradeoff --plot               # also draws the figure

Reads results/injection-wsl.csv (written by experiments.inject) and labels
each trial from what the injector read back, never from the gate's verdict:

    stale        the premise is gone (s1-s2 came back up, Case 5) or the
                 target is infeasible (a backup link is down, Case 4).
                 An off-path change (h1-s1, s2-h2, h3-s3) is not stale.
    false block  BLOCK on an action that is not stale.
    silent miss  ALLOW on an action that is stale.

This is the user's definition (2026-11-19), not the gate's: the gate never
sees it. Every count is shown twice, below the freshness budget (binding
decides) and over all delays, where freshness blocks every scope alike on
state that may not have changed at all. Delay is simulated (see inject.py).

Counts, not rates, lead: one repeat is 25 cells, and a ratio over a handful
of stale trials says little. The figure is drawn only with --plot, from the
optional `plot` extra, so the tables need nothing beyond the standard library.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from experiments import inject
from verifix.checks.freshness import MAX_DECISION_AGE_S

DEFAULT_CSV = Path("results/injection-wsl.csv")
DEFAULT_PNG = Path("results/tradeoff-wsl.png")
MODES = ("check_once", "revalidate")
SCOPES = tuple(label for label, _ in inject.SCOPES)

Row = dict[str, str]


def load(path: Path) -> list[Row]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != inject.COLUMNS:
            raise SystemExit(
                f"{path}: header does not match experiments.inject.COLUMNS; "
                f"refusing to analyse a schema this code did not write"
            )
        return list(reader)


def select(rows: list[Row], run_ids: list[str] | None) -> list[Row]:
    # Default is every run: each draws its own seed, so runs are independent
    # repeats of the grid and pool without double counting.
    chosen = rows if not run_ids else [r for r in rows if r["run_id"] in run_ids]
    missing = set(run_ids or ()) - {r["run_id"] for r in chosen}
    if missing:
        raise SystemExit(f"runs not found: {', '.join(sorted(missing))}")
    if not chosen:
        raise SystemExit("no rows to analyse")
    return chosen


def _flag(value: str) -> bool:
    # A typo here would silently relabel trials, so anything else is fatal.
    if value not in ("True", "False"):
        raise SystemExit(f"expected True/False, got {value!r}")
    return value == "True"


def is_stale(row: Row) -> bool:
    return _flag(row["premise_changed"]) or _flag(row["target_changed"])


def below_budget(row: Row) -> bool:
    # Freshness fails on age strictly greater than the budget.
    return float(row["delay_s"]) <= MAX_DECISION_AGE_S


@dataclass
class Tally:
    trials: int = 0
    stale: int = 0
    false_blocks: int = 0
    by_freshness: int = 0  # false blocks where freshness failed
    misses: int = 0
    flagged_misses: int = 0  # misses the gate at least marked weakly verified

    def add(self, row: Row) -> None:
        if row["verdict"] not in ("allow", "block"):
            raise SystemExit(f"unknown verdict {row['verdict']!r}")
        stale, blocked = is_stale(row), row["verdict"] == "block"
        self.trials += 1
        self.stale += stale
        if blocked and not stale:
            self.false_blocks += 1
            self.by_freshness += row["freshness"] == "fail"
        if not blocked and stale:
            self.misses += 1
            self.flagged_misses += _flag(row["weakly_verified"])

    @property
    def sound(self) -> int:
        return self.trials - self.stale

    @property
    def false_block_rate(self) -> float | None:
        return self.false_blocks / self.sound if self.sound else None

    @property
    def miss_rate(self) -> float | None:
        return self.misses / self.stale if self.stale else None


Key = tuple[object, ...]


def tally(rows: Iterable[Row], key: Callable[[Row], Key]) -> dict[Key, Tally]:
    out: dict[Key, Tally] = {}
    for r in rows:
        out.setdefault(key(r), Tally()).add(r)
    return out


def _frac(n: int, d: int) -> str:
    return f"{n}/{d}" if d else "-"


def _table(header: list[str], body: list[list[str]], out: TextIO) -> None:
    print("| " + " | ".join(header) + " |", file=out)
    print("|" + "---|" * len(header), file=out)
    for line in body:
        print("| " + " | ".join(line) + " |", file=out)
    print(file=out)


def summary(rows: list[Row], out: TextIO) -> None:
    windows = (
        (f"delay <= {MAX_DECISION_AGE_S:g} s (binding decides)",
         [r for r in rows if below_budget(r)]),
        ("all delays (freshness included)", rows),
    )
    for title, subset in windows:
        print(f"**{title}**", file=out)
        print(file=out)
        t = tally((r for r in subset if r["mode"] in MODES),
                  lambda r: (r["mode"], r["variant"]))
        body = []
        for mode in MODES:
            for scope in SCOPES:
                x = t.get((mode, scope), Tally())
                body.append([
                    mode, scope, str(x.trials), str(x.stale),
                    _frac(x.false_blocks, x.sound), str(x.by_freshness),
                    _frac(x.misses, x.stale), str(x.flagged_misses),
                ])
        _table(["Mode", "Scope", "Trials", "Stale", "False blocks (of sound)",
                "of which freshness", "Silent misses (of stale)",
                "of which flagged"], body, out)


def breakdown(rows: list[Row], field: str, title: str, out: TextIO) -> None:
    live = [r for r in rows if r["mode"] == "revalidate"]
    t = tally(live, lambda r: (float(r[field]), r["variant"]))
    print(f"**{title}** (revalidate; cells are false blocks of sound / "
          f"misses of stale)", file=out)
    print(file=out)
    body = []
    for value in sorted({k[0] for k in t}):
        cells = []
        for scope in SCOPES:
            x = t.get((value, scope), Tally())
            cells.append(f"{_frac(x.false_blocks, x.sound)} / "
                         f"{_frac(x.misses, x.stale)}")
        body.append([f"{value:g}", *cells])
    _table([field, *SCOPES], body, out)


def render(rows: list[Row], out: TextIO) -> None:
    runs = sorted({r["run_id"] for r in rows})
    cells = {(r["run_id"], r["repeat"], r["delay_s"], r["churn_rate"]) for r in rows}
    print(f"Binding-scope trade-off (runs: {', '.join(runs)}; "
          f"{len(cells)} injected cells)", file=out)
    print(file=out)
    summary(rows, out)
    breakdown([r for r in rows if below_budget(r)], "churn_rate",
              f"By churn rate, delay <= {MAX_DECISION_AGE_S:g} s", out)
    breakdown(rows, "delay_s", "By delay, all churn rates", out)
    print("Stale = premise gone or target infeasible, read back from the links "
          "after the delay; off-path changes are not stale. Flagged = allowed "
          "but marked weakly verified. CHECK_ONCE never reads live state, so "
          "it cannot catch a change. Delay is simulated.", file=out)


def curves(rows: list[Row]) -> dict[str, dict[str, list[tuple[float, float | None]]]]:
    """Per scope: (churn rate, false-block rate) and (churn rate, miss rate)."""
    live = [r for r in rows if r["mode"] == "revalidate" and below_budget(r)]
    t = tally(live, lambda r: (r["variant"], float(r["churn_rate"])))
    out: dict[str, dict[str, list[tuple[float, float | None]]]] = {}
    for scope in SCOPES:
        rates = sorted(k[1] for k in t if k[0] == scope)
        out[scope] = {
            "false_block": [(x, t[(scope, x)].false_block_rate) for x in rates],
            "miss": [(x, t[(scope, x)].miss_rate) for x in rates],
        }
    return out


def plot(rows: list[Row], path: Path) -> None:
    try:
        import matplotlib
    except ImportError:
        raise SystemExit("--plot needs matplotlib (the `plot` extra)")
    matplotlib.use("Agg")  # files only; never open a window
    import matplotlib.pyplot as plt

    data = curves(rows)
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.6), sharex=True)
    panels = (("false_block", "False blocks / sound actions"),
              ("miss", "Silent misses / stale actions"))
    # Scopes often coincide (all at 0), so each needs its own marker and
    # dash to stay visible where the lines overlap.
    styles = (("o", "-"), ("s", "--"), ("^", ":"))
    for ax, (key, ylabel) in zip(axes, panels):
        for scope, (marker, dash) in zip(SCOPES, styles):
            # An undefined rate (no sound or no stale trial in the cell) is
            # left as a gap, not drawn as zero.
            xs = [x for x, _ in data[scope][key]]
            ys = [float("nan") if y is None else y for _, y in data[scope][key]]
            ax.plot(xs, ys, marker=marker, linestyle=dash, fillstyle="none",
                    markersize=8, label=scope)
        ax.set_xlabel("churn rate (toggles per link per s)")
        ax.set_ylabel(ylabel)
        ax.set_ylim(-0.05, 1.05)
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=8)
    runs = sorted({r["run_id"] for r in rows})
    fig.suptitle(f"REVALIDATE, delay <= {MAX_DECISION_AGE_S:g} s "
                 f"(runs {', '.join(runs)})", fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--run", action="append",
                        help="run_id to include (repeatable); default: all")
    parser.add_argument("--plot", type=Path, nargs="?", const=DEFAULT_PNG,
                        help=f"write the figure (default path {DEFAULT_PNG})")
    args = parser.parse_args(argv)

    rows = select(load(args.csv), args.run)
    render(rows, stdout)
    if args.plot is not None:
        plot(rows, args.plot)
        print(f"wrote {args.plot}", file=sys.stderr)


if __name__ == "__main__":
    main()
