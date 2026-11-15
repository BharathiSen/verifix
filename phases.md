# VeriFix — end-to-end build prompt

## Status

Update this section whenever a phase completes. It is the handoff between
sessions.

**Current phase:** Phase 2 (NetArena), not started. It opens with the
investigate-and-report step, as written, before any code. Read the "two
working copies" note below first.

| Phase | State | Notes |
|---|---|---|
| 0. Skeleton | done | models, gate, api, 4 checks, StaticStateProvider, demo. Verified 2026-11-15 on Python 3.11.9 after `pip install -e .`: 5 passed, demo runs (revalidate blocks, check_once allows). |
| 1. Mininet adapter | done | 2026-11-15, Python 3.11.9 on Windows: 31 passed (14 adapter, 17 gate), static demo cases run. `MininetStateProvider` reads a duck-typed `net` and never imports Mininet; only the demo's `--mininet` case does. Case 4 run by the user on the Linux VM (official Mininet VM image, not WSL: user decision 2026-11-15): check_once allows, revalidate blocks, under both binding scopes. Also landed: five review fixes, `depends_on`, REVALIDATE wording. See notes. Addendum, 2026-11-15: Case 5 (primary recovers) added to `--mininet`, plus a 3-scope parametrized adapter test; 34 passed. Case 5 then ran on the VM (2026-11-15, `sudo .venv/bin/python demo/run_demo.py --mininet`, output in `results/phase1-demo-2026-11-15.txt` on the VM) and matched the fake-net run line for line: whole-state and premise+target revalidate BLOCK, target-only ALLOW, feasibility PASS in all three. The VM's `.venv` is built with `--system-site-packages`, which is how it sees the system Mininet. |
| 2. NetArena (go/no-go) | not started | |
| 3. Trace replay | not started | |
| 4. Experiments | not started | |
| 5. Paper artifacts | not started | only on explicit instruction |

**Carried-forward notes**

- 2026-11-15, two working copies (standing rule, every phase from here).
  Code is written on the Windows checkout. Mininet, and later the experiment
  runs, execute on the Linux VM. Both are clones of the same GitHub repo.
  Code reaches the VM through GitHub, but results never came back, and Phase
  4's `analyse.py` reads `results/runs.csv`, every row of which is generated
  on the VM. Rule: results flow back through git too. The VM commits and
  pushes `results/` after each run, and the Windows checkout pulls before any
  session that reads results. `results/` is deliberately not git-ignored. Keep
  each side to one writer so pushes never conflict: the VM writes only
  `results/` and never edits code; Windows never writes `results/`. A code fix
  found on the VM is made on Windows and pulled. `runs.csv` stays append-only
  with a single writer, the VM. A session that needs a result not yet in
  this checkout says so and asks; it does not reason from a result it has not
  seen. The first sync worked: the VM pushed
  `results/phase1-demo-2026-11-15.txt` in `21cfb17`, and Windows pulled it.
- 2026-11-15: `phases.md` is now tracked in git (was git-ignored, so it existed
  on one machine with no backup). It is research record: decisions, findings,
  phase history. `CLAUDE.md` is still ignored, pending the user's call.

- 2026-11-15, binding must earn its keep (user decision; Phase 2 requirement).
  Every case shown so far (demo Cases 1 and 4, the adapter tests) has
  feasibility and state binding failing together: the target path went down,
  so feasibility catches it alone and binding adds nothing. A reviewer will
  ask why state binding is needed at all, and there is no answer on paper.
  Binding is justified only where the path is still feasible but the decision
  is wrong: (1) the primary recovered, so the reroute is unnecessary;
  (2) congestion shifted, so the backup is now worse than the primary;
  (3) another flow moved onto the backup, so the reroute oversubscribes it.
  **Requirement: at least one scenario where feasibility PASSes and state
  binding BLOCKs.** Resolved early: user decision to build it in Phase 1 as
  demo Case 5 (scenario 1) rather than leave the headline claim resting on
  NetArena. If Phase 2 can produce such scenarios too, they go in Table 1;
  Case 5 is the guaranteed demonstration, not a replacement.
- 2026-11-15, paper scope (user decision): **VeriFix binds decisions to
  topological state, meaning link and path status. Load-sensitive premises
  (scenarios 2 and 3) are out of scope.** Do not digest metrics, raw or
  bucketed. Raw values change on every read and would block everything.
  Bucketing adds a third unjustified constant, and it blocks a change that
  crosses a boundary (49% to 51%) while passing a larger one that doesn't
  (10% to 49%). The real blocker is upstream: nothing in the pipeline
  measures link load, and Mininet would need traffic generation plus counter
  polling. Producing load data is a project; hashing it is not the hard
  part. State the scope plainly in the paper as a limitation.
- Named future extension, for the paper's discussion section; record it, do
  not build it: move binding from "did the state change?" to "does the
  premise still hold?". The agent declares predicates (`util(s1-s3) < 0.8`)
  instead of keys, and binding re-evaluates them against live state. There is
  no quantization and no constant of ours, because the threshold comes from
  the agent's own reasoning. Topological keys are the special case of a
  predicate.
- Finding, for the paper rather than a limitation to bury: **declaring the
  premise vs. declaring the target.** In Case 5, declared binding catches
  "primary recovered" only if `depends_on` includes the premise the agent
  acted on (`links:s1-s2`, the down primary). Declaring only what it acts
  toward (the backup) gives PASS, so revalidate allows the now-unwarranted
  reroute. An agent that declares what it touches gets a silently weaker gate
  than one that declares why it decided. Whole-state binding catches it, at
  the false-block cost shown in Case 3. Phase 2 and 4 agent prompts must ask
  for premises, and results should record which kind of declaration was made
  where it can be told apart.

- 2026-11-15: `Decision.reason` now returns "verification disabled" in OFF
  mode (was "0/0 checks passed"); test added, 6 passed.
- Phase 4 hazard: `analyse.py` must split rows on `mode`, never on `coverage`
  or `weakly_verified`. OFF decisions return before coverage is computed, so
  they carry `coverage=0/0, weakly_verified=False`, and a "properly verified"
  filter on `weakly_verified` would silently include every OFF row.
- README demo output refreshed in Phase 1. The README curl example now
  carries `live_state`: a bare server returns 503 instead of judging against an
  invented empty state.
- 2026-11-15, Phase 1 review fixes: feasibility FAILs a path over a link absent
  from state (was treated as up; invariant 2) and skips when state has no
  links; `paths` documented as link ids; `FeasibilityCheck.known_kinds` and
  `PolicyCheck.forbidden_kinds` are per-instance `__init__` params (`is None`,
  not `or`); `api.py` overrides keep `unknown_policy`/`min_coverage`;
  `StaticStateProvider` and `MininetStateProvider` raise on a missing proposal
  state instead of falling back to live (that fallback would turn CHECK_ONCE
  into REVALIDATE).
- 2026-11-15, state binding scope (user decision): optional
  `ProposedAction.depends_on`, keys `links:<id>` / `paths:<id>`. Present: digest
  over those keys only (`NetworkState.digest(keys)`; absent keys hash as absent,
  bad keys raise, empty list is NOT_APPLICABLE). Absent: whole-state digest,
  unchanged, so existing digests still match. The scope is in the check's
  reason on PASS and FAIL. Invariant 6 text updated in CLAUDE.md and here.
  Phase 4: add a `binding` column (whole / declared / none) to `runs.csv`, and
  to compare scopes on the same actions the harness must produce both digests
  at proposal time; stripping `depends_on` afterwards is not enough.
  Paper framing: declared binding needs agent cooperation, so NIKA replay
  degrades to whole-state or NOT_APPLICABLE; state that as a limitation.
- REVALIDATE checks live state only; no proposal-time pass. Its catches over
  CHECK_ONCE have two sources (schema-aware checks on newer state; live-only
  checks), documented in `GateMode`. Attribute blocks per-check in Phase 4.
- Open from the Phase 1 review, not fixed: (a) freshness subtracts the agent's
  clock from the adapter's, and negative age (skew) passes silently;
  (b) coverage totals differ by mode (check_once 2, revalidate 4), so ratios are
  not comparable across modes in Table 1; (c) the state-provider-failure path
  reports `total=len(self.checks)` regardless of mode and lists
  `state_provider` as errored though it is not a check; (d) `Decision.reason`
  hides ERRORs when nothing FAILed.
- Revalidation narrows the TOCTOU window but does not close it: there is still
  a gap between the gate's check and execution. The Phase 1 demo does not
  execute actions, so it cannot measure that gap; measure it in Phase 2 where
  NetArena executes.
- Placeholder thresholds, not settled defaults: `MAX_DECISION_AGE_S = 5.0`
  (checks/freshness.py) and `DEFAULT_MIN_COVERAGE = 0.5` (gate.py) were picked
  without data. Derive the staleness budget from measured
  proposal-to-execution latency in Phase 2, and `min_coverage` from the
  observed coverage distribution in Phase 3. Do not cite either in the paper
  until it has that justification.
- Coverage may be inflated: `PolicyCheck` is schema-agnostic, so it PASSes and
  counts as evaluated on any action, including ones nothing else can model.
  Before Phase 4, decide whether to report coverage raw or to separate
  substantive evaluations from trivial passes. Substance belongs to the
  result, not the check: policy PASS on a kind it has an opinion about is
  meaningful, on an unknown kind it is not; freshness is schema-agnostic yet
  always substantive. If the metric is split, the flag goes on `CheckResult`.

---

Paste everything below into a fresh Claude session with the `verifix/` skeleton
attached or present in the working directory.

---

You are a senior systems engineer. We are completing **VeriFix**, a verification
sidecar for AI agents that act on networks. This is a research project with a
paper as the output, so architectural discipline matters more than speed.

## The claim the whole system exists to support

Existing work validates an agent's proposed fix **once**, at proposal time. The
network keeps moving. A fix that was correct when checked can be wrong — or
harmful — by the time it executes. VeriFix re-validates against **live state at
the moment of execution**, and measures how often that gap matters.

This is time-of-check-to-time-of-use. Every design decision serves it. If a
feature does not help prove or disprove that claim, it does not belong.

Related work we are differentiating from, cited as context, never as rows in our
results table: `draft-howard-virp` (signed observations, intent gating, outcome
verification), `draft-smith-opsawg-ai-network-governance` (governance framework,
no-bypass guardrail layer), Spice (runtime policy layer for agent tool calls),
TwinGridShield (consequence-aware runtime authorization). All of them check
once. None re-validate at execution. None report coverage.

## What already exists

A working skeleton. Read every file before writing anything.

```
verifix/
├── README.md
├── pyproject.toml
├── verifix/
│   ├── models.py          ProposedAction, NetworkState, CheckResult,
│   │                      CheckStatus, Coverage, Decision, GateMode,
│   │                      UnknownPolicy
│   ├── gate.py            runs checks per mode, accounts coverage, contains errors
│   ├── api.py             POST /verify, GET /healthz
│   ├── checks/
│   │   ├── base.py        Check ABC; _ok / _fail / _skip
│   │   ├── freshness.py   FreshnessCheck, StateBindingCheck  (the contribution)
│   │   └── feasibility.py FeasibilityCheck, PolicyCheck      (shared baseline)
│   └── adapters/
│       └── base.py        StateProvider ABC, StaticStateProvider
├── demo/run_demo.py
└── tests/test_gate.py     5 tests, all passing
```

## Architecture invariants — do not violate these

These are what make the project defensible. Breaking one silently destroys a
claim in the paper.

1. **The gate never imports an environment.** No Mininet, NetArena or trace
   import may appear in `gate.py`, `checks/` or `models.py`. All environment
   access goes through `StateProvider`.
2. **`NOT_APPLICABLE` is not `PASS`.** A check that cannot evaluate an action
   must return `_skip()`. A verifier that silently passes what it did not check
   is the failure mode this design exists to prevent. Never "return True to be
   safe."
3. **Every `Decision` carries honest coverage.** `evaluated`, `total`,
   `skipped`, `errored`. Coverage is a reported metric in the paper, not
   debug output.
4. **The three evaluation conditions are one flag, not three implementations.**
   `GateMode.OFF`, `CHECK_ONCE`, `REVALIDATE`. If you ever find yourself writing
   a separate "baseline gate", stop — you have broken the comparison.
5. **A check that raises must not take the gate down.** Catch, record as
   `ERROR`, continue. Environments will surprise us.
6. **Schema-agnostic checks must stay schema-agnostic.** `FreshnessCheck` and
   `StateBindingCheck` may read only `proposed_at`, `state_digest`,
   `depends_on` (as an opaque key list) and `state.captured_at`, plus
   `NetworkState.digest()`. They must never touch `action.params` or read
   `state.paths` directly — that portability is why the system runs on environments it
   was not built for.
7. **Fail loudly on unknowns, never silently.** `weakly_verified` plus
   `unknown_policy` is the mechanism. Keep it.

## Build order

Do these in sequence. Do not start a phase before the previous one runs and its
tests pass. Report progress after each phase.

### Phase 1 — Mininet adapter
`verifix/adapters/mininet.py`

Implement `StateProvider` against the existing Mininet setup. `live_state()`
reads the topology and link status now; `state_at_proposal()` returns a cached
snapshot keyed by `action_id`. Add `tests/test_mininet_adapter.py` with the
network stubbed — no real Mininet in tests.

Success: `demo/run_demo.py` runs against the real topology and blocks a reroute
onto a link dropped between proposal and execution.

### Phase 2 — NetArena integration (go/no-go)
`verifix/adapters/netarena.py`

**First, investigate and report before writing code.** Clone
`github.com/Froot-NetSys/NetArena`, read its agent/environment interface, and
tell me in plain terms: can a proposed action be intercepted between the agent
deciding and the environment executing? Where exactly is that seam? Do not
start implementing until you have answered this.

If the seam exists: map NetArena state into `NetworkState`, map its actions into
`ProposedAction`, and set `FeasibilityCheck.known_kinds` for its action
vocabulary. Expect some kinds to remain unknown — that is a result, not a bug.

If the seam does not exist: stop and tell me. Do not fork their codebase.

### Phase 3 — Trace replay
`verifix/adapters/replay.py`

Offline replay of NIKA's public traces (`github.com/sands-lab/nika`, 900+ agent
reasoning traces). No emulator, no agent. Read recorded actions and states from
disk, feed them through the gate, record verdicts. Many traces will lack a
state digest — those must produce `NOT_APPLICABLE` on state binding and show up
honestly in coverage.

### Phase 4 — Experiments
`experiments/run.py`, writing to `results/`

One runner, one flag for mode, one flag for adapter. Every run appends a row to
`results/runs.csv` with: run id, timestamp, adapter, mode, unknown_policy,
min_coverage, action id, verdict, per-check status, coverage evaluated/total,
latency_ms. Raw rows are never overwritten or edited in place.

`experiments/analyse.py` reads that CSV and emits exactly two tables:

**Table 1 — NetArena**

| Mode | Task success | Harmful actions executed | Coverage | Added latency (p50/p95) |
|---|---|---|---|---|
| off | | | | |
| check_once | | | | |
| revalidate | | | | |

**Table 2 — NIKA replay**

| Mode | Bad actions caught | False blocks | Coverage |
|---|---|---|---|

That is the entire experimental section. **Do not** build a factor matrix, a
sweep harness, or a hyperparameter search. If you think a third table is needed,
ask me first.

### Phase 5 — Paper artifacts
`paper/` — only on my explicit instruction, not automatically.

## File discipline

Create **only** these new paths:

```
verifix/adapters/mininet.py
verifix/adapters/netarena.py
verifix/adapters/replay.py
tests/test_mininet_adapter.py
tests/test_netarena_adapter.py
tests/test_replay_adapter.py
experiments/run.py
experiments/analyse.py
results/.gitkeep
```

Plus new check files under `verifix/checks/` if and only if a new check is
genuinely needed.

**Do not create**, unless I ask for it by name: `Dockerfile`,
`docker-compose.yml`, `Makefile`, `.github/`, `docs/`, `examples/`,
`notebooks/`, `scripts/`, `CONTRIBUTING.md`, `CHANGELOG.md`, `config/` trees,
`settings.py`, logging config files, a plugin registry, an abstract factory, a
CLI framework, or a web UI. This project is small on purpose. Every extra file
is something I have to defend in a code review with my mentor.

Edit existing files in place rather than creating parallel versions. No
`gate_v2.py`, no `gate_new.py`.

## Engineering standards

- Every file you write must run. No `TODO`, no `pass  # implement later`, no
  stub that returns a hardcoded value.
- Every new module gets tests in the same phase. Run `pytest` and show me real
  output — never claim tests pass without running them.
- Type hints on everything. `from __future__ import annotations` at the top.
- Comments explain *why*, not *what*. The existing files set the tone; match it.
- Keep functions short enough to read in one screen.
- If you need a dependency, add it to `pyproject.toml` and say why.

## Stop and ask me rather than guessing

- NetArena's interception seam does not exist or is unclear.
- NIKA's trace format does not carry what the gate needs.
- You believe an invariant above has to be broken.
- A phase is turning out much larger than described.
- You are about to create a file that is not on the allowed list.

Guessing on any of these costs me more time than asking does.

## Start here

Read the whole skeleton. Then tell me, before writing any code: your
understanding of the TOCTOU claim in your own words, anything in the existing
code you think is wrong or weak, and your plan for Phase 1. I will confirm
before you build.
