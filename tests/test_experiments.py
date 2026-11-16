"""Cases 1-5, the CSV runner, and the demo's recorded evidence.

Cases 3-5 run against a stub with Mininet's link API, so the case logic is
tested everywhere; only the real-topology run needs the VM.
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

import pytest

from experiments import run
from experiments.cases import TRIANGLE_LINKS, Trial, network_trials, static_trials
from verifix.adapters.mininet import link_id
from verifix.models import GateMode, Verdict

ROOT = Path(__file__).resolve().parent.parent
# Recorded on the VM with `demo/run_demo.py --mininet`. Supersedes
# phase1-demo-2026-11-15.txt, which predates h3 and the Mininet Case 3.
RECORDING = ROOT / "results" / "phase4-demo-2026-11-16.txt"


class StubIntf:
    def __init__(self, node: str) -> None:
        self.node = type("Node", (), {"name": node})()
        self.up = True

    def isUp(self) -> bool:
        return self.up


class StubLink:
    def __init__(self, a: str, b: str) -> None:
        self.intf1, self.intf2 = StubIntf(a), StubIntf(b)


class StubNet:
    """The topology from cases.build_triangle, with Mininet's link toggle."""

    def __init__(self) -> None:
        self.links = [StubLink(a, b) for a, b in TRIANGLE_LINKS]

    def configLinkStatus(self, a: str, b: str, status: str) -> None:
        link = next(lk for lk in self.links
                    if link_id(lk.intf1.node.name, lk.intf2.node.name) == link_id(a, b))
        link.intf1.up = link.intf2.up = status == "up"


def _verdicts(trial: Trial) -> tuple[str, str, str]:
    return tuple(trial.decisions[m].verdict.value for m in run.MODES)  # type: ignore[return-value]


def _by_variant(trials: list[Trial], case: int) -> dict[str, Trial]:
    return {t.variant: t for t in trials if t.case == case}


# Expected verdicts for Cases 1, 2, 4 and 5 are the ones in the Phase 1
# recording. Case 3's are the static Case 3's, now asserted on the topology.

def test_static_cases_match_the_recording() -> None:
    trials = static_trials(1000.0)
    assert _verdicts(_by_variant(trials, 1)["whole-state"]) == ("allow", "allow", "block")

    case2 = _by_variant(trials, 2)
    for variant, verdict in (("unknown=open", "allow"), ("unknown=closed", "block")):
        d = case2[variant].decisions[GateMode.REVALIDATE]
        assert d.verdict.value == verdict
        assert (d.coverage.evaluated, d.coverage.total) == (2, 4)
        assert d.weakly_verified

    assert {t.case for t in trials} == {1, 2}


def test_network_cases_match_the_recording() -> None:
    trials = network_trials(StubNet())
    assert {t.adapter for t in trials} == {"mininet"}

    case3 = _by_variant(trials, 3)
    for variant, verdict, binding in (("whole-state", "block", "fail"),
                                      ("declared", "allow", "pass")):
        live = case3[variant].decisions[GateMode.REVALIDATE]
        status = {c.name: c.status.value for c in live.checks}
        assert case3[variant].decisions[GateMode.CHECK_ONCE].verdict is Verdict.ALLOW
        assert (live.verdict.value, status["state_binding"]) == (verdict, binding)
        # The false block is state binding's alone: the reroute stays feasible.
        assert status["feasibility"] == "pass"

    for trial in _by_variant(trials, 4).values():
        assert _verdicts(trial) == ("allow", "allow", "block")

    expected = {
        "whole-state": ("block", "fail"),
        "declared target only": ("allow", "pass"),
        "declared premise+target": ("block", "fail"),
    }
    for variant, trial in _by_variant(trials, 5).items():
        live = trial.decisions[GateMode.REVALIDATE]
        status = {c.name: c.status.value for c in live.checks}
        assert trial.decisions[GateMode.CHECK_ONCE].verdict is Verdict.ALLOW
        assert (live.verdict.value, status["state_binding"]) == expected[variant]
        assert status["feasibility"] == "pass"


def test_binding_label_follows_the_action() -> None:
    trials = static_trials(1000.0) + network_trials(StubNet())
    labels = {(t.case, t.variant): t.binding for t in trials}
    assert labels[(2, "unknown=open")] == "none"
    assert labels[(3, "whole-state")] == "whole"
    assert labels[(3, "declared")] == "declared"
    assert labels[(5, "declared target only")] == "declared"


def _csv_rows(argv: list[str]) -> list[dict[str, str]]:
    out = io.StringIO()
    run.main(argv, stdout=out)
    return list(csv.DictReader(io.StringIO(out.getvalue())))


def test_one_row_per_trial_and_mode() -> None:
    rows = _csv_rows([])
    # Cases 1-2: 1 + 2 trials, three modes each.
    assert len(rows) == 9
    assert list(rows[0]) == run.COLUMNS
    assert {r["adapter"] for r in rows} == {"static"}


def test_unrun_checks_are_not_run_never_not_applicable() -> None:
    rows = _csv_rows([])
    for r in rows:
        if r["mode"] == "off":
            assert all(r[name] == run.NOT_RUN for name in run.CHECK_NAMES)
        if r["mode"] == "check_once":
            # Freshness and state binding need live state; CHECK_ONCE
            # never attempts them, which is not the same as abstaining.
            assert r["freshness"] == r["state_binding"] == run.NOT_RUN
        if r["mode"] == "revalidate":
            assert run.NOT_RUN not in (r[name] for name in run.CHECK_NAMES)


def test_off_rows_are_identified_by_mode_not_coverage() -> None:
    # The Phase 4 hazard: OFF rows carry weakly_verified=False and 0/0, so
    # any filter on those fields would mix them in with verified rows.
    off = [r for r in _csv_rows([]) if r["mode"] == "off"]
    assert off and all(r["weakly_verified"] == "False" for r in off)
    assert all(r["coverage_total"] == "0" for r in off)


def test_append_writes_one_header_across_runs(tmp_path: Path) -> None:
    out = tmp_path / "cases.csv"
    run.main(["--out", str(out)])
    run.main(["--out", str(out)])
    lines = out.read_text(encoding="utf-8").splitlines()
    assert sum(1 for line in lines if line.startswith("run_id,")) == 1
    rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8"))))
    assert len(rows) == 18 and len({r["run_id"] for r in rows}) == 2


def test_refuses_to_append_under_a_different_header(tmp_path: Path) -> None:
    out = tmp_path / "cases.csv"
    out.write_text("run_id,verdict\nx,allow\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="header does not match"):
        run.main(["--out", str(out)])
    assert out.read_text(encoding="utf-8") == "run_id,verdict\nx,allow\n"


def test_mininet_flag_without_mininet_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "mininet", None)  # makes the import raise
    monkeypatch.setitem(sys.modules, "mininet.net", None)
    with pytest.raises(SystemExit, match="Mininet is unavailable"):
        run.main(["--mininet"], stdout=io.StringIO())


def test_demo_still_reproduces_the_recorded_evidence(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from demo import run_demo

    if not RECORDING.exists():
        # Only the VM can make this file, and a recording generated here from
        # the stub would be evidence of nothing. Skipped visibly until then.
        pytest.skip(f"{RECORDING.name} not yet recorded on the VM")
    monkeypatch.setattr(run_demo, "mininet_trials", lambda: network_trials(StubNet()))
    monkeypatch.setattr(sys, "argv", ["run_demo.py", "--mininet"])
    run_demo.main()
    printed = capsys.readouterr().out
    # Byte for byte, digests included: digest() hashes links and paths but
    # not captured_at, so the same topology gives the same digest every run.
    assert printed == RECORDING.read_text(encoding="utf-8")


# analyse.py: Table 2

def _table(tmp_path: Path, argv: list[str] | None = None) -> str:
    from experiments import analyse

    out = io.StringIO()
    analyse.main(["--csv", str(tmp_path / "cases.csv"), *(argv or [])], stdout=out)
    return out.getvalue()


def _body(table: str) -> list[list[str]]:
    lines = [ln for ln in table.splitlines() if ln.startswith("| ")]
    return [[cell.strip() for cell in ln.strip("|").split("|")] for ln in lines[1:]]


def test_table_has_one_row_per_case_and_variant(tmp_path: Path) -> None:
    run.main(["--out", str(tmp_path / "cases.csv")])
    body = _body(_table(tmp_path))
    assert [(r[0], r[1]) for r in body] == [
        ("1", "whole-state"), ("2", "unknown=open"), ("2", "unknown=closed"),
    ]


def test_table_keeps_not_run_apart_from_abstained(tmp_path: Path) -> None:
    run.main(["--out", str(tmp_path / "cases.csv")])
    case2_open = _body(_table(tmp_path))[1]
    feasibility, freshness = case2_open[7], case2_open[8]
    assert feasibility == "n/a / n/a"        # ran in both modes, abstained
    assert freshness == "not run / pass"     # never attempted in check_once


def test_table_reports_latency_per_mode(tmp_path: Path) -> None:
    out = tmp_path / "cases.csv"
    run.main(["--out", str(out)])
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    once = next(r for r in rows if r["case"] == "1" and r["mode"] == "check_once")
    live = next(r for r in rows if r["case"] == "1" and r["mode"] == "revalidate")
    assert _body(_table(tmp_path))[0][-1] == f"{once['latency_ms']} / {live['latency_ms']}"


def test_table_selects_on_mode_so_off_rows_never_appear(tmp_path: Path) -> None:
    run.main(["--out", str(tmp_path / "cases.csv")])
    table = _table(tmp_path)
    # OFF's 0/0 coverage would show up in a verdict cell had it leaked in.
    assert "(0/0)" not in table


def test_table_defaults_to_the_latest_run(tmp_path: Path) -> None:
    out = tmp_path / "cases.csv"
    run.main(["--out", str(out)])
    first = next(csv.DictReader(out.open(encoding="utf-8")))["run_id"]
    text = out.read_text(encoding="utf-8")
    # A second, later run: same rows, new id and timestamp.
    rows = list(csv.DictReader(io.StringIO(text)))
    for r in rows:
        r["run_id"], r["timestamp"] = "laterrun", "2999-01-01T00:00:00+00:00"
    with out.open("a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=run.COLUMNS).writerows(rows)
    assert "run laterrun" in _table(tmp_path)
    assert f"run {first}" in _table(tmp_path, ["--run", first])


def test_analyse_refuses_a_foreign_schema(tmp_path: Path) -> None:
    (tmp_path / "cases.csv").write_text("run_id,verdict\nx,allow\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="header does not match"):
        _table(tmp_path)
