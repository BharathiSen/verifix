"""Schema-aware checks: feasibility and policy.

These need to understand what the action means, so on an unfamiliar
environment they report NOT_APPLICABLE rather than pretending to approve.
Keep them deliberately simple. Run against live state in REVALIDATE, they are
also one of the two sources of REVALIDATE's catches (see GateMode), so a block
they cause there is a staleness catch even though they are not live-only.
"""

from __future__ import annotations

from verifix.checks.base import Check
from verifix.models import CheckResult, NetworkState, ProposedAction


class FeasibilityCheck(Check):
    """A reroute onto a path that is down (or absent) cannot succeed."""

    name = "feasibility"
    schema_agnostic = False

    def __init__(self, known_kinds: set[str] | None = None) -> None:
        # An instance attribute, not a class one: adapters set their own
        # vocabulary, and a mutated class set would leak across every gate.
        self.known_kinds = {"reroute"} if known_kinds is None else set(known_kinds)

    def run(self, action: ProposedAction, state: NetworkState) -> CheckResult:
        if action.kind not in self.known_kinds:
            return self._skip(f"no feasibility model for action kind {action.kind!r}")
        if not state.paths:
            return self._skip("state carries no path information")
        if not state.links:
            return self._skip("state carries no link information")

        target = action.params.get("target_path")
        if target is None:
            return self._fail("reroute with no target_path")
        if target not in state.paths:
            return self._fail(f"unknown path {target!r}")

        links = state.paths[target]
        # A link the state does not report is not an up link. On a live
        # network that usually means it was deleted, which is worse than down.
        missing = [link for link in links if link not in state.links]
        if missing:
            return self._fail(
                f"path {target!r} uses links absent from state: {', '.join(missing)}"
            )
        down = [link for link in links if not state.links[link]]
        if down:
            return self._fail(f"path {target!r} has down links: {', '.join(down)}")
        return self._ok()


class PolicyCheck(Check):
    """Deny actions the operator has placed out of bounds.

    Policy is a deny-list, so an unrecognised action kind is genuinely outside
    its scope rather than unevaluable - this one can PASS on unknown kinds.
    """

    name = "policy"
    schema_agnostic = True

    def __init__(self, forbidden_kinds: set[str] | None = None) -> None:
        # `is None`, not `or`: an explicitly empty deny-list is a real choice.
        self.forbidden_kinds = (
            {"disable_mgmt_iface"} if forbidden_kinds is None else set(forbidden_kinds)
        )

    def run(self, action: ProposedAction, state: NetworkState) -> CheckResult:
        if action.kind in self.forbidden_kinds:
            return self._fail(f"action kind {action.kind!r} is forbidden by policy")
        return self._ok()
