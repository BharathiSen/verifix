"""The Phase 9 labels and tallies, on rows the injector writes for the stub.

Scripted churn pins one cell at a time to a known stale/sound label, so each
test says which side of the trade-off a scope lands on. Rows go through the
real CSV writer and reader, since the labels are read back from strings.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from experiments import inject, run, tradeoff
from tests.test_experiments import StubNet
from tests.test_inject import run_one

Row = dict[str, str]


def as_rows(*results: inject.InjectionTrial) -> list[Row]:
    out = io.StringIO()
    records = list(inject.rows(list(results), seed=1, run_id="r1",
                               timestamp="2026-11-19T00:00:00+00:00"))
    run.write(out, records, with_header=True, columns=inject.COLUMNS)
    return list(csv.DictReader(io.StringIO(out.getvalue())))


def tallies(rows: list[Row]) -> dict[str, tradeoff.Tally]:
    t = tradeoff.tally((r for r in rows if r["mode"] == "revalidate"),
                       lambda r: (r["variant"],))
    return {k[0]: v for k, v in t.items()}


def churn(monkeypatch: pytest.MonkeyPatch, link: str, status: str,
          delay: float = 1.0) -> list[Row]:
    return as_rows(run_one(delay, [inject.Event(0.3, link, status)], monkeypatch))


# --- labels -------------------------------------------------------------------

@pytest.mark.parametrize("premise,target,other,stale", [
    ("False", "False", "False", False),
    ("False", "False", "True", False),   # off-path: Case 3, not stale
    ("True", "False", "False", True),    # premise gone: Case 5
    ("False", "True", "False", True),    # target infeasible: Case 4
])
def test_stale_is_premise_or_target(premise: str, target: str, other: str,
                                    stale: bool) -> None:
    row = {"premise_changed": premise, "target_changed": target,
           "other_changed": other}
    assert tradeoff.is_stale(row) is stale


def test_a_malformed_flag_is_fatal() -> None:
    with pytest.raises(SystemExit):
        tradeoff.is_stale({"premise_changed": "true", "target_changed": "False"})


# --- one scripted cell per side of the trade-off --------------------------------

def test_off_path_change_is_a_false_block_for_whole_state_only(
        monkeypatch: pytest.MonkeyPatch) -> None:
    t = tallies(churn(monkeypatch, "h3-s3", "down"))
    assert t["whole-state"].false_blocks == 1
    assert t["declared target only"].false_blocks == 0
    assert t["declared premise+target"].false_blocks == 0
    assert all(x.stale == 0 and x.misses == 0 for x in t.values())


def test_premise_recovery_is_a_silent_miss_for_target_only(
        monkeypatch: pytest.MonkeyPatch) -> None:
    t = tallies(churn(monkeypatch, "s1-s2", "up"))
    assert all(x.stale == 1 for x in t.values())
    assert t["declared target only"].misses == 1
    assert t["declared target only"].flagged_misses == 0  # silent, not flagged
    assert t["whole-state"].misses == 0
    assert t["declared premise+target"].misses == 0


def test_target_drop_is_caught_by_every_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    t = tallies(churn(monkeypatch, "s1-s3", "down"))
    assert all(x.stale == 1 and x.misses == 0 and x.false_blocks == 0
               for x in t.values())


def test_check_once_misses_every_stale_action(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = churn(monkeypatch, "s1-s3", "down")
    once = tradeoff.tally((r for r in rows if r["mode"] == "check_once"),
                          lambda r: (r["variant"],))
    assert all(x.misses == 1 for x in once.values())


def test_freshness_false_blocks_fall_outside_the_budget_window() -> None:
    rows = as_rows(run_one(delay=8.0))  # nothing changed, aged out
    t = tallies(rows)
    assert all(x.false_blocks == 1 and x.by_freshness == 1 for x in t.values())
    assert not any(tradeoff.below_budget(r) for r in rows)


def test_rates_are_undefined_without_a_denominator(
        monkeypatch: pytest.MonkeyPatch) -> None:
    t = tallies(churn(monkeypatch, "h3-s3", "down"))["whole-state"]
    assert t.false_block_rate == 1.0
    assert t.miss_rate is None  # no stale trial: no rate, not zero


def test_an_unknown_verdict_is_fatal() -> None:
    with pytest.raises(SystemExit):
        tradeoff.Tally().add({"verdict": "maybe"})


# --- the CSV and the report ------------------------------------------------------

def grid_csv(tmp_path: Path, seed: str = "4") -> Path:
    path = tmp_path / "injection.csv"
    inject.main(["--seed", seed, "--out", str(path)], net=StubNet())
    return path


def test_load_refuses_a_foreign_header(tmp_path: Path) -> None:
    path = tmp_path / "other.csv"
    path.write_text(",".join(run.COLUMNS) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        tradeoff.load(path)


def test_select_pools_runs_and_rejects_unknown_ids(tmp_path: Path) -> None:
    path = grid_csv(tmp_path, "4")
    inject.main(["--seed", "9", "--out", str(path)], net=StubNet())
    rows = tradeoff.load(path)
    assert len({r["run_id"] for r in tradeoff.select(rows, None)}) == 2
    one = rows[0]["run_id"]
    assert {r["run_id"] for r in tradeoff.select(rows, [one])} == {one}
    with pytest.raises(SystemExit):
        tradeoff.select(rows, ["nope"])


def test_counts_add_up_to_the_verdicts(tmp_path: Path) -> None:
    rows = tradeoff.load(grid_csv(tmp_path))
    live = [r for r in rows if r["mode"] == "revalidate"]
    for scope, x in tallies(rows).items():
        mine = [r for r in live if r["variant"] == scope]
        blocks = sum(r["verdict"] == "block" for r in mine)
        caught = x.stale - x.misses
        assert blocks == x.false_blocks + caught
        assert x.trials == len(mine) == len(inject.DELAYS_S) * len(inject.RATES)


def test_main_prints_both_windows(tmp_path: Path) -> None:
    out = io.StringIO()
    tradeoff.main(["--csv", str(grid_csv(tmp_path))], stdout=out)
    text = out.getvalue()
    assert "delay <= 5 s (binding decides)" in text
    assert "all delays (freshness included)" in text
    assert "By churn rate" in text and "By delay" in text


def test_plot_writes_a_png(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    png = tmp_path / "tradeoff.png"
    tradeoff.main(["--csv", str(grid_csv(tmp_path)), "--plot", str(png)],
                  stdout=io.StringIO())
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
