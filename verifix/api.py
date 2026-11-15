"""The sidecar HTTP surface.

This is the thing that makes VeriFix installable rather than embedded. Any
agent, in any language, can call POST /verify before it executes an action.
"""

from __future__ import annotations

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
    #: Optional: callers that hold their own state can pass it in instead of
    #: relying on the configured provider. Useful for offline replay.
    live_state: NetworkState | None = None
    #: Needed only for CHECK_ONCE with caller-supplied state. Without it a
    #: CHECK_ONCE request errors rather than checking the live state.
    proposal_state: NetworkState | None = None


def _gate_for(req: VerifyRequest) -> Gate:
    if req.live_state is None:
        if _gate is None:
            # Verifying against an invented empty state would return a
            # decision that looks real and means nothing.
            raise HTTPException(
                status_code=503,
                detail="no state provider configured and no live_state supplied",
            )
        return _gate
    provider = StaticStateProvider(req.live_state, at_proposal=req.proposal_state)
    if _gate is None:
        return Gate(state_provider=provider, mode=GateMode.REVALIDATE)
    # Carry over every setting, not just mode and checks: dropping
    # unknown_policy would silently turn a fail-closed gate open.
    return Gate(
        state_provider=provider,
        mode=_gate.mode,
        checks=_gate.checks,
        unknown_policy=_gate.unknown_policy,
        min_coverage=_gate.min_coverage,
    )


@app.post("/verify", response_model=Decision)
def verify(req: VerifyRequest) -> Decision:
    return _gate_for(req).verify(req.action)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "mode": _gate.mode.value if _gate else "unconfigured"}
