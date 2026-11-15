"""The Check interface.

Every verification rule is one small class. Adding a check means adding a file
here and registering it - no changes to the gate.

Checks come in two kinds, and the split is what lets VeriFix run on an
environment it has never seen:

  schema-agnostic  - needs only a timestamp, a state digest and (optionally)
                     the action's declared dependencies, so it works on any
                     action from any environment (freshness, state binding)
  schema-aware     - needs to understand the action's fields, so it reports
                     NOT_APPLICABLE on action kinds it does not know
                     (feasibility, policy)

A schema-aware check must never return PASS for something it did not actually
evaluate. That is the difference between a gate that degrades and a gate that
quietly stops working.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from verifix.models import CheckResult, CheckStatus, NetworkState, ProposedAction


class Check(ABC):
    name: str = "unnamed"

    #: If True, this check is only meaningful against live state at execution
    #: time (i.e. it is skipped in CHECK_ONCE mode).
    requires_live_state: bool = False

    #: If True, the check works on any action regardless of its schema.
    schema_agnostic: bool = False

    @abstractmethod
    def run(self, action: ProposedAction, state: NetworkState) -> CheckResult:
        """Evaluate this action against this state.

        Return `self._skip(...)` rather than `self._ok()` whenever the check
        cannot genuinely evaluate the action.
        """
        ...

    def _ok(self, reason: str = "") -> CheckResult:
        # A reason on PASS is for checks that can pass in more than one way,
        # where results need to know which (e.g. state binding's scope).
        return CheckResult(name=self.name, status=CheckStatus.PASS, reason=reason)

    def _fail(self, reason: str) -> CheckResult:
        return CheckResult(name=self.name, status=CheckStatus.FAIL, reason=reason)

    def _skip(self, reason: str) -> CheckResult:
        return CheckResult(
            name=self.name, status=CheckStatus.NOT_APPLICABLE, reason=reason
        )
