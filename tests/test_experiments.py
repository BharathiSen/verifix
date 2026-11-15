"""Cases 1-5, the CSV runner, and the demo's recorded evidence.

Cases 4-5 run against a stub with Mininet's link API, so the case logic is
tested everywhere; only the real-topology run needs the VM.
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

import pytest

from experiments import run
from experiments.cases import Trial, network_trials, static_trials
from verifix.adapters.mininet import link_id
from verifix.models import GateMode, Verdict

ROOT = Path(__file__).resolve().parent.parent
RECORDING = ROOT / "results" / "phase1-demo-2026-11-15.txt"


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
    """The triangle from cases.mininet_trials, with Mininet's link toggle."""

    def __init__(self) -> None:
        self.links = [StubLink(a, b) for a, b in (
            ("h1", "s1"), ("s1", "s2"), ("s1", "s3"), ("s3", "s2"), ("s2", "h2"))]

    def configLinkStatus(self, a: str, b: str, status: str) -> None:
        link = next(lk for lk in self.links
                    if link_id(lk.intf1.node.name, lk.intf2.node.name) == link_id(a, b))
        link.intf1.up = link.intf2.up = status == "up"


def _verdicts(trial: Trial) -> tuple[str, str, str]:
    return tuple(trial.decisions[m].verdict.value for m in run.MODES)  # type: ignore[return-value]


def _by_variant(trials: list[Trial], case: int) -> dict[str, Trial]:
    return {t.variant: t for t in trials if t.case == case}


# Expected verdicts are the ones in the Phase 1 recording, not new claims.

def test_static_cases_match_the_recording() -> None:
    trials = static_trials(1000.0)
    assert _verdicts(_by_variant(trials, 1)["whole-state"]) == ("allow", "allow", "block")

    case2 = _by_variant(trials, 2)
    for variant, verdict in (("unknown=open", "allow"), ("unknown=closed", "block")):
        d = case2[variant].decisions[GateMode.REVALIDATE]
        assert d.verdict.value == verdict
        assert (d.coverage.evaluated, d.coverage.total) == (2, 4)
        assert d.weakly_verified

    case3 = _by_variant(trials, 3)
    assert case3["whole-state"].decisions[GateMode.REVALIDATE].verdict is Verdict.BLOCK
    assert case3["declared"].decisions[GateMode.REVALIDATE].verdict is Verdict.ALLOW


def test_network_cases_match_the_recording() -> None:
    trials = network_trials(StubNet())
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
    assert labels[(5, "declared target only")] == "declared"


def _csv_rows(argv: list[str]) -> list[dict[str, str]]:
    out = io.StringIO()
    run.main(argv, stdout=out)
    return list(csv.DictReader(io.StringIO(out.getvalue())))


def test_one_row_per_trial_and_mode() -> None:
    rows = _csv_rows([])
    # Cases 1-3: 1 + 2 + 2 trials, three modes each.
    assert len(rows) == 15
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
    assert len(rows) == 30 and len({r["run_id"] for r in rows}) == 2


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

    monkeypatch.setattr(run_demo, "mininet_trials", lambda: network_trials(StubNet()))
    monkeypatch.setattr(sys, "argv", ["run_demo.py", "--mininet"])
    run_demo.main()
    printed = capsys.readouterr().out
    # Byte for byte, digests included: digest() hashes links and paths but
    # not captured_at, so the same topology gives the same digest every run.
    assert printed == RECORDING.read_text(encoding="utf-8")
