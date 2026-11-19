from __future__ import annotations

import time
import pytest
from fastapi import HTTPException

from verifix import api
from verifix.adapters.base import StaticStateProvider
from verifix.checks.feasibility import FeasibilityCheck, PolicyCheck
from verifix.checks.freshness import StateBindingCheck
from verifix.gate import Gate
from verifix.models import (
    CheckStatus, GateMode, NetworkState, ProposedAction, UnknownPolicy, Verdict,
)


@pytest.fixture
def states():
    now = time.time()
    before = NetworkState(captured_at=now, links={"l1": True},
                          paths={"backup": ["l1"]})
    after = NetworkState(captured_at=now + 1, links={"l1": False},
                         paths={"backup": ["l1"]})
    return now, before, after


def _action(now, digest=None, kind="reroute"):
    return ProposedAction(action_id="a", kind=kind,
                          params={"target_path": "backup"},
                          proposed_at=now, state_digest=digest)


def test_check_once_misses_what_revalidate_catches(states):
    now, before, after = states
    p = StaticStateProvider(live=after, at_proposal=before)
    a = _action(now, before.digest())
    assert Gate(p, mode=GateMode.CHECK_ONCE).verify(a).verdict is Verdict.ALLOW
    assert Gate(p, mode=GateMode.REVALIDATE).verify(a).verdict is Verdict.BLOCK


def test_off_mode_never_blocks(states):
    now, before, after = states
    p = StaticStateProvider(live=after, at_proposal=before)
    assert Gate(p, mode=GateMode.OFF).verify(_action(now)).verdict is Verdict.ALLOW


def test_off_mode_says_it_did_not_check(states):
    now, before, after = states
    p = StaticStateProvider(live=after, at_proposal=before)
    d = Gate(p, mode=GateMode.OFF).verify(_action(now, before.digest()))
    assert d.reason == "verification disabled"


def test_unknown_action_is_skipped_not_passed(states):
    now, before, _ = states
    p = StaticStateProvider(live=before, at_proposal=before)
    d = Gate(p).verify(_action(now, before.digest(), kind="never_seen"))
    assert "feasibility" in d.coverage.skipped
    assert d.coverage.evaluated < d.coverage.total


def test_opaque_state_is_weakly_verified_and_policy_decides(states):
    now, _, _ = states
    opaque = NetworkState(captured_at=now + 1)
    p = StaticStateProvider(live=opaque, at_proposal=opaque)
    a = _action(now, kind="never_seen")
    assert Gate(p, unknown_policy=UnknownPolicy.OPEN,
                min_coverage=0.6).verify(a).verdict is Verdict.ALLOW
    assert Gate(p, unknown_policy=UnknownPolicy.CLOSED,
                min_coverage=0.6).verify(a).verdict is Verdict.BLOCK


def test_a_broken_check_does_not_crash_the_gate(states):
    now, before, _ = states

    class Exploding:
        name = "boom"
        requires_live_state = False
        schema_agnostic = True
        def run(self, action, state):
            raise RuntimeError("unfamiliar state shape")

    p = StaticStateProvider(live=before, at_proposal=before)
    d = Gate(p, checks=[Exploding()]).verify(_action(now, before.digest()))
    assert d.coverage.errored == ["boom"]


# --- feasibility and policy fixes -----------------------------------------

def test_feasibility_fails_a_path_over_a_link_absent_from_state() -> None:
    state = NetworkState(captured_at=0.0, links={"l1": True},
                         paths={"backup": ["l1", "l2"]})
    r = FeasibilityCheck().run(_action(0.0), state)
    assert r.status is CheckStatus.FAIL
    assert "l2" in r.reason


def test_feasibility_skips_when_state_reports_no_links() -> None:
    state = NetworkState(captured_at=0.0, paths={"backup": ["l1"]})
    assert FeasibilityCheck().run(_action(0.0), state).status is \
        CheckStatus.NOT_APPLICABLE


def test_known_kinds_is_per_instance() -> None:
    a = FeasibilityCheck(known_kinds={"reroute", "drain"})
    b = FeasibilityCheck()
    assert "drain" in a.known_kinds
    assert b.known_kinds == {"reroute"}


def test_empty_forbidden_kinds_is_respected() -> None:
    state = NetworkState(captured_at=0.0)
    a = _action(0.0, kind="disable_mgmt_iface")
    assert PolicyCheck(forbidden_kinds=set()).run(a, state).status is CheckStatus.PASS
    assert PolicyCheck().run(a, state).status is CheckStatus.FAIL


# --- state binding scopes -------------------------------------------------

def _bound(state: NetworkState, deps: list[str] | None) -> ProposedAction:
    return ProposedAction(action_id="b", kind="reroute", proposed_at=0.0,
                          state_digest=state.digest(deps), depends_on=deps)


def test_unrelated_change_blocks_whole_state_but_not_declared(states) -> None:
    now, _, _ = states
    before = NetworkState(captured_at=now, links={"l1": True, "l2": True})
    after = NetworkState(captured_at=now, links={"l1": True, "l2": False})
    check = StateBindingCheck()
    whole = check.run(_bound(before, None), after)
    declared = check.run(_bound(before, ["links:l1"]), after)
    assert whole.status is CheckStatus.FAIL and whole.reason.startswith("whole-state")
    assert declared.status is CheckStatus.PASS and declared.reason.startswith("declared")


def test_declared_link_disappearing_changes_the_digest() -> None:
    before = NetworkState(captured_at=0.0, links={"l1": True})
    after = NetworkState(captured_at=0.0, links={})
    assert StateBindingCheck().run(_bound(before, ["links:l1"]), after).status \
        is CheckStatus.FAIL


def test_empty_dependency_set_is_not_a_pass() -> None:
    state = NetworkState(captured_at=0.0, links={"l1": True})
    assert StateBindingCheck().run(_bound(state, []), state).status is \
        CheckStatus.NOT_APPLICABLE


@pytest.mark.parametrize("digest, depends_on", [(None, None), ("x", [])])
def test_opting_out_of_state_binding_is_flagged(digest, depends_on) -> None:
    # No digest, or an empty dependency set, makes state binding abstain.
    # At 3/4 that cleared the 0.5 floor, so it must flag on its own.
    state = NetworkState(captured_at=0.0, links={"l1": True},
                         paths={"backup": ["l1"]})
    a = ProposedAction(action_id="c", kind="reroute",
                       params={"target_path": "backup"}, proposed_at=0.0,
                       state_digest=digest, depends_on=depends_on)
    p = StaticStateProvider(state)
    opened = Gate(p, unknown_policy=UnknownPolicy.OPEN).verify(a)
    closed = Gate(p, unknown_policy=UnknownPolicy.CLOSED).verify(a)
    assert (opened.coverage.evaluated, opened.coverage.total) == (3, 4)
    assert opened.coverage.skipped == ["state_binding"]
    assert opened.verdict is Verdict.ALLOW and opened.weakly_verified
    assert closed.verdict is Verdict.BLOCK and closed.weakly_verified
    assert "skipped: state_binding" in opened.reason


def test_schema_aware_abstention_alone_is_left_to_the_ratio(states) -> None:
    # Feasibility abstaining on an unknown kind is the floor's job, not an
    # opt-out: 3/4 with a bound digest stays unflagged at the default.
    now, before, _ = states
    d = Gate(StaticStateProvider(before), unknown_policy=UnknownPolicy.CLOSED
             ).verify(_action(now, before.digest(), kind="rebalance_ecmp_hash"))
    assert d.coverage.skipped == ["feasibility"]
    assert not d.weakly_verified and d.verdict is Verdict.ALLOW


def test_unresolvable_dependency_key_is_an_error_in_the_gate() -> None:
    state = NetworkState(captured_at=0.0, links={"l1": True},
                         paths={"backup": ["l1"]})
    a = ProposedAction(action_id="b", kind="reroute",
                       params={"target_path": "backup"}, proposed_at=0.0,
                       state_digest="x", depends_on=["link:l1"])
    p = StaticStateProvider(state)
    opened = Gate(p, unknown_policy=UnknownPolicy.OPEN).verify(a)
    closed = Gate(p, unknown_policy=UnknownPolicy.CLOSED).verify(a)
    # 3/4 clears the 0.5 floor; the error must flag on its own, not via ratio.
    assert (opened.coverage.evaluated, opened.coverage.total) == (3, 4)
    assert "state_binding" in opened.coverage.errored
    assert opened.verdict is Verdict.ALLOW and opened.weakly_verified
    assert closed.verdict is Verdict.BLOCK and closed.weakly_verified
    assert "errored: state_binding" in opened.reason


def test_an_error_flags_even_above_the_coverage_floor(states) -> None:
    now, before, _ = states

    class Exploding:
        name = "boom"
        requires_live_state = False
        schema_agnostic = True
        def run(self, action, state):
            raise RuntimeError("unfamiliar state shape")

    p = StaticStateProvider(live=before, at_proposal=before)
    # The raising check comes first, so the ones after it must still run.
    checks = [Exploding(), PolicyCheck(), FeasibilityCheck()]
    for policy, verdict in ((UnknownPolicy.OPEN, Verdict.ALLOW),
                            (UnknownPolicy.CLOSED, Verdict.BLOCK)):
        d = Gate(p, checks=checks, unknown_policy=policy,
                 min_coverage=0.1).verify(_action(now, before.digest()))
        assert [c.name for c in d.checks] == ["boom", "policy", "feasibility"]
        assert [c.status for c in d.checks[1:]] == [CheckStatus.PASS] * 2
        assert d.weakly_verified and d.verdict is verdict
        assert d.reason.endswith("errored: boom)")


def test_a_failure_reason_still_names_errors(states) -> None:
    now, before, after = states

    class Exploding:
        name = "boom"
        requires_live_state = False
        schema_agnostic = True
        def run(self, action, state):
            raise RuntimeError("x")

    p = StaticStateProvider(live=after)
    d = Gate(p, checks=[FeasibilityCheck(), Exploding()]).verify(
        _action(now, before.digest()))
    assert d.verdict is Verdict.BLOCK
    assert d.reason.startswith("feasibility:") and d.reason.endswith("errored: boom")


def test_state_provider_failure_reason_names_the_error(states) -> None:
    now, before, _ = states
    d = Gate(StaticStateProvider(before), mode=GateMode.CHECK_ONCE).verify(
        _action(now, before.digest()))
    assert d.weakly_verified and d.verdict is Verdict.ALLOW
    assert "errored: state_provider" in d.reason
    closed = Gate(StaticStateProvider(before), mode=GateMode.CHECK_ONCE,
                  unknown_policy=UnknownPolicy.CLOSED).verify(
        _action(now, before.digest()))
    assert closed.verdict is Verdict.BLOCK


# --- api ------------------------------------------------------------------

@pytest.fixture
def configured():
    yield api.configure
    api.configure(None)


def test_api_override_keeps_unknown_policy(states, configured) -> None:
    now, _, _ = states
    opaque = NetworkState(captured_at=now)
    configured(Gate(StaticStateProvider(opaque), unknown_policy=UnknownPolicy.CLOSED,
                    min_coverage=0.6))
    req = api.VerifyRequest(action=_action(now, kind="never_seen"), live_state=opaque)
    assert api.verify(req).verdict is Verdict.BLOCK


def test_api_check_once_without_proposal_state_errors(states, configured) -> None:
    now, before, after = states
    configured(Gate(StaticStateProvider(before), mode=GateMode.CHECK_ONCE))
    req = api.VerifyRequest(action=_action(now, before.digest()), live_state=after)
    d = api.verify(req)
    assert d.coverage.errored == ["state_provider"]
    req.proposal_state = before
    assert api.verify(req).checks[0].status is not CheckStatus.ERROR


def test_api_unconfigured_without_state_refuses() -> None:
    api.configure(None)
    with pytest.raises(HTTPException) as exc:
        api.verify(api.VerifyRequest(action=_action(0.0)))
    assert exc.value.status_code == 503
