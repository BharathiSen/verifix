"""An LLM proposes remediations on the Mininet triangle; the gate judges them.

    sudo -E ~/verifix-venv/bin/python -m experiments.agent              # CSV to stdout
    sudo -E ~/verifix-venv/bin/python -m experiments.agent --trials 3 --out <csv>

Run from the repository root under WSL2, as root, with GROQ_API_KEY set
(`-E` keeps it through sudo). Each trial takes s1-s2 down, snapshots the
state, and asks the model for one action. It then applies one drift and has
the gate decide in all three modes:

    none              nothing changes; the action is still sound
    target_drops      s1-s3 goes down (Case 4's change)
    premise_recovers  s1-s2 comes back (Case 5's change)

`vocabulary` is one flag, run both ways: whether the prompt names the
action kinds the gate's feasibility check understands. Unprompted, the model
may name its own kind, and feasibility then abstains; that coverage loss is
one of the things measured here, not a bug to prompt away.

What the model declares in `depends_on` is kept as it said it, never fixed
up: whether it declares the premise, the target or both is the result.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from experiments import run
from experiments.cases import (
    MININET_PATHS, TRIANGLE_LINKS, Trial, build_triangle, decide,
)
from verifix.adapters.mininet import MininetStateProvider
from verifix.models import NetworkState, ProposedAction

#: Production on Groq's models page, read 2026-11-19.
DEFAULT_MODEL = "openai/gpt-oss-120b"

#: (system, user) -> (model id the provider reports, raw reply text)
Proposer = Callable[[str, str], tuple[str, str]]

DRIFTS: dict[str, list[tuple[str, str, str]]] = {
    "none": [],
    "target_drops": [("s1", "s3", "down")],
    "premise_recovers": [("s1", "s2", "up")],
}

_REPLY_FORMAT = """Reply with a single JSON object and nothing else:
{"kind": <string, the action type>,
 "params": <object, the action's arguments>,
 "depends_on": <list of state keys your decision rests on, each "links:<id>" or "paths:<id>">,
 "rationale": <one sentence>}
If no action is warranted, reply {"kind": "none", "params": {}, "depends_on": [], "rationale": ...}."""

_VOCABULARY = """The executor understands one action kind: "reroute", with params \
{"target_path": <a path id from the state>}. Use it, or "none"."""


def prompt(state: NetworkState, vocabulary: bool) -> tuple[str, str]:
    system = ("You operate a small network. You are shown its current state and "
              "must propose exactly one remediation action, or none if nothing "
              "should change.\n\n")
    if vocabulary:
        system += _VOCABULARY + "\n\n"
    system += _REPLY_FORMAT
    user = ("Traffic between h1 and h2 normally uses path 'primary'. "
            "Current state:\n" + json.dumps(
                {"links": state.links, "paths": state.paths}, indent=2))
    return system, user


def parse(raw: str, action_id: str, seen: NetworkState) -> ProposedAction:
    """The reply as an action bound to what the model saw. Raises on bad replies."""
    reply = json.loads(raw)
    if not isinstance(reply, dict) or not isinstance(reply.get("kind"), str):
        raise ValueError("reply has no string 'kind'")
    params = reply.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("'params' is not an object")
    deps = reply.get("depends_on")
    if deps is not None and not (isinstance(deps, list)
                                 and all(isinstance(k, str) for k in deps)):
        raise ValueError("'depends_on' is not a list of strings")
    # An empty list is kept as declared-nothing, not widened to whole state:
    # binding then abstains and the gate flags it, which is the honest result.
    return ProposedAction(
        action_id=action_id, kind=reply["kind"], params=params,
        proposed_at=seen.captured_at, state_digest=seen.digest(deps),
        depends_on=deps, rationale=reply.get("rationale"),
    )


def unresolved(keys: list[str] | None, seen: NetworkState) -> list[str]:
    """Declared keys naming nothing in the state the model was shown.

    Such a key hashes as absent and stays absent, so the binding silently
    covers nothing there. Reported, not corrected.
    """
    out = []
    for key in keys or []:
        section, _, name = key.partition(":")
        if section not in ("links", "paths") or name not in getattr(seen, section):
            out.append(key)
    return out


@dataclass(frozen=True)
class AgentTrial:
    model: str
    vocabulary: bool
    drift: str
    raw: str
    seen: NetworkState
    trial: Trial | None  # None when the reply could not be parsed
    error: str = ""


def _set_links(net: Any, changes: list[tuple[str, str, str]]) -> None:
    for a, b, status in changes:
        net.configLinkStatus(a, b, status)


def one_trial(net: Any, provider: MininetStateProvider, propose: Proposer,
              action_id: str, vocabulary: bool, drift: str) -> AgentTrial:
    try:
        net.configLinkStatus("s1", "s2", "down")  # the fault to remediate
        seen = provider.snapshot(action_id)
        model, raw = propose(*prompt(seen, vocabulary))
        try:
            action = parse(raw, action_id, seen)
        except ValueError as exc:  # json.JSONDecodeError included
            return AgentTrial(model, vocabulary, drift, raw, seen, None, str(exc))
        _set_links(net, DRIFTS[drift])
        trial = decide(7, f"{'vocab' if vocabulary else 'free'}/{drift}",
                       "mininet", provider, action)
        return AgentTrial(model, vocabulary, drift, raw, seen, trial)
    finally:
        # Every trial starts from the same healthy triangle.
        _set_links(net, [(a, b, "up") for a, b in TRIANGLE_LINKS])


def trials(net: Any, propose: Proposer, repeats: int,
           run_id: str) -> Iterator[AgentTrial]:
    provider = MininetStateProvider(net, MININET_PATHS)
    for i in range(repeats):
        for vocabulary in (False, True):
            for drift in DRIFTS:
                action_id = (f"ag-{run_id}-{i}-"
                             f"{'vocab' if vocabulary else 'free'}-{drift}")
                yield one_trial(net, provider, propose, action_id,
                                vocabulary, drift)


COLUMNS = [*run.COLUMNS, "model", "vocabulary", "drift", "kind", "params",
           "depends_on", "unresolved_keys", "declares_premise",
           "declares_target", "raw", "parse_error"]


def rows(results: list[AgentTrial], run_id: str,
         timestamp: str) -> Iterator[dict[str, object]]:
    for r in results:
        extra: dict[str, object] = {
            "model": r.model, "vocabulary": r.vocabulary, "drift": r.drift,
            "raw": r.raw, "parse_error": r.error,
        }
        if r.trial is None:
            # One row, not three: no mode decided anything.
            yield {**dict.fromkeys(COLUMNS, ""), "run_id": run_id,
                   "timestamp": timestamp, "adapter": "mininet", "case": 7,
                   **extra}
            continue
        a = r.trial.action
        deps = a.depends_on or []
        extra.update({
            "kind": a.kind, "params": json.dumps(a.params, sort_keys=True),
            "depends_on": json.dumps(a.depends_on),
            "unresolved_keys": json.dumps(unresolved(a.depends_on, r.seen)),
            "declares_premise": "links:s1-s2" in deps,
            # The target is the backup path's links; naming the path alone
            # binds its link list, not whether those links are up.
            "declares_target": all(f"links:{lk}" in deps
                                   for lk in MININET_PATHS["backup"]),
        })
        for row in run.rows([r.trial], run_id, timestamp):
            yield {**row, **extra}


def groq_proposer(model: str) -> Proposer:
    # Imported here so the module and its tests load without the SDK or a key.
    from groq import Groq

    client = Groq()  # reads GROQ_API_KEY

    def propose(system: str, user: str) -> tuple[str, str]:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
        )
        return response.model, response.choices[0].message.content or ""

    return propose


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--trials", type=int, default=1,
                        help="repeats of the 2 x 3 grid (6 model calls each)")
    parser.add_argument("--out", type=Path,
                        help="CSV to append to; omitted, rows go to stdout")
    args = parser.parse_args(argv)

    propose = groq_proposer(args.model)
    run_id = uuid.uuid4().hex[:12]
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    net = build_triangle()
    net.start()
    try:
        results = list(trials(net, propose, args.trials, run_id))
    finally:
        net.stop()
    records = list(rows(results, run_id, timestamp))

    if args.out is None:
        run.write(stdout, records, with_header=True, columns=COLUMNS)
    else:
        run.append(args.out, records, columns=COLUMNS)
        print(f"appended {len(records)} rows to {args.out} (run {run_id})",
              file=sys.stderr)


if __name__ == "__main__":
    main()
