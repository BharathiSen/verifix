"""The environment boundary.

This interface is what turns VeriFix from a Mininet script into a product.
The gate never imports Mininet, NetArena, or a trace file - it asks a
StateProvider for state. Swap the provider, same gate, same checks, same
results pipeline.

Implementations:
    adapters/mininet.py    - your development and demo environment
    adapters/netarena.py   - the shared benchmark, for the paper's numbers
    adapters/replay.py     - offline replay of recorded agent traces
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from verifix.models import NetworkState, ProposedAction


class StateProvider(ABC):
    @abstractmethod
    def live_state(self) -> NetworkState:
        """Current state of the network, read now."""
        ...

    @abstractmethod
    def state_at_proposal(self, action: ProposedAction) -> NetworkState:
        """The state as it was when the agent made this decision.

        For live environments this is a cached snapshot; for replayed traces
        it is read from the record.
        """
        ...


class StaticStateProvider(StateProvider):
    """Trivial provider for tests, the demo and caller-supplied API state."""

    def __init__(
        self, live: NetworkState, at_proposal: NetworkState | None = None
    ) -> None:
        self._live = live
        self._at_proposal = at_proposal

    def live_state(self) -> NetworkState:
        return self._live

    def state_at_proposal(self, action: ProposedAction) -> NetworkState:
        # Never fall back to live state: that would make CHECK_ONCE quietly
        # behave like REVALIDATE and corrupt the comparison between them.
        if self._at_proposal is None:
            raise LookupError("no proposal-time state was supplied")
        return self._at_proposal
