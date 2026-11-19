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

An agent proposes an action based on the network as it was when it looked. By
the time the action runs, the network may have moved. VeriFix re-reads state at
execution and checks the action against what is true then, reporting how much
of the check set it was able to apply.

Pre-execution revalidation is established prior art (TOCTOU-Bench, DYN-WEB,
Commit-Time Authorization, "Stale Does Not Mean Unsafe"). This implementation
occupies one point in that design space — a digest over whole or declared
state, plus freshness — and measures that point's costs on hand-built cases,
three of them on a small Mininet topology.

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

On a Linux machine with Mininet, `sudo python demo/run_demo.py --mininet` adds
Cases 3–5 on a real three-switch topology. Mininet is a system package,
not a pip dependency; the reference environment is Ubuntu 22.04 under WSL2
with apt's `mininet` and `openvswitch-switch`.

## Run as a service

Against a live network, the gate reads state itself:

```bash
sudo ~/verifix-venv/bin/python -m experiments.serve            # Linux, Mininet
```

This builds the Mininet triangle, gives the gate a `MininetStateProvider`,
and serves on `127.0.0.1:8080` (`--mode`, `--unknown-policy`, `--host`,
`--port`). The agent asks the gate for its proposal-time state and binds its
action to the digest it gets back, so the agent never supplies state:

```bash
curl -X POST localhost:8080/snapshot -H 'content-type: application/json' \
     -d '{"action_id": "a-001"}'     # -> {"action_id", "digest", "state"}
curl -X POST localhost:8080/verify -H 'content-type: application/json' -d '{
  "action": {"action_id": "a-001", "kind": "reroute",
             "params": {"target_path": "backup"},
             "proposed_at": <state.captured_at>, "state_digest": "<digest>"}}'
```

With a provider configured, a `/verify` request that carries `live_state` or
`proposal_state` gets a 400: an agent that sends the state it is judged
against can always pass. A repeated snapshot id gets a 409. `/verify` also
holds the action to the snapshot taken for its id: no snapshot gets a 409,
and a `proposed_at` or `state_digest` that differs from the gate's record
(for example, a digest from a later snapshot taken under another id) gets a
400. An action with no digest, or a malformed `depends_on` key, still reaches
the gate, which flags it.

Started bare (`uvicorn verifix.api:app --port 8080`), the service has no
provider. Then each request must carry `live_state`, the gate runs
`revalidate`, and `/snapshot` returns 503. A request with no state gets a 503
rather than a verdict computed against an invented state. This mode trusts the
caller and is for tests and offline use:

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
   and the remaining checks still run. Any error marks the decision weakly
   verified whatever the coverage ratio, so `unknown_policy` decides it.

The live-only checks, freshness and state binding, are schema-agnostic: they
need only a timestamp and a state digest, so they work on any action from any
environment. Feasibility is schema-aware and degrades to `not_applicable`.

State binding has two scopes. If the action lists the state keys its decision
rested on (`depends_on`, e.g. `["paths:backup", "links:r3-r4"]`), only those
are compared, so an unrelated link flap does not block it. If it lists nothing,
the whole state is compared, which works for any agent but blocks on any
change. The scope is recorded on every result. Declared binding is only as
good as the declaration: if it names what the action acts on but not why it was
chosen, a change to the premise passes unseen. An action with no digest at all,
or an empty `depends_on`, gets `not_applicable` and the decision is flagged
`weakly_verified` whatever the coverage ratio, so declining to bind cannot
pass unnoticed. A malformed key is an `error`, which flags the same way.

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
├── api.py             POST /verify, POST /snapshot
├── checks/
│   ├── base.py        the Check interface — add a file to add a check
│   ├── freshness.py   staleness + state binding (live-only)
│   └── feasibility.py feasibility + policy (shared with the baseline)
└── adapters/
    ├── base.py        StateProvider — the environment boundary
    └── mininet.py     reads a running Mininet net; never imports Mininet
experiments/
├── cases.py           Cases 1–5, the single definition
├── run.py             one CSV row per case, variant and mode
├── serve.py           the HTTP server on a live Mininet triangle
├── agent.py           an LLM (Groq) proposes remediations; the gate judges them
├── inject.py          seeded link churn between proposal and execution
├── tradeoff.py        false blocks vs silent misses per binding scope
└── analyse.py         Table 2 from results/mininet-cases-wsl.csv
demo/run_demo.py       prints a narrative subset of the same cases
results/               research data, append-only
```

The gate never imports an environment. Write a `StateProvider` and the same
gate and checks run against it. The only environment adapter is
`adapters/mininet.py`.

## Experiments

```bash
python -m experiments.run                    # Cases 1–2, CSV to stdout
sudo .venv/bin/python -m experiments.run --mininet --out results/mininet-cases-wsl.csv
python -m experiments.analyse                # Table 2
```

Run from the repository root. `--mininet` needs Linux, root and Mininet, and
stops with an error if Mininet is missing rather than quietly running two
cases.

`experiments/agent.py` puts an LLM in the proposer's seat (needs
`pip install groq` and `GROQ_API_KEY`; under sudo, use `sudo -E`). Each
trial takes s1-s2 down, asks the model for one action, applies one drift
(none, target drops, premise recovers) and records all three modes, with and
without the action vocabulary in the prompt. What the model declares in
`depends_on` is recorded as given.

```bash
sudo -E ~/verifix-venv/bin/python -m experiments.agent --trials 1 --out results/agent-groq-wsl.csv
```

With `--evidence reads` the state is not in the prompt: the model reads it
through tools (`get_link`, `get_path`) and submits through a `submit` tool.
The harness logs which keys were read; that list is the inferred dependency
set. Each action is then judged bound to what it declared, to what it read,
and to the whole state, on the same drift. Its rows have extra columns, so
they go to their own file:

```bash
sudo -E ~/verifix-venv/bin/python -m experiments.agent --evidence reads --trials 1 --out results/agent-inferred-wsl.csv
```

`experiments/inject.py` replaces the single scripted drift with churn: from
Case 5's fault, the reroute is proposed under three binding scopes, then
every link toggles as a seeded Poisson process for a set delay before the
gate decides. The grid is delays 0.5–8 s (8 s is past the freshness budget)
by rates 0–0.2 toggles per link per second. Time is simulated (the
provider's clock is advanced, nothing sleeps); the link changes are real.
Each row logs the events and which links ended changed, not whether the
action was stale.

```bash
sudo ~/verifix-venv/bin/python -m experiments.inject --repeats 1 --out results/injection-wsl.csv
```

`experiments/tradeoff.py` labels those rows and counts each scope's cost.
An action is stale if its premise is gone (s1-s2 back up) or its target is
infeasible (a backup link down); an off-path change does not make it stale.
A false block is a block on a sound action, a silent miss an allow on a
stale one. Counts are printed below the freshness budget and over all
delays. `--plot` draws the figure and needs matplotlib (the `plot` extra);
the tables need nothing extra.

```bash
python -m experiments.tradeoff --plot        # results/tradeoff-wsl.png
```

## Status

The gate, checks, Mininet adapter, HTTP API and evaluation tooling are built
and tested. Cases 1–2 use static state; Cases 3–5 run on one Mininet topology
under WSL2. The two
network-operations agent benchmarks examined (NetArena, NIKA) don't exercise
state-changing actions with proposal-time state; LLM-proposed actions come
from `experiments/agent.py` instead, on the same topology.
