"""The gate: runs checks according to the configured mode.

The three evaluation conditions in your paper are three values of `mode` on
one object. You do not implement baselines - you configure them.

The gate is written to survive environments it has never seen: a check that
raises is caught, a check that cannot evaluate the action is recorded as
skipped, and the resulting coverage is reported on every decision.
"""

from __future__ import annotations

import time

from verifix.adapters.base import StateProvider
from verifix.checks.base import Check
from verifix.checks.feasibility import FeasibilityCheck, PolicyCheck
from verifix.checks.freshness import FreshnessCheck, StateBindingCheck
from verifix.models import (
    CheckResult,
    CheckStatus,
    Coverage,
    Decision,
    GateMode,
    ProposedAction,
    UnknownPolicy,
    Verdict,
)

DEFAULT_CHECKS: list[Check] = [
    PolicyCheck(),
    FeasibilityCheck(),
    FreshnessCheck(),
    StateBindingCheck(),
]

#: Below this fraction of applicable checks, a decision is flagged as weakly
#: verified. Tune from measurement.
DEFAULT_MIN_COVERAGE = 0.5


class Gate:
    def __init__(
        self,
        state_provider: StateProvider,
        mode: GateMode = GateMode.REVALIDATE,
        checks: list[Check] | None = None,
        unknown_policy: UnknownPolicy = UnknownPolicy.OPEN,
        min_coverage: float = DEFAULT_MIN_COVERAGE,
    ) -> None:
        self.state_provider = state_provider
        self.mode = mode
        # A copy, so appending to one gate's checks cannot change another's.
        self.checks = list(checks if checks is not None else DEFAULT_CHECKS)
        self.unknown_policy = unknown_policy
        self.min_coverage = min_coverage

    def verify(self, action: ProposedAction) -> Decision:
        started = time.perf_counter()

        if self.mode is GateMode.OFF:
            return Decision(
                action_id=action.action_id,
                verdict=Verdict.ALLOW,
                mode=self.mode,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        # CHECK_ONCE validates against the state the agent saw.
        # REVALIDATE validates against live state at execution time, and only
        # that; see GateMode for why there is no proposal-time pass.
        try:
            if self.mode is GateMode.CHECK_ONCE:
                state = self.state_provider.state_at_proposal(action)
                active = [c for c in self.checks if not c.requires_live_state]
            else:
                state = self.state_provider.live_state()
                active = list(self.checks)
        except Exception as exc:  # the environment itself failed us
            return Decision(
                action_id=action.action_id,
                verdict=(
                    Verdict.BLOCK
                    if self.unknown_policy is UnknownPolicy.CLOSED
                    else Verdict.ALLOW
                ),
                mode=self.mode,
                checks=[
                    CheckResult(
                        name="state_provider",
                        status=CheckStatus.ERROR,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                ],
                coverage=Coverage(
                    evaluated=0, total=len(self.checks), errored=["state_provider"]
                ),
                weakly_verified=True,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        results: list[CheckResult] = []
        for check in active:
            try:
                results.append(check.run(action, state))
            except Exception as exc:
                # An unfamiliar action or state shape must never take the gate
                # down. Record it and keep going.
                results.append(
                    CheckResult(
                        name=check.name,
                        status=CheckStatus.ERROR,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                )

        coverage = Coverage(
            evaluated=sum(
                1 for r in results if r.status in (CheckStatus.PASS, CheckStatus.FAIL)
            ),
            total=len(active),
            skipped=[
                r.name for r in results if r.status is CheckStatus.NOT_APPLICABLE
            ],
            errored=[r.name for r in results if r.status is CheckStatus.ERROR],
        )
        # An errored check is an unknown, not a near-pass. Left to the ratio,
        # three passes and one error clear any floor up to 0.75 and the action
        # is allowed under CLOSED too, so an error flags on its own.
        weak = coverage.ratio < self.min_coverage or bool(coverage.errored)

        if any(r.status is CheckStatus.FAIL for r in results):
            verdict = Verdict.BLOCK
        elif weak and self.unknown_policy is UnknownPolicy.CLOSED:
            verdict = Verdict.BLOCK
        else:
            verdict = Verdict.ALLOW

        return Decision(
            action_id=action.action_id,
            verdict=verdict,
            mode=self.mode,
            checks=results,
            coverage=coverage,
            weakly_verified=weak,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
