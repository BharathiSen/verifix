"""The environment boundary.

The gate never imports an environment - it asks a StateProvider for state.
Swap the provider, same gate, same checks.

Implementations:
    StaticStateProvider (below) - tests, the static cases, caller-supplied state
    adapters/mininet.py         - a running Mininet network
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

        For a live environment this is a snapshot cached when the agent
        decided; for caller-supplied state it is whatever the caller passed.
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
