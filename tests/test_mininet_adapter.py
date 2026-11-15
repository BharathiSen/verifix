"""MininetStateProvider against a stubbed network.

The fakes mirror the slice of Mininet's API the adapter reads: `net.links`,
`link.intf1/intf2`, `intf.node.name`, `intf.isUp()`. No Mininet is imported.
"""

from __future__ import annotations

import pytest

from verifix.adapters.mininet import MininetStateProvider, link_id
from verifix.gate import Gate
from verifix.models import CheckStatus, GateMode, ProposedAction, Verdict


class FakeNode:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeIntf:
    def __init__(self, node: str) -> None:
        self.node = FakeNode(node)
        self.up = True

    def isUp(self) -> bool:
        return self.up


class FakeLink:
    def __init__(self, a: str, b: str) -> None:
        self.intf1 = FakeIntf(a)
        self.intf2 = FakeIntf(b)


class FakeNet:
    """h1-s1, s1-s2 (primary), s1-s3-s2 (backup), s2-h2."""

    def __init__(self) -> None:
        self.links = [
            FakeLink("h1", "s1"),
            FakeLink("s1", "s2"),
            FakeLink("s1", "s3"),
            FakeLink("s3", "s2"),  # reversed on purpose: ids must not care
            FakeLink("s2", "h2"),
        ]

    def link(self, a: str, b: str) -> FakeLink:
        return next(
            lk for lk in self.links
            if link_id(lk.intf1.node.name, lk.intf2.node.name) == link_id(a, b)
        )

    def down(self, a: str, b: str) -> None:
        self.link(a, b).intf2.up = False

    def delete(self, a: str, b: str) -> None:
        self.links.remove(self.link(a, b))


PATHS = {"primary": ["s1-s2"], "backup": ["s1-s3", "s2-s3"]}
BACKUP_DEPS = ["paths:backup", "links:s1-s3", "links:s2-s3"]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def net() -> FakeNet:
    return FakeNet()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def provider(net: FakeNet, clock: Clock) -> MininetStateProvider:
    return MininetStateProvider(net, PATHS, clock=clock)


def _reroute(
    provider: MininetStateProvider,
    action_id: str = "a",
    depends_on: list[str] | None = None,
) -> ProposedAction:
    seen = provider.snapshot(action_id)
    return ProposedAction(
        action_id=action_id,
        kind="reroute",
        params={"target_path": "backup"},
        proposed_at=seen.captured_at,
        state_digest=seen.digest(depends_on),
        depends_on=depends_on,
    )


def _status(decision_checks: list, name: str) -> CheckStatus:
    return next(c.status for c in decision_checks if c.name == name)


def test_reads_link_status_with_canonical_ids(
    net: FakeNet, provider: MininetStateProvider
) -> None:
    net.down("s1", "s3")
    links = provider.live_state().links
    assert set(links) == {"h1-s1", "s1-s2", "s1-s3", "s2-s3", "h2-s2"}
    assert links["s1-s3"] is False
    assert links["s2-s3"] is True


def test_one_interface_down_means_link_down(
    net: FakeNet, provider: MininetStateProvider
) -> None:
    net.link("s1", "s2").intf1.up = False
    assert provider.live_state().links["s1-s2"] is False


def test_digest_is_stable_across_reads_of_an_unchanged_network(
    provider: MininetStateProvider, clock: Clock
) -> None:
    first = provider.live_state()
    clock.now += 3
    second = provider.live_state()
    assert first.digest() == second.digest()
    assert second.captured_at == first.captured_at + 3


def test_snapshot_is_unaffected_by_later_changes(
    net: FakeNet, provider: MininetStateProvider
) -> None:
    action = _reroute(provider)
    net.down("s1", "s3")
    assert provider.state_at_proposal(action).links["s1-s3"] is True
    assert provider.live_state().links["s1-s3"] is False


def test_returned_snapshot_cannot_mutate_the_cache(
    provider: MininetStateProvider,
) -> None:
    action = _reroute(provider)
    provider.state_at_proposal(action).links["s1-s3"] = False
    assert provider.state_at_proposal(action).links["s1-s3"] is True


def test_cache_miss_is_an_error_not_a_live_fallback(
    provider: MininetStateProvider,
) -> None:
    orphan = ProposedAction(
        action_id="never-snapshotted", kind="reroute",
        params={"target_path": "backup"}, proposed_at=1000.0,
    )
    with pytest.raises(LookupError):
        provider.state_at_proposal(orphan)
    d = Gate(provider, mode=GateMode.CHECK_ONCE).verify(orphan)
    assert d.coverage.errored == ["state_provider"]
    assert d.coverage.evaluated == 0


def test_duplicate_snapshot_is_refused(provider: MininetStateProvider) -> None:
    provider.snapshot("a")
    with pytest.raises(ValueError):
        provider.snapshot("a")


def test_cache_is_bounded(net: FakeNet, clock: Clock) -> None:
    provider = MininetStateProvider(net, PATHS, clock=clock, cache_size=2)
    for action_id in ("a", "b", "c"):
        provider.snapshot(action_id)
    oldest = ProposedAction(action_id="a", kind="reroute", proposed_at=0.0)
    with pytest.raises(LookupError):
        provider.state_at_proposal(oldest)


def test_paths_naming_missing_links_are_rejected(net: FakeNet) -> None:
    with pytest.raises(ValueError, match="s9-s1"):
        MininetStateProvider(net, {"bogus": ["s1-s2", "s9-s1"]})


def test_parallel_links_are_refused(net: FakeNet) -> None:
    net.links.append(FakeLink("s2", "s1"))
    with pytest.raises(ValueError, match="parallel"):
        MininetStateProvider(net, PATHS)


def test_link_dropped_after_proposal(
    net: FakeNet, provider: MininetStateProvider, clock: Clock
) -> None:
    # The Phase 1 scenario: check_once approves, revalidate blocks.
    action = _reroute(provider)
    clock.now += 1
    net.down("s1", "s3")
    once = Gate(provider, mode=GateMode.CHECK_ONCE).verify(action)
    live = Gate(provider, mode=GateMode.REVALIDATE).verify(action)
    assert once.verdict is Verdict.ALLOW
    assert live.verdict is Verdict.BLOCK
    assert _status(live.checks, "feasibility") is CheckStatus.FAIL
    assert _status(live.checks, "state_binding") is CheckStatus.FAIL


def test_deleted_link_fails_feasibility_rather_than_passing(
    net: FakeNet, provider: MininetStateProvider
) -> None:
    action = _reroute(provider)
    net.delete("s1", "s3")
    d = Gate(provider, mode=GateMode.REVALIDATE).verify(action)
    feas = next(c for c in d.checks if c.name == "feasibility")
    assert feas.status is CheckStatus.FAIL
    assert "absent" in feas.reason


def test_declared_binding_ignores_unrelated_change(
    net: FakeNet, provider: MininetStateProvider
) -> None:
    # The false-block case: a link the reroute does not use goes down.
    whole = _reroute(provider, "whole")
    declared = _reroute(provider, "declared", depends_on=BACKUP_DEPS)
    net.down("h2", "s2")
    gate = Gate(provider, mode=GateMode.REVALIDATE)
    assert gate.verify(whole).verdict is Verdict.BLOCK
    assert gate.verify(declared).verdict is Verdict.ALLOW


def test_declared_binding_still_catches_a_dependency_change(
    net: FakeNet, provider: MininetStateProvider
) -> None:
    action = _reroute(provider, depends_on=BACKUP_DEPS)
    net.down("s2", "s3")
    d = Gate(provider, mode=GateMode.REVALIDATE).verify(action)
    binding = next(c for c in d.checks if c.name == "state_binding")
    assert binding.status is CheckStatus.FAIL
    assert binding.reason.startswith("declared (3 keys)")
