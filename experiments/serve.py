"""Run the VeriFix HTTP server against a live Mininet triangle.

    sudo ~/verifix-venv/bin/python -m experiments.serve            # revalidate
    sudo ~/verifix-venv/bin/python -m experiments.serve --mode check_once

Run from the repository root, as root, on Linux with Mininet. It builds the
topology from experiments/cases.py, hands MininetStateProvider to the gate,
and serves verifix.api. From then on the gate reads state itself and refuses
caller-supplied state; agents take proposal-time state from POST /snapshot.

The launcher lives here, not in verifix/, so the package never imports an
environment.
"""

from __future__ import annotations

import argparse
from typing import Any

from experiments.cases import MININET_PATHS, build_triangle
from verifix import api
from verifix.adapters.mininet import MininetStateProvider
from verifix.gate import Gate
from verifix.models import GateMode, UnknownPolicy


def make_gate(net: Any, mode: GateMode = GateMode.REVALIDATE,
              unknown_policy: UnknownPolicy = UnknownPolicy.OPEN) -> Gate:
    """The served gate for a net with Mininet's link API. Split out for tests."""
    return Gate(state_provider=MininetStateProvider(net, MININET_PATHS),
                mode=mode, unknown_policy=unknown_policy)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=[m.value for m in GateMode],
                        default=GateMode.REVALIDATE.value)
    parser.add_argument("--unknown-policy", choices=[p.value for p in UnknownPolicy],
                        default=UnknownPolicy.OPEN.value)
    # Loopback by default: the server runs as root next to the network it
    # judges, and nothing here authenticates callers.
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    # Imported here so the module loads (and make_gate is testable) without
    # uvicorn's server stack being exercised.
    import uvicorn

    net = build_triangle()
    net.start()
    try:
        api.configure(make_gate(net, GateMode(args.mode),
                                UnknownPolicy(args.unknown_policy)))
        uvicorn.run(api.app, host=args.host, port=args.port)
    finally:
        # Left running, the topology's interfaces and OVS bridges outlive the
        # server and break the next run.
        api.configure(None)
        net.stop()


if __name__ == "__main__":
    main()
