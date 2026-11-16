"""Core data types exchanged across the VeriFix boundary.

Everything that crosses the sidecar's API is defined here. Keeping these
environment-agnostic is what lets the same gate run against any
StateProvider, Mininet or otherwise.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Verdict(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"


class GateMode(str, Enum):
    """The three evaluation conditions.

    OFF          - no verification. Baseline 1.
    CHECK_ONCE   - verify against the state captured at proposal time. Baseline 2.
    REVALIDATE   - verify against live state read at execution time, and only
                   that. There is no separate proposal-time pass. Design
                   rationale, not a measured result: state binding detects
                   changes to what the decision was bound to, so a second
                   pass was expected to add latency for little benefit.
                   Binding only sees what it was bound to: a premise left out
                   of `depends_on` can change unseen (Case 5, target only).

    REVALIDATE's catches over CHECK_ONCE come from two sources, and the paper
    must keep them apart: (1) the same schema-aware checks re-run on newer
    state, and (2) the live-only checks (freshness, state binding), which have
    nothing to say in CHECK_ONCE. Per-check status on every Decision is what
    lets results attribute a block to one or the other.
    """

    OFF = "off"
    CHECK_ONCE = "check_once"
    REVALIDATE = "revalidate"


class NetworkState(BaseModel):
    """A snapshot of the network at a moment in time."""

    captured_at: float = Field(..., description="Unix timestamp of capture")
    links: dict[str, bool] = Field(
        default_factory=dict, description="link id -> up/down"
    )
    paths: dict[str, list[str]] = Field(
        default_factory=dict,
        description="path id -> ordered list of link ids (keys of `links`)",
    )
    metrics: dict[str, float] = Field(
        default_factory=dict, description="free-form telemetry, e.g. loss, latency"
    )

    def digest(self, keys: list[str] | None = None) -> str:
        """Stable hash of the state, used to bind a decision to the state it saw.

        With no keys, hashes all links and paths. With keys ("links:<id>" or
        "paths:<id>"), hashes only those entries, so a decision can be bound
        to the part of the network it rested on rather than to all of it. A
        key absent from the state hashes as absent, so a link that disappears
        changes the digest instead of dropping out of it.
        """
        import hashlib
        import json

        if keys is None:
            payload: dict[str, Any] = {"links": self.links, "paths": self.paths}
        else:
            payload = {key: self._lookup(key) for key in keys}
        encoded = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(encoded.encode()).hexdigest()[:16]

    def _lookup(self, key: str) -> Any:
        section, _, name = key.partition(":")
        if section not in ("links", "paths") or not name:
            # A key we cannot resolve must not hash to something stable, or a
            # typo would bind the decision to nothing and always match.
            raise ValueError(f"unresolvable state key {key!r}")
        return getattr(self, section).get(name)


class ProposedAction(BaseModel):
    """An action an agent wants to take, plus the state it based that on."""

    action_id: str
    kind: str = Field(..., description="e.g. reroute, drop_rule, restart_iface")
    params: dict[str, Any] = Field(default_factory=dict)
    proposed_at: float
    state_digest: str | None = Field(
        None, description="digest of the state the agent saw when deciding"
    )
    depends_on: list[str] | None = Field(
        None,
        description=(
            "state keys the decision rested on, e.g. 'links:s1-s3'. When set, "
            "state_digest is NetworkState.digest(depends_on); when absent, it "
            "is the whole-state digest."
        ),
    )
    rationale: str | None = None


class CheckStatus(str, Enum):
    """Four outcomes, not two.

    The distinction that matters on an unfamiliar environment is PASS versus
    NOT_APPLICABLE. A check that cannot evaluate an action has not approved it.
    Collapsing those two is how a verifier silently stops verifying.
    """

    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    ERROR = "error"


class UnknownPolicy(str, Enum):
    """What to do when coverage is too thin to be meaningful.

    OPEN   - allow, but mark the decision as weakly verified (observability
             first; the right default while you are measuring).
    CLOSED - block anything the gate could not meaningfully check (the right
             default in production).
    """

    OPEN = "open"
    CLOSED = "closed"


class CheckResult(BaseModel):
    name: str
    status: CheckStatus
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.status is CheckStatus.PASS


class Coverage(BaseModel):
    """How much of the check set could actually run on this action.

    Report this in the paper. "Our gate blocked 40% of harmful actions" means
    very little without "and it was able to evaluate 90% of them".
    """

    evaluated: int = 0
    total: int = 0
    skipped: list[str] = Field(default_factory=list)
    errored: list[str] = Field(default_factory=list)

    @property
    def ratio(self) -> float:
        return self.evaluated / self.total if self.total else 0.0


class Decision(BaseModel):
    """What the sidecar returns. The reason field is what makes it debuggable."""

    action_id: str
    verdict: Verdict
    mode: GateMode
    checks: list[CheckResult] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)
    weakly_verified: bool = Field(
        False, description="True when coverage fell below the configured floor"
    )
    latency_ms: float = 0.0

    @property
    def reason(self) -> str:
        # OFF must not read like a thin-coverage allow: "didn't check" and
        # "checked a little" are different conditions in the results.
        if self.mode is GateMode.OFF:
            return "verification disabled"
        failed = [c for c in self.checks if c.status is CheckStatus.FAIL]
        if failed:
            return "; ".join(f"{c.name}: {c.reason}" for c in failed)
        if self.weakly_verified:
            return (
                f"weakly verified: only {self.coverage.evaluated}/"
                f"{self.coverage.total} checks applied "
                f"(skipped: {', '.join(self.coverage.skipped) or 'none'})"
            )
        return f"{self.coverage.evaluated}/{self.coverage.total} checks passed"
