"""The sidecar HTTP surface.

This is the thing that makes VeriFix installable rather than embedded. Any
agent, in any language, can call POST /verify before it executes an action.

Who supplies the state matters more than anything else here. With a provider
configured (experiments/serve.py), the gate reads state itself and refuses
state sent by the caller: the caller may be the agent under verification, and
an agent that picks the state it is judged against can always pass. The agent
gets its proposal-time state from POST /snapshot, and /verify holds the
action to that snapshot: its `proposed_at` and `state_digest` must be the
ones the gate recorded for its id. Otherwise an agent could bind to a later
snapshot taken under another id, or date its proposal in the future, and
pass state binding and freshness on its own say-so.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from verifix.adapters.base import StaticStateProvider
from verifix.gate import Gate
from verifix.models import Decision, GateMode, NetworkState, ProposedAction

app = FastAPI(title="VeriFix", version="0.1.0")

# Replaced at startup by the adapter for the environment in use.
_gate: Gate | None = None


def configure(gate: Gate | None) -> None:
    global _gate
    _gate = gate


class VerifyRequest(BaseModel):
    action: ProposedAction
    #: Accepted only when the server has no provider (tests, offline use).
    #: With one configured, any caller state is refused with a 400.
    live_state: NetworkState | None = None
    #: Same rule. Without a provider the gate runs REVALIDATE, so this is
    #: carried for the request shape but has no CHECK_ONCE to feed.
    proposal_state: NetworkState | None = None


class SnapshotRequest(BaseModel):
    action_id: str


class SnapshotResponse(BaseModel):
    action_id: str
    #: Whole-state digest. An agent binding to declared keys computes their
    #: digest from `state` with NetworkState.digest(keys).
    digest: str
    state: NetworkState


def _gate_for(req: VerifyRequest) -> Gate:
    if _gate is not None:
        if req.live_state is not None or req.proposal_state is not None:
            # Refused, not ignored: silently dropping the caller's state would
            # hide from the caller that its request was misunderstood.
            raise HTTPException(
                status_code=400,
                detail="this server reads state itself; caller-supplied state is refused",
            )
        _hold_to_snapshot(_gate, req.action)
        return _gate
    if req.live_state is None:
        # Verifying against an invented empty state would return a decision
        # that looks real and means nothing.
        raise HTTPException(
            status_code=503,
            detail="no state provider configured and no live_state supplied",
        )
    provider = StaticStateProvider(req.live_state, at_proposal=req.proposal_state)
    return Gate(state_provider=provider, mode=GateMode.REVALIDATE)


def _hold_to_snapshot(gate: Gate, action: ProposedAction) -> None:
    """Refuse an action whose binding fields are not the gate's own record.

    Only for providers that take snapshots: one that cannot has no record to
    hold the action to, and its state is the operator's, not the caller's.
    """
    if getattr(gate.state_provider, "snapshot", None) is None:
        return
    try:
        recorded = gate.state_provider.state_at_proposal(action)
    except LookupError:
        # Without the gate's record, proposed_at and state_digest are the
        # caller's word alone. Refused rather than judged unbound: an agent
        # could otherwise skip /snapshot and set its own clock.
        raise HTTPException(
            status_code=409,
            detail=f"no snapshot for action {action.action_id!r}; "
                   f"call /snapshot first",
        ) from None
    if action.proposed_at != recorded.captured_at:
        raise HTTPException(
            status_code=400,
            detail="proposed_at differs from the snapshot taken for this action",
        )
    if action.state_digest is None:
        return  # declining to bind is the gate's to flag, not this layer's
    try:
        expected = recorded.digest(action.depends_on)
    except ValueError:
        # A malformed key: state binding errors on it in the gate, and the
        # decision is flagged there. Nothing here to compare against.
        return
    if action.state_digest != expected:
        raise HTTPException(
            status_code=400,
            detail="state_digest is not the digest of the snapshot taken for "
                   "this action over its depends_on",
        )


@app.post("/verify", response_model=Decision)
def verify(req: VerifyRequest) -> Decision:
    return _gate_for(req).verify(req.action)


@app.post("/snapshot", response_model=SnapshotResponse)
def snapshot(req: SnapshotRequest) -> SnapshotResponse:
    if _gate is None:
        raise HTTPException(status_code=503, detail="no state provider configured")
    # Not every provider can hold proposal-time state; a static one cannot
    # read anything new, so a snapshot from it would be a fiction.
    take: Any = getattr(_gate.state_provider, "snapshot", None)
    if take is None:
        raise HTTPException(
            status_code=501, detail="the configured state provider takes no snapshots"
        )
    try:
        state: NetworkState = take(req.action_id)
    except ValueError as exc:
        # A repeated id would otherwise move that action's proposal state.
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return SnapshotResponse(action_id=req.action_id, digest=state.digest(), state=state)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "mode": _gate.mode.value if _gate else "unconfigured"}
