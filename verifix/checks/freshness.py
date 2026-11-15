"""Staleness and state-binding checks: the live-only checks.

These only run in REVALIDATE mode, and they are one of two sources of
REVALIDATE's catches over CHECK_ONCE. The other is the schema-aware checks
(feasibility, policy) re-run against live state. The two must be reported
separately: these work on any environment because they never interpret the
action, but they are also the ones that can over-block, since "the state
changed" is not the same as "the action is now wrong".
"""

from __future__ import annotations

from verifix.checks.base import Check
from verifix.models import CheckResult, NetworkState, ProposedAction

# Defaults; tune from measurement, not from taste.
MAX_DECISION_AGE_S = 5.0


class FreshnessCheck(Check):
    """Reject a decision that has aged past the staleness budget."""

    name = "freshness"
    requires_live_state = True
    schema_agnostic = True

    def __init__(self, max_age_s: float = MAX_DECISION_AGE_S) -> None:
        self.max_age_s = max_age_s

    def run(self, action: ProposedAction, state: NetworkState) -> CheckResult:
        age = state.captured_at - action.proposed_at
        if age > self.max_age_s:
            return self._fail(
                f"decision is {age:.2f}s old, budget is {self.max_age_s:.2f}s"
            )
        return self._ok()


class StateBindingCheck(Check):
    """Reject a decision whose assumed state no longer matches reality.

    This is the time-of-check-to-time-of-use case. The agent decided against
    state A; the network is now in state B. Even a perfectly reasoned action
    can be wrong - or harmful - under B.

    Two binding scopes, both measured:
      declared    - the action lists the state keys its decision rested on
                    (`depends_on`); only those are compared. Precise, but it
                    needs the agent's cooperation.
      whole-state - no declaration; every link and path is compared. Works on
                    any agent, but any unrelated change blocks.

    The scope is in the result's reason on PASS and FAIL alike, so results can
    split catches and false blocks by it. The check reads `depends_on` as an
    opaque list of keys and never interprets the action itself.
    """

    name = "state_binding"
    requires_live_state = True
    schema_agnostic = True

    def run(self, action: ProposedAction, state: NetworkState) -> CheckResult:
        if action.state_digest is None:
            return self._skip("action carries no state binding")
        if action.depends_on is not None and not action.depends_on:
            # Binding to nothing would always match: that is a pass on no
            # evidence, so it is not applicable rather than a PASS.
            return self._skip("action declares an empty dependency set")

        if action.depends_on is None:
            scope = "whole-state"
            current = state.digest()
        else:
            scope = f"declared ({len(set(action.depends_on))} keys)"
            current = state.digest(action.depends_on)

        if action.state_digest != current:
            return self._fail(
                f"{scope}: state changed since proposal "
                f"(decided against {action.state_digest}, now {current})"
            )
        return self._ok(scope)
