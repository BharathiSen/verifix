"""Controlled injection: link churn between proposal and execution (Phase 8).

    sudo ~/verifix-venv/bin/python -m experiments.inject                  # CSV to stdout
    sudo ~/verifix-venv/bin/python -m experiments.inject --repeats 3 \\
        --out results/injection-wsl.csv

Run from the repository root under WSL2, as root. Each trial starts from
Case 5's fault (s1-s2 down) and proposes the reroute onto 'backup' under
three binding scopes: whole-state, declared target only, declared
premise+target. Between proposal and execution every link of the triangle
toggles as an independent Poisson process at `rate` per second, for
`delay` seconds. Then the gate decides in all three modes.

Time is simulated; links are not. The schedule is drawn from a seeded RNG
and each event is applied to the real Mininet link, but nothing sleeps: the
provider's clock is advanced to each event and then to the end of the
delay, so freshness sees an age of exactly `delay`. What is measured is
what the gate makes of the state the churn leaves behind, not how events
race the gate's own read. A link toggled twice in the window ends where it
started; the events column keeps the flap, the changed-links column does not.

The rows log what happened (events, which links ended changed, whether the
premise, the target or something else changed) and never label an action
stale or sound. Which of those makes a block false is Phase 9's definition.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from experiments import run
from experiments.cases import (
    BACKUP_DEPS, MININET_PATHS, TRIANGLE_LINKS, Trial, build_triangle, decide,
    reroute,
)
from verifix.adapters.mininet import MininetStateProvider, link_id

#: Seconds from proposal to execution. 8 s is past the 5 s freshness budget
#: on purpose: it shows where freshness, not binding, decides.
DELAYS_S = (0.5, 1.0, 2.0, 4.0, 8.0)

#: Toggles per link per second. 0 is the control: any block there is not
#: caused by churn.
RATES = (0.0, 0.02, 0.05, 0.1, 0.2)

PREMISE = "s1-s2"
TARGET = tuple(MININET_PATHS["backup"])

SCOPES: tuple[tuple[str, list[str] | None], ...] = (
    ("whole-state", None),
    ("declared target only", BACKUP_DEPS),
    ("declared premise+target", [f"links:{PREMISE}", *BACKUP_DEPS]),
)

#: Links by canonical id, with the endpoint names configLinkStatus takes.
ENDPOINTS = {link_id(a, b): (a, b) for a, b in TRIANGLE_LINKS}


class SimClock:
    """The provider's clock, moved by the injector instead of by time."""

    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@dataclass(frozen=True)
class Event:
    at: float  # seconds after the proposal
    link: str
    status: str  # what the link was set to


def schedule(rng: random.Random, rate: float, delay: float,
             start: dict[str, bool]) -> list[Event]:
    """Each link toggles at Poisson times in [0, delay), in time order."""
    times: list[tuple[float, str]] = []
    if rate > 0:
        for link in sorted(start):  # sorted, so a seed fixes the schedule
            t = rng.expovariate(rate)
            while t < delay:
                times.append((t, link))
                t += rng.expovariate(rate)
    up = dict(start)
    events = []
    for t, link in sorted(times):
        up[link] = not up[link]
        events.append(Event(t, link, "up" if up[link] else "down"))
    return events


@dataclass(frozen=True)
class InjectionTrial:
    delay: float
    rate: float
    repeat: int
    events: list[Event]
    changed: list[str]  # links whose status differs from the proposal's
    trials: list[Trial]  # one per scope, all judged on the same live state


def _set(net: Any, link: str, status: str) -> None:
    net.configLinkStatus(*ENDPOINTS[link], status)


def one_trial(net: Any, provider: MininetStateProvider, clock: SimClock,
              seed: int, delay: float, rate: float, repeat: int,
              id_prefix: str) -> InjectionTrial:
    # Seeded per cell, not from one stream, so any single trial can be
    # reproduced from the row alone without replaying the run before it.
    rng = random.Random(f"{seed}/{delay}/{rate}/{repeat}")
    try:
        _set(net, PREMISE, "down")  # the fault the reroute remediates
        proposed_at = clock.now
        actions = []
        for i, (_, deps) in enumerate(SCOPES):
            action_id = f"{id_prefix}-{i}"
            actions.append(reroute(action_id, provider.snapshot(action_id), deps))
        seen = provider.state_at_proposal(actions[0]).links
        events = schedule(rng, rate, delay, seen)
        for event in events:
            clock.now = proposed_at + event.at
            _set(net, event.link, event.status)
        clock.now = proposed_at + delay
        trials = [decide(8, label, "mininet", provider, action)
                  for (label, _), action in zip(SCOPES, actions)]
        # Read back rather than inferred from the events: if a toggle did
        # not take, the log should say what the gate actually saw.
        live = provider.live_state().links
        changed = sorted(lk for lk in seen if live[lk] != seen[lk])
        return InjectionTrial(delay, rate, repeat, events, changed, trials)
    finally:
        # Every trial starts from the same healthy triangle.
        for link in ENDPOINTS:
            _set(net, link, "up")
        clock.now += 1.0  # keep proposals strictly ordered in time


def grid(net: Any, seed: int, repeats: int, run_id: str,
         delays: tuple[float, ...] = DELAYS_S,
         rates: tuple[float, ...] = RATES) -> Iterator[InjectionTrial]:
    clock = SimClock(time.time())
    provider = MininetStateProvider(net, MININET_PATHS, clock=clock)
    for repeat in range(repeats):
        for delay in delays:
            for rate in rates:
                yield one_trial(net, provider, clock, seed, delay, rate, repeat,
                                f"inj-{run_id}-{repeat}-{delay}-{rate}")


COLUMNS = [*run.COLUMNS, "delay_s", "churn_rate", "repeat", "seed", "events",
           "changed_links", "premise_changed", "target_changed",
           "other_changed"]


def rows(results: list[InjectionTrial], seed: int, run_id: str,
         timestamp: str) -> Iterator[dict[str, object]]:
    for r in results:
        extra = {
            "delay_s": r.delay, "churn_rate": r.rate, "repeat": r.repeat,
            "seed": seed,
            "events": json.dumps([[round(e.at, 4), e.link, e.status]
                                  for e in r.events]),
            "changed_links": json.dumps(r.changed),
            "premise_changed": PREMISE in r.changed,
            "target_changed": any(lk in r.changed for lk in TARGET),
            "other_changed": any(lk != PREMISE and lk not in TARGET
                                 for lk in r.changed),
        }
        for row in run.rows(r.trials, run_id, timestamp):
            yield {**row, **extra}


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout,
         net: Any = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeats", type=int, default=1,
                        help=f"repeats of the {len(DELAYS_S)} x {len(RATES)} grid")
    parser.add_argument("--seed", type=int,
                        help="base seed; omitted, one is drawn and recorded")
    parser.add_argument("--out", type=Path,
                        help="CSV to append to; omitted, rows go to stdout")
    args = parser.parse_args(argv)

    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**31)
    run_id = uuid.uuid4().hex[:12]
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # `net` is for tests: a stub stands in for Mininet, never in a real run.
    owned = net is None
    if owned:
        net = build_triangle()
        net.start()
    try:
        results = list(grid(net, seed, args.repeats, run_id))
    finally:
        if owned:
            net.stop()
    records = list(rows(results, seed, run_id, timestamp))

    if args.out is None:
        run.write(stdout, records, with_header=True, columns=COLUMNS)
    else:
        run.append(args.out, records, columns=COLUMNS)
        print(f"appended {len(records)} rows to {args.out} (run {run_id}, "
              f"seed {seed})", file=sys.stderr)


if __name__ == "__main__":
    main()
