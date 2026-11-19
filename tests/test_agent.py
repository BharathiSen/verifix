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


def test_malformed_key_reaches_the_gate_flagged_not_a_parse_error() -> None:
    result = run_one(reroute(["link:s1-s3", "links:s1-s2"]), "none")
    assert result.trial is not None and not result.error
    d = result.trial.decisions[GateMode.REVALIDATE]
    assert d.weakly_verified and "state_binding" in d.coverage.errored
    row = next(agent.rows([result], "r", "ts"))
    assert json.loads(str(row["unresolved_keys"])) == ["link:s1-s3"]


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


# --- Phase 10: read tools, inferred dependency set -------------------------

def reading(read: list[tuple[str, str | None]],
            reply: dict[str, object] | str) -> agent.ToolProposer:
    """Reads the given (tool, id) pairs through the executor, then submits."""
    raw = reply if isinstance(reply, str) else json.dumps(reply)

    def propose(system: str, user: str,
                execute: agent.Executor) -> tuple[str, str]:
        for tool, ident in read:
            execute(tool, {} if ident is None else {"id": ident})
        return "scripted", raw

    return propose


READ_ALL = [("get_link", "s1-s2"), ("get_path", "backup"),
            ("get_link", "s1-s3"), ("get_link", "s2-s3")]


def run_reads(propose: agent.ToolProposer, drift: str,
              vocabulary: bool = False) -> agent.AgentTrial:
    net = StubNet()
    provider = MininetStateProvider(net, agent.MININET_PATHS)
    result = agent.one_trial_reads(net, provider, propose, "t", vocabulary, drift)
    assert all(lk.intf1.up and lk.intf2.up for lk in net.links), "links not restored"
    return result


def revalidate(trial: object) -> tuple[str, str]:
    d = trial.decisions[GateMode.REVALIDATE]  # type: ignore[attr-defined]
    binding = {c.name: c.status.value for c in d.checks}["state_binding"]
    return d.verdict.value, binding


def fresh_state() -> object:
    return MininetStateProvider(StubNet(), agent.MININET_PATHS).live_state()


def test_reader_serves_the_snapshot_and_logs_reads_once_in_order() -> None:
    seen = fresh_state()
    seen = seen.model_copy(update={"links": {**seen.links, "s1-s2": False}})  # type: ignore[attr-defined]
    reader = agent.StateReader(seen)
    assert json.loads(reader("get_link", {"id": "s1-s2"})) == {"id": "s1-s2", "up": False}
    assert json.loads(reader("get_path", {"id": "backup"}))["links"] == ["s1-s3", "s2-s3"]
    reader("get_link", {"id": "s1-s2"})
    assert reader.reads == ["links:s1-s2", "paths:backup"]


def test_listings_errors_and_unknown_tools_are_not_reads() -> None:
    reader = agent.StateReader(fresh_state())  # type: ignore[arg-type]
    assert "s1-s2" in json.loads(reader("list_links", {}))
    assert json.loads(reader("list_paths", {})) == sorted(agent.MININET_PATHS)
    assert "error" in json.loads(reader("get_link", {}))
    assert "error" in json.loads(reader("get_link", {"id": ""}))
    assert "error" in json.loads(reader("set_link", {"id": "s1-s2"}))
    assert reader.reads == []


def test_reading_an_absent_id_is_a_read() -> None:
    reader = agent.StateReader(fresh_state())  # type: ignore[arg-type]
    assert json.loads(reader("get_link", {"id": "s9-s9"})) == {"id": "s9-s9", "exists": False}
    assert reader.reads == ["links:s9-s9"]


def test_inferred_catches_target_drop_the_declaration_missed() -> None:
    # Phase 7's pattern: own kind (feasibility abstains), premise declared,
    # target not. The model read the target's links, so inferred binds them.
    reply = {"kind": "set_path", "params": {"path": "backup"},
             "depends_on": ["links:s1-s2"]}
    result = run_reads(reading(READ_ALL, reply), "target_drops")
    assert result.reads == ("links:s1-s2", "paths:backup",
                            "links:s1-s3", "links:s2-s3")
    assert revalidate(result.trial) == ("allow", "pass")
    assert revalidate(result.inferred) == ("block", "fail")
    assert revalidate(result.whole) == ("block", "fail")


def test_inferred_catches_premise_recovery_the_declaration_missed() -> None:
    # Case 5's pattern: target declared, premise read but not declared.
    reply = {"kind": "set_path", "params": {"path": "backup"},
             "depends_on": BACKUP_LINKS}
    result = run_reads(reading(READ_ALL, reply), "premise_recovers")
    assert revalidate(result.trial) == ("allow", "pass")
    assert revalidate(result.inferred) == ("block", "fail")


def test_unread_key_is_not_inferred() -> None:
    # Decided without reading the target: inferred misses what declared caught.
    reply = {"kind": "set_path", "params": {"path": "backup"},
             "depends_on": ["links:s1-s2", *BACKUP_LINKS]}
    result = run_reads(reading([("get_link", "s1-s2")], reply), "target_drops")
    assert revalidate(result.trial) == ("block", "fail")
    assert revalidate(result.inferred) == ("allow", "pass")


def test_no_reads_infers_an_empty_set_and_binding_abstains() -> None:
    result = run_reads(reading([], reroute(BACKUP_LINKS)), "none")
    assert result.reads == ()
    assert result.inferred is not None
    assert result.inferred.action.depends_on == []
    assert revalidate(result.inferred)[1] == "not_applicable"


def test_all_three_bindings_share_one_action_and_snapshot() -> None:
    result = run_reads(reading(READ_ALL, reroute(BACKUP_LINKS)), "none", True)
    judged = [result.trial, result.inferred, result.whole]
    actions = [t.action for t in judged]  # type: ignore[union-attr]
    assert {a.action_id for a in actions} == {"t"}
    assert {(a.kind, json.dumps(a.params)) for a in actions} == {
        ("reroute", '{"target_path": "backup"}')}
    assert [t.binding for t in judged] == ["declared", "declared", "whole"]  # type: ignore[union-attr]
    for t in judged:
        assert revalidate(t) == ("allow", "pass")
    assert result.deliberation_s is not None and result.deliberation_s >= 0


def test_no_submit_and_bad_submit_are_one_row_each() -> None:
    def never(system: str, user: str, execute: agent.Executor) -> tuple[str, str]:
        execute("get_link", {"id": "s1-s2"})
        raise agent.NoSubmit("no submit within 12 turns")

    stuck = run_reads(never, "none")
    bad = run_reads(reading([("get_link", "s1-s2")], "not json"), "none")
    out = list(agent.read_rows([stuck, bad], "r", "ts"))
    assert len(out) == 2
    assert all(set(r) == set(agent.READ_COLUMNS) and r["case"] == 10 for r in out)
    assert "no submit" in str(out[0]["parse_error"])
    assert json.loads(str(out[0]["reads"])) == ["links:s1-s2"]
    assert out[1]["raw"] == "not json" and out[1]["parse_error"]


def test_read_rows_are_three_bindings_by_three_modes() -> None:
    reply = {"kind": "set_path", "params": {"path": "backup"},
             "depends_on": ["links:s1-s2"]}
    result = run_reads(reading(READ_ALL, reply), "target_drops")
    out = list(agent.read_rows([result], "r", "ts"))
    assert len(out) == 9 and all(set(r) == set(agent.READ_COLUMNS) for r in out)
    assert sorted({str(r["binding_source"]) for r in out}) == [
        "declared", "inferred", "whole"]
    # depends_on is the declaration on every row, whichever list was bound.
    assert {str(r["depends_on"]) for r in out} == {'["links:s1-s2"]'}
    assert all(r["declares_premise"] is True and r["declares_target"] is False
               for r in out)
    assert all(r["reads_premise"] is True and r["reads_target"] is True
               for r in out)
    assert {r["binding"] for r in out if r["binding_source"] == "whole"} == {"whole"}


class _Fn:
    def __init__(self, name: str, arguments: str) -> None:
        self.name, self.arguments = name, arguments


class _Call:
    def __init__(self, cid: str, name: str, arguments: str) -> None:
        self.id, self.function = cid, _Fn(name, arguments)


class _Msg:
    def __init__(self, calls: list[_Call], content: str) -> None:
        self.tool_calls, self.content = calls, content


class _Choice:
    def __init__(self, message: _Msg) -> None:
        self.message = message


class _Resp:
    """Just the fields of an OpenAI-shaped completion that tool_loop reads."""

    def __init__(self, calls: list[_Call], content: str = "") -> None:
        self.choices = [_Choice(_Msg(calls, content))]
        self.model = "fake-model"


def test_tool_loop_feeds_results_back_and_stops_at_submit() -> None:
    script = [
        _Resp([_Call("a", "get_link", '{"id": "s1-s2"}'),
               _Call("b", "get_link", "not json")]),
        _Resp([_Call("c", "submit", '{"kind": "none"}'),
               _Call("d", "get_link", '{"id": "s1-s3"}')]),
    ]
    sent: list[list[dict[str, object]]] = []

    def create(messages: list[dict[str, object]]) -> _Resp:
        sent.append([dict(m) for m in messages])
        return script[len(sent) - 1]

    reader = agent.StateReader(fresh_state())  # type: ignore[arg-type]
    model, raw = agent.tool_loop(create, "sys", "user", reader)
    assert (model, raw) == ("fake-model", '{"kind": "none"}')
    # The second request carries the assistant turn and both tool results.
    second = sent[1]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool", "tool"]
    assert [m["tool_call_id"] for m in second[3:]] == ["a", "b"]
    assert "error" in json.loads(str(second[4]["content"]))
    # The call after submit never ran.
    assert reader.reads == ["links:s1-s2"]


def test_tool_loop_gives_up_after_max_turns() -> None:
    def create(messages: list[dict[str, object]]) -> _Resp:
        return _Resp([_Call("x", "list_links", "{}")])

    reader = agent.StateReader(fresh_state())  # type: ignore[arg-type]
    with pytest.raises(agent.NoSubmit):
        agent.tool_loop(create, "s", "u", reader, max_turns=3)


def test_tool_loop_returns_text_when_no_tool_is_called() -> None:
    reader = agent.StateReader(fresh_state())  # type: ignore[arg-type]
    _, raw = agent.tool_loop(lambda m: _Resp([], "hello"), "s", "u", reader)
    assert raw == "hello"


def test_reads_prompt_shows_no_state_and_tools_include_submit() -> None:
    system, user = agent.prompt_reads(True)
    assert "reroute" in system and "submit" in system
    assert "s1-s2" not in system + user
    assert "reroute" not in agent.prompt_reads(False)[0]
    assert {t["function"]["name"] for t in agent.TOOLS} == {
        "list_links", "list_paths", "get_link", "get_path", "submit"}


def test_main_reads_mode_writes_read_columns(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    net = StubNet()
    net.start = net.stop = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(agent, "build_triangle", lambda: net)
    monkeypatch.setattr(agent, "groq_tool_proposer",
                        lambda model: reading(READ_ALL, reroute(BACKUP_LINKS)))
    path = tmp_path / "inferred.csv"
    agent.main(["--evidence", "reads", "--pause", "0", "--out", str(path)])
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(agent.READ_COLUMNS)
    assert len(lines) == 1 + 6 * 9
    # A Phase 7 file refuses read-mode rows: the schemas differ.
    phase7 = tmp_path / "agent.csv"
    phase7.write_text(",".join(agent.COLUMNS) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        agent.main(["--evidence", "reads", "--pause", "0", "--out", str(phase7)])


class _Limited(Exception):
    pass


def test_retrying_waits_then_succeeds_and_finally_gives_up() -> None:
    import io

    slept: list[float] = []
    outcomes: list[object] = [_Limited(), _Limited(), "ok"]

    def call() -> str:
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return str(out)

    log = io.StringIO()
    assert agent.retrying(call, _Limited, (1.0, 2.0, 3.0), slept.append, log) == "ok"
    assert slept == [1.0, 2.0] and "rate limited" in log.getvalue()

    def always() -> str:
        raise _Limited()

    with pytest.raises(_Limited):
        agent.retrying(always, _Limited, (1.0,), slept.append, io.StringIO())
    # Anything but the rate limit is not retried.
    with pytest.raises(ValueError):
        agent.retrying(lambda: int("x"), _Limited, (1.0,), slept.append, io.StringIO())


def test_pause_sits_between_trials_only() -> None:
    slept: list[float] = []
    results = list(agent.trials(StubNet(), reading(READ_ALL, reroute(BACKUP_LINKS)),
                                1, "r", "reads", pause=7.0, sleep=slept.append))
    assert len(results) == 6 and slept == [7.0] * 5
