"""Cases 1-5: the single definition, shared by the demo and the runner.

Each case runs the gate itself and returns trials rather than actions,
because Cases 3-5 are sequences: snapshot, change the network, verify.
The verdicts only mean something while the network is in the changed state.

Every trial runs all three modes, so the CSV is uniform. What to show is the
caller's choice: the demo prints a narrative subset, `run.py` writes every row.
Nothing here prints.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from verifix.adapters.base import StateProvider, StaticStateProvider
from verifix.adapters.mininet import MininetStateProvider
from verifix.gate import DEFAULT_MIN_COVERAGE, Gate
from verifix.models import (
    Decision,
    GateMode,
    NetworkState,
    ProposedAction,
    UnknownPolicy,
)

MODES = (GateMode.OFF, GateMode.CHECK_ONCE, GateMode.REVALIDATE)

MININET_PATHS = {"primary": ["s1-s2"], "backup": ["s1-s3", "s2-s3"]}
BACKUP_DEPS = ["paths:backup", "links:s1-s3", "links:s2-s3"]


@dataclass(frozen=True)
class Trial:
    """One action under one configuration, decided in every mode."""

    case: int
    variant: str
    adapter: str
    action: ProposedAction
    unknown_policy: UnknownPolicy
    min_coverage: float
    decisions: dict[GateMode, Decision]

    @property
    def binding(self) -> str:
        # Read off the action rather than stored, so the label cannot
        # disagree with what StateBindingCheck actually bound to.
        if self.action.state_digest is None:
            return "none"
        return "whole" if self.action.depends_on is None else "declared"


def decide(
    case: int,
    variant: str,
    adapter: str,
    provider: StateProvider,
    action: ProposedAction,
    unknown_policy: UnknownPolicy = UnknownPolicy.OPEN,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
) -> Trial:
    decisions = {
        mode: Gate(provider, mode=mode, unknown_policy=unknown_policy,
                   min_coverage=min_coverage).verify(action)
        for mode in MODES
    }
    return Trial(case, variant, adapter, action, unknown_policy, min_coverage,
                 decisions)


def reroute(action_id: str, seen: NetworkState,
            depends_on: list[str] | None = None) -> ProposedAction:
    return ProposedAction(
        action_id=action_id, kind="reroute", params={"target_path": "backup"},
        proposed_at=seen.captured_at, state_digest=seen.digest(depends_on),
        depends_on=depends_on,
    )


def case_known(now: float) -> list[Trial]:
    """Case 1: reroute onto 'backup', which dies before execution."""
    at_proposal = NetworkState(
        captured_at=now,
        links={"r1-r2": True, "r3-r4": True},
        paths={"primary": ["r1-r2"], "backup": ["r3-r4"]},
    )
    live = at_proposal.model_copy(update={  # backup went down in the meantime
        "captured_at": now + 2.0, "links": {"r1-r2": True, "r3-r4": False},
    })
    provider = StaticStateProvider(live, at_proposal)
    return [decide(1, "whole-state", "static", provider,
                   reroute("a-001", at_proposal))]


def case_unknown(now: float) -> list[Trial]:
    """Case 2: opaque state, an action kind VeriFix has never seen."""
    # No topology exposed, no state binding on the action, an unknown kind.
    opaque = NetworkState(captured_at=now + 1.0, metrics={"p99_latency_ms": 31.0})
    provider = StaticStateProvider(opaque, at_proposal=opaque)
    unknown = ProposedAction(
        action_id="a-002", kind="rebalance_ecmp_hash",
        params={"router": "r7", "seed": 42}, proposed_at=now,
    )
    # 0.6 puts the floor above the 2/4 this action can reach, so the
    # unknown policy, not the checks, decides the verdict.
    return [
        decide(2, f"unknown={policy.value}", "static", provider, unknown,
               unknown_policy=policy, min_coverage=0.6)
        for policy in (UnknownPolicy.OPEN, UnknownPolicy.CLOSED)
    ]


STATIC_CASES: tuple[Callable[[float], list[Trial]], ...] = (
    case_known, case_unknown,
)


def static_trials(now: float) -> list[Trial]:
    return [trial for case in STATIC_CASES for trial in case(now)]


def case_binding_scope(net: Any, provider: MininetStateProvider) -> list[Trial]:
    """Case 3: h3-s3, on neither path, drops; whole-state vs declared binding.

    On the same topology as Cases 4 and 5, so the false block here and the
    silent miss in Case 5 come from one gate, one provider, one network.
    h3 is a stub host: its link touches s3 but is in no path, and no traffic
    between h1 and h2 needs it, so the reroute stays sound when it drops.
    """
    whole = reroute("mn3-whole", provider.snapshot("mn3-whole"))
    declared = reroute("mn3-declared", provider.snapshot("mn3-declared"),
                       BACKUP_DEPS)
    net.configLinkStatus("h3", "s3", "down")
    try:
        return [decide(3, label, "mininet", provider, action)
                for label, action in (("whole-state", whole),
                                      ("declared", declared))]
    finally:
        net.configLinkStatus("h3", "s3", "up")  # restore for Case 4


def case_target_drops(net: Any, provider: MininetStateProvider) -> list[Trial]:
    """Case 4: s1-s3 dropped between proposal and execution."""
    whole = reroute("mn4-whole", provider.snapshot("mn4-whole"))
    declared = reroute("mn4-declared", provider.snapshot("mn4-declared"),
                       BACKUP_DEPS)
    net.configLinkStatus("s1", "s3", "down")
    try:
        return [decide(4, label, "mininet", provider, action)
                for label, action in (("whole-state", whole),
                                      ("declared", declared))]
    finally:
        net.configLinkStatus("s1", "s3", "up")  # restore for Case 5


def case_premise_recovers(
    net: Any, provider: MininetStateProvider
) -> list[Trial]:
    """Case 5: the primary s1-s2 recovers before the reroute runs."""
    # The agent reroutes *because* s1-s2 is down. Every scope sees the same
    # proposal state; they differ only in what the decision is bound to.
    net.configLinkStatus("s1", "s2", "down")
    scopes: list[tuple[str, list[str] | None]] = [
        ("whole-state", None),
        ("declared target only", BACKUP_DEPS),
        ("declared premise+target", ["links:s1-s2", *BACKUP_DEPS]),
    ]
    actions = [
        (label, reroute(f"mn5-{i}", provider.snapshot(f"mn5-{i}"), deps))
        for i, (label, deps) in enumerate(scopes)
    ]
    net.configLinkStatus("s1", "s2", "up")
    return [decide(5, label, "mininet", provider, action)
            for label, action in actions]


def network_trials(net: Any) -> list[Trial]:
    """Cases 3, 4 and 5 against any net with Mininet's link API.

    Split from `mininet_trials` so the case logic can be exercised against a
    stub without Mininet installed. One provider for all three: each case
    restores the link it dropped before the next one snapshots.
    """
    provider = MininetStateProvider(net, MININET_PATHS)
    return (case_binding_scope(net, provider)
            + case_target_drops(net, provider)
            + case_premise_recovers(net, provider))


#: The topology's links, in the order they are added. The test stub reads
#: this too, so the stub and the real topology cannot drift
#: apart.
TRIANGLE_LINKS = (("h1", "s1"), ("s1", "s2"), ("s1", "s3"), ("s3", "s2"),
                  ("s2", "h2"), ("h3", "s3"))


def build_triangle() -> Any:
    """The Mininet net for Cases 3-5, built but not started. Linux, Mininet.

    h1-s1-s2-h2 is the primary path, s1-s3-s2 the backup, and h3 a stub host
    on s3 whose link is on neither path (Case 3 drops it).
    """
    # Imported here so Cases 1-2 run on machines without Mininet.
    from mininet.net import Mininet
    from mininet.topo import Topo

    class Triangle(Topo):
        def build(self) -> None:
            for name in sorted({n for link in TRIANGLE_LINKS for n in link}):
                if name.startswith("h"):
                    self.addHost(name)
                else:
                    self.addSwitch(name)
            for a, b in TRIANGLE_LINKS:
                self.addLink(a, b)

    # No controller: the cases read link state and forward no traffic. What
    # the switches do with frames in that state depends on their OVS fail
    # mode, which is recorded from WSL2 rather than assumed here.
    return Mininet(topo=Triangle(), controller=None)


def mininet_trials() -> list[Trial]:
    """Cases 3, 4 and 5 on the real Mininet topology. Linux, root, Mininet."""
    net = build_triangle()
    net.start()
    try:
        return network_trials(net)
    finally:
        net.stop()
