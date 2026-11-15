"""StateProvider for a running Mininet network.

This module never imports Mininet. It reads a `net` object handed to it
(`net.links`, each link's two interfaces, `intf.isUp()`), so it and its tests
run anywhere; only the process that builds the topology needs Mininet and
root. That split is deliberate: the adapter is developed on any machine, and
the real topology runs on the Linux VM.

Mininet has no notion of a path, so candidate paths are supplied by whoever
built the topology, as lists of link ids.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Callable, Iterable, Protocol

from verifix.adapters.base import StateProvider
from verifix.models import NetworkState, ProposedAction


class _Node(Protocol):
    name: str


class _Intf(Protocol):
    node: _Node

    def isUp(self) -> bool: ...


class _Link(Protocol):
    intf1: _Intf
    intf2: _Intf


class _Net(Protocol):
    links: Iterable[_Link]


def link_id(node_a: str, node_b: str) -> str:
    """Canonical id for the link between two nodes.

    Sorted, so the same link always gets the same id however Mininet orders
    its endpoints. An unstable id would change the digest when the network
    did not, and state binding would block for nothing.
    """
    return "-".join(sorted((node_a, node_b)))


class MininetStateProvider(StateProvider):
    def __init__(
        self,
        net: _Net,
        paths: dict[str, list[str]],
        clock: Callable[[], float] = time.time,
        cache_size: int = 256,
    ) -> None:
        if cache_size < 1:
            raise ValueError("cache_size must be at least 1")
        self._net = net
        self._paths = {name: list(links) for name, links in paths.items()}
        self._clock = clock
        self._cache_size = cache_size
        self._snapshots: OrderedDict[str, NetworkState] = OrderedDict()

        # A path naming a link the topology lacks is a configuration error.
        # Caught here, it is one clear message; caught later, it is every
        # reroute onto that path failing feasibility for a misleading reason.
        present = self._read_links()
        unknown = sorted(
            {link for links in self._paths.values() for link in links} - set(present)
        )
        if unknown:
            raise ValueError(f"paths reference links not in topology: {unknown}")

    def live_state(self) -> NetworkState:
        links = self._read_links()
        # Timestamp after the read, not before: the state is only known once
        # read, and erring late makes freshness stricter rather than looser.
        return NetworkState(
            captured_at=self._clock(),
            links=links,
            paths={name: list(hops) for name, hops in self._paths.items()},
        )

    def snapshot(self, action_id: str) -> NetworkState:
        """Read state now and keep it as the proposal-time state for an action.

        The harness calls this when the agent decides, and the agent binds
        its action to the returned state's digest.
        """
        if action_id in self._snapshots:
            # Overwriting would silently move an action's proposal state.
            raise ValueError(f"snapshot already taken for action {action_id!r}")
        state = self.live_state()
        self._snapshots[action_id] = state.model_copy(deep=True)
        while len(self._snapshots) > self._cache_size:
            self._snapshots.popitem(last=False)
        return state

    def state_at_proposal(self, action: ProposedAction) -> NetworkState:
        # A miss must raise, never fall back to live state: that would make
        # CHECK_ONCE quietly behave like REVALIDATE. The gate records it.
        try:
            cached = self._snapshots[action.action_id]
        except KeyError:
            raise LookupError(
                f"no proposal snapshot for action {action.action_id!r}"
            ) from None
        return cached.model_copy(deep=True)

    def _read_links(self) -> dict[str, bool]:
        links: dict[str, bool] = {}
        for link in self._net.links:
            lid = link_id(link.intf1.node.name, link.intf2.node.name)
            if lid in links:
                # Parallel links would share an id and hide each other's
                # status. Not needed for our topologies, so refuse loudly.
                raise ValueError(f"parallel links between {lid} are not supported")
            # One end down is enough to stop traffic, so a link is up only
            # when both of its interfaces are.
            links[lid] = bool(link.intf1.isUp()) and bool(link.intf2.isUp())
        return links
