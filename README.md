# VeriFix

A verification sidecar for AI agents that act on networks.

An agent proposes an action. VeriFix checks it against the **live** state of the
network and returns allow or block with a reason. The controller executes only
what passes.

```
agent  ──proposes──▶  VeriFix  ──allow──▶  controller ──▶ network
                         │
                         └──block──▶  reason returned to agent
```

## Why

Existing work validates an agent's proposed fix once, at proposal time. But the
network keeps moving. A fix that was correct when it was checked can be wrong —
or harmful — by the time it runs. VeriFix re-validates against live state at the
moment of execution, and measures how often that matters.

## Quickstart

```bash
pip install -e .
python demo/run_demo.py
```

The demo proposes a reroute onto a backup path, drops that path between proposal
and execution, and shows the three modes disagreeing:

```
  off          ALLOW cov 0/0  verification disabled
  check_once   ALLOW cov 2/2  2/2 checks passed
  revalidate   BLOCK cov 4/4  feasibility: path 'backup' has down links: r3-r4; state_binding: whole-state: state changed since proposal (...)
```

On a Linux machine with Mininet, `sudo python demo/run_demo.py --mininet` runs
the same scenario on a real three-switch topology. Mininet is a system package,
not a pip dependency; the reference environment is the official Mininet VM
image.

## Run as a service

```bash
uvicorn verifix.api:app --port 8080
```

Started bare, the service has no state provider, so each request must carry
`live_state` (and `proposal_state`, for `check_once`). A request with neither
gets a 503 rather than a verdict computed against an invented state.

```bash
curl -X POST localhost:8080/verify -H 'content-type: application/json' -d '{
  "action": {
    "action_id": "a-001",
    "kind": "reroute",
    "params": {"target_path": "backup"},
    "proposed_at": 1763200000.0,
    "state_digest": "13677c7492054654"
  },
  "live_state": {
    "captured_at": 1763200002.0,
    "links": {"r1-r2": true, "r3-r4": false},
    "paths": {"primary": ["r1-r2"], "backup": ["r3-r4"]}
  }
}'
```

## Modes

The three evaluation conditions are one flag, not three systems.

| Mode | Behaviour | Role |
|---|---|---|
| `off` | no verification | baseline |
| `check_once` | validate against state at proposal time | baseline |
| `revalidate` | validate against live state at execution | VeriFix |

`revalidate` catches more than `check_once` for two separate reasons: the same
feasibility and policy checks run on newer state, and the live-only checks
(freshness, state binding) run at all. Every decision carries per-check status,
so results can attribute each block to one source or the other.

## Unfamiliar environments

VeriFix is meant to run against networks, agents and action vocabularies it has
never seen. Three rules make that safe:

1. **A check that cannot evaluate an action does not approve it.** Results are
   `pass` / `fail` / `not_applicable` / `error`, never a silent pass.
2. **Every decision reports coverage** — how many checks actually applied. A
   verdict of `allow` with 2/6 coverage is a different claim from `allow` with
   6/6, and the API says which one you got.
3. **A check that raises cannot take the gate down.** It is recorded as `error`
   and the remaining checks still run.

The live-only checks, freshness and state binding, are schema-agnostic: they
need only a timestamp and a state digest, so they work on any action from any
environment. Feasibility is schema-aware and degrades to `not_applicable`.

State binding has two scopes. If the action lists the state keys its decision
rested on (`depends_on`, e.g. `["paths:backup", "links:r3-r4"]`), only those
are compared, so an unrelated link flap does not block it. If it lists nothing,
the whole state is compared, which works for any agent but blocks on any
change. The scope is recorded on every result. Declared binding needs the
agent's cooperation, so replayed traces fall back to whole-state or, with no
digest at all, `not_applicable`.

When coverage falls below `min_coverage`, the decision is flagged
`weakly_verified` and `unknown_policy` decides what to do with it:

```python
Gate(provider, unknown_policy=UnknownPolicy.OPEN)    # allow, but flag it
Gate(provider, unknown_policy=UnknownPolicy.CLOSED)  # block what you can't check
```

```
unknown=open    ALLOW  cov 2/4  weak=True
                weakly verified: only 2/4 checks applied (skipped: feasibility, state_binding)
unknown=closed  BLOCK  cov 2/4  weak=True
```

Report coverage alongside catch rate. "Blocked 40% of harmful actions" means
little without "and could evaluate 90% of them."

## Layout

```
verifix/
├── models.py          shared types across the API boundary
├── gate.py            runs checks according to mode
├── api.py             POST /verify
├── checks/
│   ├── base.py        the Check interface — add a file to add a check
│   ├── freshness.py   staleness + state binding (live-only)
│   └── feasibility.py feasibility + policy (shared with the baseline)
└── adapters/
    ├── base.py        StateProvider — the environment boundary
    └── mininet.py     reads a running Mininet net; never imports Mininet
```

The gate never imports an environment. Write a `StateProvider` and the same
gate, checks and results pipeline run anywhere:

- `adapters/mininet.py` — development and demo
- `adapters/netarena.py` — the shared benchmark, for published comparisons
- `adapters/replay.py` — offline replay of recorded agent traces

## Status

Early. Core gate, check set, the Mininet adapter and tests are in place. The
Mininet adapter is tested against a stubbed network; its real-topology demo
runs on the Linux VM. NetArena and replay adapters are next.
