"""The churn injector, on the stub triangle: no Mininet, no root.

Scripted schedules pin what each scope makes of a given change; the drawn
schedules are tested only for the properties the CSV relies on (order,
window, alternation, reproducibility), never for particular draws.
"""

from __future__ import annotations

import csv
import io
import json
import random
from pathlib import Path

import pytest

from experiments import inject
from tests.test_experiments import StubNet
from verifix.adapters.mininet import MininetStateProvider
from verifix.models import GateMode, Verdict

START = {lk: lk != inject.PREMISE for lk in inject.ENDPOINTS}


def run_one(delay: float, events: list[inject.Event] | None = None,
            monkeypatch: pytest.MonkeyPatch | None = None) -> inject.InjectionTrial:
    if events is not None:
        assert monkeypatch is not None
        monkeypatch.setattr(inject, "schedule", lambda *a: list(events))
    net = StubNet()
    clock = inject.SimClock(1000.0)
    provider = MininetStateProvider(net, inject.MININET_PATHS, clock=clock)
    result = inject.one_trial(net, provider, clock, seed=1, delay=delay,
                              rate=0.0, repeat=0, id_prefix="t")
    assert all(lk.intf1.up and lk.intf2.up for lk in net.links), "links not restored"
    return result


def revalidate(result: inject.InjectionTrial) -> dict[str, tuple[str, dict[str, str]]]:
    out = {}
    for trial in result.trials:
        d = trial.decisions[GateMode.REVALIDATE]
        out[trial.variant] = (d.verdict.value,
                              {c.name: c.status.value for c in d.checks})
    return out


# --- schedule ---------------------------------------------------------------

def test_zero_rate_draws_no_events() -> None:
    assert inject.schedule(random.Random(0), 0.0, 8.0, START) == []


def test_schedule_is_ordered_inside_the_window_and_alternates() -> None:
    events = inject.schedule(random.Random(7), 0.5, 8.0, START)
    assert events, "rate 0.5 over 8 s on 6 links should draw something"
    assert [e.at for e in events] == sorted(e.at for e in events)
    assert all(0 <= e.at < 8.0 for e in events)
    up = dict(START)
    for e in events:  # each event flips its link from where it was
        up[e.link] = not up[e.link]
        assert e.status == ("up" if up[e.link] else "down")


def test_same_seed_same_schedule() -> None:
    a = inject.schedule(random.Random("s/1"), 0.2, 4.0, START)
    b = inject.schedule(random.Random("s/1"), 0.2, 4.0, START)
    assert a == b


# --- one trial, scripted churn ------------------------------------------------

def test_no_churn_inside_budget_allows_every_scope() -> None:
    result = run_one(delay=4.0)
    assert result.events == [] and result.changed == []
    assert {v for v, _ in revalidate(result).values()} == {"allow"}


def test_delay_past_budget_blocks_on_freshness_alone() -> None:
    for verdict, checks in revalidate(run_one(delay=8.0)).values():
        assert verdict == "block"
        assert checks["freshness"] == "fail"
        assert checks["state_binding"] == "pass"


def test_off_path_change_blocks_whole_state_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # Case 3 under churn: a change on neither path.
    result = run_one(1.0, [inject.Event(0.3, "h3-s3", "down")], monkeypatch)
    assert result.changed == ["h3-s3"]
    got = revalidate(result)
    assert got["whole-state"][0] == "block"
    assert got["declared target only"][0] == "allow"
    assert got["declared premise+target"][0] == "allow"


def test_premise_recovery_is_missed_by_target_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # Case 5 under churn: the silent miss.
    result = run_one(1.0, [inject.Event(0.5, "s1-s2", "up")], monkeypatch)
    got = revalidate(result)
    assert got["whole-state"][0] == "block"
    assert got["declared target only"][0] == "allow"
    assert got["declared premise+target"][0] == "block"


def test_target_drop_blocks_every_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_one(1.0, [inject.Event(0.2, "s1-s3", "down")], monkeypatch)
    for verdict, checks in revalidate(result).values():
        assert verdict == "block"
        assert checks["feasibility"] == "fail"


def test_a_flap_is_logged_but_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    flap = [inject.Event(0.1, "s1-s3", "down"), inject.Event(0.6, "s1-s3", "up")]
    result = run_one(1.0, flap, monkeypatch)
    assert result.events == flap and result.changed == []
    assert {v for v, _ in revalidate(result).values()} == {"allow"}


def test_check_once_never_sees_the_churn(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_one(1.0, [inject.Event(0.2, "s1-s3", "down")], monkeypatch)
    assert {t.decisions[GateMode.CHECK_ONCE].verdict for t in result.trials} \
        == {Verdict.ALLOW}


# --- the CSV --------------------------------------------------------------------

def _csv(argv: list[str]) -> list[dict[str, str]]:
    out = io.StringIO()
    inject.main(argv, stdout=out, net=StubNet())
    return list(csv.DictReader(io.StringIO(out.getvalue())))


def test_full_grid_one_row_per_cell_scope_and_mode() -> None:
    rows = _csv(["--seed", "3"])
    cells = len(inject.DELAYS_S) * len(inject.RATES)
    assert len(rows) == cells * len(inject.SCOPES) * 3
    assert list(rows[0]) == inject.COLUMNS
    assert {r["seed"] for r in rows} == {"3"}
    assert {r["case"] for r in rows} == {"8"}


def test_a_seed_reproduces_the_churn() -> None:
    keep = ("delay_s", "churn_rate", "events", "changed_links", "variant",
            "mode", "verdict")
    a = [[r[k] for k in keep] for r in _csv(["--seed", "11"])]
    b = [[r[k] for k in keep] for r in _csv(["--seed", "11"])]
    assert a == b


def test_ground_truth_columns_follow_the_changed_links() -> None:
    for r in _csv(["--seed", "5"]):
        changed = set(json.loads(r["changed_links"]))
        assert (r["premise_changed"] == "True") == (inject.PREMISE in changed)
        assert (r["target_changed"] == "True") == bool(changed & set(inject.TARGET))
        if r["churn_rate"] == "0.0":
            assert changed == set()


def test_append_keeps_one_header(tmp_path: Path) -> None:
    out = tmp_path / "injection.csv"
    for _ in range(2):
        inject.main(["--seed", "2", "--out", str(out)], net=StubNet())
    lines = out.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("run_id,") for line in lines) == 1
