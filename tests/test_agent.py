"""The LLM proposal loop, with a scripted proposer in place of the model.

No network call and no key: the proposer is a function, so these tests pin
what the loop does with a reply, not what any model replies.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiments import agent
from experiments.cases import TRIANGLE_LINKS
from tests.test_experiments import StubNet
from verifix.adapters.mininet import MininetStateProvider, link_id
from verifix.models import GateMode

BACKUP_LINKS = ["links:s1-s3", "links:s2-s3"]


def scripted(reply: dict[str, object] | str) -> agent.Proposer:
    raw = reply if isinstance(reply, str) else json.dumps(reply)
    return lambda system, user: ("scripted", raw)


def reroute(depends_on: list[str]) -> dict[str, object]:
    return {"kind": "reroute", "params": {"target_path": "backup"},
            "depends_on": depends_on, "rationale": "primary is down"}


def run_one(reply: dict[str, object] | str, drift: str,
            vocabulary: bool = True) -> agent.AgentTrial:
    net = StubNet()
    provider = MininetStateProvider(net, agent.MININET_PATHS)
    result = agent.one_trial(net, provider, scripted(reply), "t", vocabulary, drift)
    assert all(lk.intf1.up and lk.intf2.up for lk in net.links), "links not restored"
    return result


def verdicts(result: agent.AgentTrial) -> dict[GateMode, str]:
    assert result.trial is not None
    return {m: d.verdict.value for m, d in result.trial.decisions.items()}


def test_proposal_sees_the_fault() -> None:
    result = run_one(reroute(["links:s1-s2", *BACKUP_LINKS]), "none")
    assert result.seen.links[link_id("s1", "s2")] is False


@pytest.mark.parametrize("drift, revalidate", [
    ("none", "allow"), ("target_drops", "block"), ("premise_recovers", "block"),
])
def test_premise_and_target_declared(drift: str, revalidate: str) -> None:
    result = run_one(reroute(["links:s1-s2", *BACKUP_LINKS]), drift)
    v = verdicts(result)
    assert v[GateMode.CHECK_ONCE] == "allow"
    assert v[GateMode.REVALIDATE] == revalidate
    d = result.trial.decisions[GateMode.REVALIDATE]  # type: ignore[union-attr]
    assert (d.coverage.evaluated, d.coverage.total) == (4, 4)


def test_free_vocabulary_premise_only_misses_target_drop() -> None:
    # The feasibility-check reply (2026-11-19): own kind, premise only. With
    # feasibility abstaining and the target undeclared, nothing sees s1-s3.
    reply = {"kind": "set_path", "params": {"path": "backup"},
             "depends_on": ["links:s1-s2", "paths:primary"]}
    result = run_one(reply, "target_drops", vocabulary=False)
    d = result.trial.decisions[GateMode.REVALIDATE]  # type: ignore[union-attr]
    statuses = {c.name: c.status.value for c in d.checks}
    assert statuses["feasibility"] == "not_applicable"
    assert d.verdict.value == "allow"
    assert (d.coverage.evaluated, d.coverage.total) == (3, 4)

    row = next(agent.rows([result], "r", "ts"))
    assert row["declares_premise"] is True and row["declares_target"] is False


def test_unparseable_reply_is_one_row_with_the_error() -> None:
    result = run_one("not json", "none")
    assert result.trial is None and result.error
    out = list(agent.rows([result], "r", "ts"))
    assert len(out) == 1 and out[0]["raw"] == "not json"
    assert set(out[0]) == set(agent.COLUMNS)


def test_bad_depends_on_shape_is_a_parse_error() -> None:
    result = run_one({"kind": "reroute", "params": {}, "depends_on": "s1-s2"}, "none")
    assert result.trial is None and "depends_on" in result.error


def test_unresolved_keys_are_reported_not_fixed() -> None:
    result = run_one(reroute(["links:s2-s1", "links:s1-s3"]), "none")
    row = next(agent.rows([result], "r", "ts"))
    assert json.loads(str(row["unresolved_keys"])) == ["links:s2-s1"]
    assert json.loads(str(row["depends_on"])) == ["links:s2-s1", "links:s1-s3"]


def test_links_restored_when_the_proposer_raises() -> None:
    net = StubNet()
    provider = MininetStateProvider(net, agent.MININET_PATHS)

    def failing(system: str, user: str) -> tuple[str, str]:
        raise RuntimeError("api down")

    with pytest.raises(RuntimeError):
        agent.one_trial(net, provider, failing, "t", True, "none")
    assert all(lk.intf1.up and lk.intf2.up for lk in net.links)


def test_vocabulary_flag_only_changes_the_prompt() -> None:
    state = MininetStateProvider(StubNet(), agent.MININET_PATHS).live_state()
    free, vocab = agent.prompt(state, False), agent.prompt(state, True)
    assert "reroute" not in free[0] and "reroute" in vocab[0]
    assert free[1] == vocab[1]


def test_grid_is_two_by_three_with_unique_ids() -> None:
    results = list(agent.trials(StubNet(), scripted(reroute(BACKUP_LINKS)), 2, "r"))
    assert len(results) == 12
    ids = {r.trial.action.action_id for r in results}  # type: ignore[union-attr]
    assert len(ids) == 12
    rows = list(agent.rows(results, "r", "ts"))
    assert len(rows) == 12 * 3 and all(set(r) == set(agent.COLUMNS) for r in rows)


def test_main_writes_csv_with_agent_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    # main() end to end with the model and Mininet swapped out: the write
    # path is the last step of a paid run, so it must not be the first to fail.
    import csv
    import io

    net = StubNet()
    net.start = net.stop = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(agent, "build_triangle", lambda: net)
    monkeypatch.setattr(agent, "groq_proposer",
                        lambda model: scripted(reroute(BACKUP_LINKS)))
    out = io.StringIO()
    agent.main(["--trials", "1"], stdout=out)
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert len(rows) == 6 * 3
    assert list(rows[0]) == agent.COLUMNS


def test_main_appends_and_refuses_a_foreign_header(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    net = StubNet()
    net.start = net.stop = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(agent, "build_triangle", lambda: net)
    monkeypatch.setattr(agent, "groq_proposer",
                        lambda model: scripted(reroute(BACKUP_LINKS)))
    path = tmp_path / "agent.csv"
    agent.main(["--out", str(path)])
    agent.main(["--out", str(path)])
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1 + 2 * 18

    foreign = tmp_path / "cases.csv"
    foreign.write_text(",".join(agent.run.COLUMNS) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        agent.main(["--out", str(foreign)])


def test_triangle_links_cover_drifts() -> None:
    known = {link_id(a, b) for a, b in TRIANGLE_LINKS}
    for changes in agent.DRIFTS.values():
        assert all(link_id(a, b) in known for a, b, _ in changes)
