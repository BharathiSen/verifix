"""Two-minute demo.

Case 1: a known action, three modes, three outcomes.
Case 2: an action kind VeriFix has never seen - it degrades, and says so.
Case 3: an unrelated link drops - whole-state binding blocks, declared does not.
Case 4 (--mininet): Case 1 on a real Mininet topology. Linux, root, Mininet.
Case 5 (--mininet): the primary recovers before execution. The backup is still
        feasible, so only state binding can block, and only if the binding
        covers the premise the agent acted on.

    python demo/run_demo.py
    sudo python demo/run_demo.py --mininet
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

sys.path.insert(0, ".")

from verifix.adapters.base import StateProvider, StaticStateProvider
from verifix.adapters.mininet import MininetStateProvider
from verifix.gate import Gate
from verifix.models import GateMode, NetworkState, ProposedAction, UnknownPolicy

MODES = (GateMode.OFF, GateMode.CHECK_ONCE, GateMode.REVALIDATE)


def banner(title: str) -> None:
    print("=" * 72)
    print(title)
    print("=" * 72)


def show_modes(provider: StateProvider, action: ProposedAction) -> None:
    for mode in MODES:
        d = Gate(provider, mode=mode).verify(action)
        print(f"  {mode.value:<12} {d.verdict.value.upper():<5} "
              f"cov {d.coverage.evaluated}/{d.coverage.total}  {d.reason}")


def reroute(action_id: str, seen: NetworkState,
            depends_on: list[str] | None = None) -> ProposedAction:
    return ProposedAction(
        action_id=action_id, kind="reroute", params={"target_path": "backup"},
        proposed_at=seen.captured_at, state_digest=seen.digest(depends_on),
        depends_on=depends_on,
    )


def case_known(now: float) -> None:
    banner("CASE 1  known action: reroute onto 'backup', which dies before execution")
    at_proposal = NetworkState(
        captured_at=now,
        links={"r1-r2": True, "r3-r4": True},
        paths={"primary": ["r1-r2"], "backup": ["r3-r4"]},
    )
    live = at_proposal.model_copy(update={  # backup went down in the meantime
        "captured_at": now + 2.0, "links": {"r1-r2": True, "r3-r4": False},
    })
    show_modes(StaticStateProvider(live, at_proposal), reroute("a-001", at_proposal))


def case_unknown(now: float) -> None:
    banner("CASE 2  unknown environment: opaque state, unrecognised action kind")
    # An environment VeriFix has no model for: no topology exposed, no state
    # binding on the action, an action kind it has never seen.
    opaque = NetworkState(captured_at=now + 1.0, metrics={"p99_latency_ms": 31.0})
    provider = StaticStateProvider(opaque, at_proposal=opaque)
    unknown = ProposedAction(
        action_id="a-002", kind="rebalance_ecmp_hash",
        params={"router": "r7", "seed": 42}, proposed_at=now,
    )
    for policy in (UnknownPolicy.OPEN, UnknownPolicy.CLOSED):
        d = Gate(provider, mode=GateMode.REVALIDATE,
                 unknown_policy=policy, min_coverage=0.6).verify(unknown)
        print(f"  unknown={policy.value:<7} {d.verdict.value.upper():<5} "
              f"cov {d.coverage.evaluated}/{d.coverage.total}  "
              f"weak={d.weakly_verified}")
        print(f"               {d.reason}")


def case_binding_scope(now: float) -> None:
    banner("CASE 3  unrelated link drops: whole-state vs declared binding")
    at_proposal = NetworkState(
        captured_at=now,
        links={"r1-r2": True, "r3-r4": True, "r5-r6": True},
        paths={"primary": ["r1-r2"], "backup": ["r3-r4"]},
    )
    live = at_proposal.model_copy(update={  # r5-r6 is on neither path
        "captured_at": now + 2.0,
        "links": {"r1-r2": True, "r3-r4": True, "r5-r6": False},
    })
    provider = StaticStateProvider(live, at_proposal)
    gate = Gate(provider, mode=GateMode.REVALIDATE)
    for label, deps in (("whole-state", None),
                        ("declared", ["paths:backup", "links:r3-r4"])):
        d = gate.verify(reroute(f"a-003-{label}", at_proposal, deps))
        print(f"  {label:<12} {d.verdict.value.upper():<5} {d.reason}")


def case_mininet() -> None:
    # Imported here so the other cases run on machines without Mininet.
    from mininet.net import Mininet
    from mininet.topo import Topo

    class Triangle(Topo):
        """h1-s1-s2-h2 is the primary path; s1-s3-s2 is the backup."""

        def build(self) -> None:
            h1, h2 = self.addHost("h1"), self.addHost("h2")
            s1, s2, s3 = (self.addSwitch(n) for n in ("s1", "s2", "s3"))
            for a, b in ((h1, s1), (s1, s2), (s1, s3), (s3, s2), (s2, h2)):
                self.addLink(a, b)

    # No controller: the demo checks link state, it does not forward traffic,
    # and without one the triangle cannot form a broadcast loop.
    net = Mininet(topo=Triangle(), controller=None)
    net.start()
    try:
        provider = MininetStateProvider(
            net, {"primary": ["s1-s2"], "backup": ["s1-s3", "s2-s3"]}
        )
        case_mininet_target_drops(net, provider)
        print()
        case_mininet_premise_recovers(net, provider)
    finally:
        net.stop()


BACKUP_DEPS = ["paths:backup", "links:s1-s3", "links:s2-s3"]


def case_mininet_target_drops(net: Any, provider: MininetStateProvider) -> None:
    banner("CASE 4  Mininet: s1-s3 dropped between proposal and execution")
    whole = reroute("mn4-whole", provider.snapshot("mn4-whole"))
    declared = reroute("mn4-declared", provider.snapshot("mn4-declared"),
                       BACKUP_DEPS)
    net.configLinkStatus("s1", "s3", "down")
    for label, action in (("whole-state binding", whole),
                          ("declared binding", declared)):
        print(f" {label}")
        show_modes(provider, action)
    net.configLinkStatus("s1", "s3", "up")  # restore for Case 5


def case_mininet_premise_recovers(
    net: Any, provider: MininetStateProvider
) -> None:
    banner("CASE 5  Mininet: primary s1-s2 recovers before the reroute runs")
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
    for label, action in actions:
        once = Gate(provider, mode=GateMode.CHECK_ONCE).verify(action)
        live = Gate(provider, mode=GateMode.REVALIDATE).verify(action)
        status = {c.name: c.status.value for c in live.checks}
        print(f"  {label:<24} check_once {once.verdict.value.upper():<5} "
              f"revalidate {live.verdict.value.upper():<5} "
              f"feasibility={status['feasibility']} "
              f"state_binding={status['state_binding']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mininet", action="store_true",
                        help="also run Cases 4 and 5 on a real Mininet topology")
    args = parser.parse_args()

    now = time.time()
    for case in (case_known, case_unknown, case_binding_scope):
        case(now)
        print()
    if args.mininet:
        case_mininet()
        print()
    print("The gate never guesses and never crashes. It reports what it could")
    print("actually check, so a thin result is visible instead of looking like a pass.")


if __name__ == "__main__":
    main()
