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

`--evidence reads` (Phase 10) withholds the state dump. The model gets read
tools instead (`get_link`, `get_path`, plus id listings) served from the
proposal snapshot, and submits its action through a `submit` tool. The keys
it actually read are the *inferred* dependency set. The same action is then
judged three times, bound to what it declared, to what it read, and to the
whole state, so the comparison is paired on one proposal and one drift:

    sudo -E ~/verifix-venv/bin/python -m experiments.agent --evidence reads \
        --out results/agent-inferred-wsl.csv

Listing ids is not recorded as a read: it says which links exist, not their
status, and a key-wise digest cannot bind "these are all the links" anyway.
The tool loop's own time counts towards freshness, as any deliberation
would; `deliberation_s` records it, so a freshness block is attributable.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO, TypeVar

from experiments import run
from experiments.cases import (
    MININET_PATHS, TRIANGLE_LINKS, Trial, build_triangle, decide,
)
from verifix.adapters.mininet import MininetStateProvider
from verifix.models import NetworkState, ProposedAction

#: Production on Groq's models page, read 2026-11-19.
DEFAULT_MODEL = "openai/gpt-oss-120b"

#: Stands in for a digest over a declaration with a malformed key; see parse.
UNRESOLVABLE_DIGEST = "unresolvable"

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
    try:
        digest = seen.digest(deps)
    except ValueError:
        # A key outside "links:"/"paths:" is a well-formed reply with a bad
        # declaration, not a parse failure. It goes to the gate as declared,
        # where state binding errors on it and the decision is flagged. The
        # digest is never compared (binding raises first), so it only marks
        # that none could be computed.
        digest = UNRESOLVABLE_DIGEST
    return ProposedAction(
        action_id=action_id, kind=reply["kind"], params=params,
        proposed_at=seen.captured_at, state_digest=digest,
        depends_on=deps, rationale=reply.get("rationale"),
    )


_READS_FORMAT = """Read the state with the tools; nothing is shown up front. \
When you have decided, call `submit` exactly once. If no action is warranted, \
submit kind "none" with empty params."""


def prompt_reads(vocabulary: bool) -> tuple[str, str]:
    """The read-tool variant: the same task, with the state left to be read."""
    system = ("You operate a small network. You must propose exactly one "
              "remediation action, or none if nothing should change.\n\n")
    if vocabulary:
        system += _VOCABULARY + "\n\n"
    system += _READS_FORMAT
    user = ("Traffic between h1 and h2 normally uses path 'primary'. "
            "Read whatever state you need before deciding.")
    return system, user


def _tool(name: str, description: str,
          properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required}}}


_ID = {"id": {"type": "string"}}

#: OpenAI-style tool schemas, as Groq takes them.
TOOLS: list[dict[str, Any]] = [
    _tool("list_links", "Ids of all links (no status).", {}, []),
    _tool("list_paths", "Ids of all paths (no hops).", {}, []),
    _tool("get_link", "Whether one link is up.", _ID, ["id"]),
    _tool("get_path", "The ordered link ids of one path.", _ID, ["id"]),
    _tool("submit", "Submit the one action you propose.", {
        "kind": {"type": "string", "description": "the action type"},
        "params": {"type": "object", "description": "the action's arguments"},
        "depends_on": {"type": "array", "items": {"type": "string"},
                       "description": ('state keys your decision rests on, '
                                       'each "links:<id>" or "paths:<id>"')},
        "rationale": {"type": "string", "description": "one sentence"},
    }, ["kind", "params", "depends_on"]),
]

#: (tool name, arguments) -> the tool's result as text
Executor = Callable[[str, dict[str, Any]], str]

#: (system, user, executor) -> (model id, raw `submit` arguments)
ToolProposer = Callable[[str, str, Executor], tuple[str, str]]


class NoSubmit(Exception):
    """The model never called `submit` within the turn limit."""


class StateReader:
    """Serves the model's reads from the proposal snapshot, and logs them.

    The log is kept here, by the harness, not taken from the model's own
    account: inferred means what was read, not what was said to be read.
    Reads come from the snapshot so that what the model saw and what the
    action is bound to are the same state. A read of an id that does not
    exist is still a read: the absence was evidence, and the key hashes as
    absent, so binding catches it appearing.
    """

    def __init__(self, seen: NetworkState) -> None:
        self.seen = seen
        self.reads: list[str] = []

    def __call__(self, tool: str, args: dict[str, Any]) -> str:
        if tool == "list_links":
            return json.dumps(sorted(self.seen.links))
        if tool == "list_paths":
            return json.dumps(sorted(self.seen.paths))
        if tool in ("get_link", "get_path"):
            ident = args.get("id")
            if not isinstance(ident, str) or not ident:
                return json.dumps({"error": "'id' must be a non-empty string"})
            section = "links" if tool == "get_link" else "paths"
            key = f"{section}:{ident}"
            if key not in self.reads:
                self.reads.append(key)
            value = getattr(self.seen, section).get(ident)
            if value is None:
                return json.dumps({"id": ident, "exists": False})
            field = "up" if section == "links" else "links"
            return json.dumps({"id": ident, field: value})
        return json.dumps({"error": f"unknown tool {tool!r}"})


#: One chat completion: messages in, an OpenAI-shaped response out.
Create = Callable[[list[dict[str, Any]]], Any]

MAX_TURNS = 12


def tool_loop(create: Create, system: str, user: str, execute: Executor,
              max_turns: int = MAX_TURNS) -> tuple[str, str]:
    """Run tool calls until `submit`; return (model id, its raw arguments).

    Separate from the Groq client so the message plumbing is testable
    without a key. Calls after `submit` in the same turn are never run.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": system},
                                      {"role": "user", "content": user}]
    for _ in range(max_turns):
        response = create(messages)
        message = response.choices[0].message
        calls = message.tool_calls or []
        if not calls:
            # Tool choice is "required", so this should not happen; if it
            # does, the text goes to parse as the reply, which decides.
            return response.model, message.content or ""
        messages.append({"role": "assistant", "content": message.content or "",
                         "tool_calls": [
                             {"id": c.id, "type": "function",
                              "function": {"name": c.function.name,
                                           "arguments": c.function.arguments}}
                             for c in calls]})
        for call in calls:
            if call.function.name == "submit":
                return response.model, call.function.arguments or ""
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = None
            result = (execute(call.function.name, args)
                      if isinstance(args, dict)
                      else json.dumps({"error": "arguments are not a JSON object"}))
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": result})
    raise NoSubmit(f"no submit within {max_turns} turns")


def unresolved(keys: list[str] | None, seen: NetworkState) -> list[str]:
    """Declared keys naming nothing in the state the model was shown.

    A well-formed key naming no link or path hashes as absent and stays
    absent, so the binding silently covers nothing there. A malformed key
    (neither "links:" nor "paths:") makes state binding error in the gate.
    Both are listed here; neither is corrected.
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
    # Read-tool mode only. `trial` is then the action bound to what it
    # declared; these are the same action bound to what it read and to the
    # whole state.
    reads: tuple[str, ...] | None = None
    inferred: Trial | None = None
    whole: Trial | None = None
    deliberation_s: float | None = None


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


def one_trial_reads(net: Any, provider: MininetStateProvider,
                    propose: ToolProposer, action_id: str, vocabulary: bool,
                    drift: str) -> AgentTrial:
    """One proposal by reading, judged under three bindings of one action."""
    try:
        net.configLinkStatus("s1", "s2", "down")
        seen = provider.snapshot(action_id)
        reader = StateReader(seen)
        started = time.monotonic()
        try:
            model, raw = propose(*prompt_reads(vocabulary), reader)
        except NoSubmit as exc:
            return AgentTrial("", vocabulary, drift, "", seen, None, str(exc),
                              reads=tuple(reader.reads))
        elapsed = time.monotonic() - started
        reads = list(reader.reads)
        try:
            declared = parse(raw, action_id, seen)
        except ValueError as exc:
            return AgentTrial(model, vocabulary, drift, raw, seen, None,
                              str(exc), reads=tuple(reads),
                              deliberation_s=elapsed)
        # An empty read set stays empty, not widened: binding then abstains
        # and the decision is flagged, as for an empty declaration.
        inferred = declared.model_copy(update={
            "depends_on": reads, "state_digest": seen.digest(reads)})
        whole = declared.model_copy(update={
            "depends_on": None, "state_digest": seen.digest()})
        _set_links(net, DRIFTS[drift])
        variant = f"{'vocab' if vocabulary else 'free'}/{drift}"
        # All three judged on the same live state, after the one drift.
        return AgentTrial(
            model, vocabulary, drift, raw, seen,
            decide(10, f"{variant}/declared", "mininet", provider, declared),
            reads=tuple(reads),
            inferred=decide(10, f"{variant}/inferred", "mininet", provider,
                            inferred),
            whole=decide(10, f"{variant}/whole", "mininet", provider, whole),
            deliberation_s=elapsed)
    finally:
        _set_links(net, [(a, b, "up") for a, b in TRIANGLE_LINKS])


EVIDENCE = ("dump", "reads")


def trials(net: Any, propose: Proposer | ToolProposer, repeats: int,
           run_id: str, evidence: str = "dump", pause: float = 0.0,
           sleep: Callable[[float], None] = time.sleep) -> Iterator[AgentTrial]:
    if evidence not in EVIDENCE:
        raise ValueError(f"evidence must be one of {EVIDENCE}")
    provider = MininetStateProvider(net, MININET_PATHS)
    prefix = "ag" if evidence == "dump" else "ir"
    first = True
    for i in range(repeats):
        for vocabulary in (False, True):
            for drift in DRIFTS:
                # The pause sits between trials, before the snapshot, so a
                # rate limit's wait lands here and not inside a trial,
                # where it would count towards freshness.
                if not first and pause > 0:
                    sleep(pause)
                first = False
                action_id = (f"{prefix}-{run_id}-{i}-"
                             f"{'vocab' if vocabulary else 'free'}-{drift}")
                if evidence == "dump":
                    yield one_trial(net, provider, propose,  # type: ignore[arg-type]
                                    action_id, vocabulary, drift)
                else:
                    yield one_trial_reads(net, provider, propose,  # type: ignore[arg-type]
                                          action_id, vocabulary, drift)


COLUMNS = [*run.COLUMNS, "model", "vocabulary", "drift", "kind", "params",
           "depends_on", "unresolved_keys", "declares_premise",
           "declares_target", "raw", "parse_error"]


def _covers(keys: list[str]) -> tuple[bool, bool]:
    """(premise, target) named among the keys.

    The target is the backup path's links; naming the path alone binds its
    link list, not whether those links are up.
    """
    return ("links:s1-s2" in keys,
            all(f"links:{lk}" in keys for lk in MININET_PATHS["backup"]))


def _agent_fields(r: AgentTrial) -> dict[str, object]:
    extra: dict[str, object] = {
        "model": r.model, "vocabulary": r.vocabulary, "drift": r.drift,
        "raw": r.raw, "parse_error": r.error,
    }
    if r.trial is not None:
        a = r.trial.action  # the declared binding in either evidence mode
        premise, target = _covers(a.depends_on or [])
        extra.update({
            "kind": a.kind, "params": json.dumps(a.params, sort_keys=True),
            "depends_on": json.dumps(a.depends_on),
            "unresolved_keys": json.dumps(unresolved(a.depends_on, r.seen)),
            "declares_premise": premise, "declares_target": target,
        })
    return extra


def rows(results: list[AgentTrial], run_id: str,
         timestamp: str) -> Iterator[dict[str, object]]:
    for r in results:
        extra = _agent_fields(r)
        if r.trial is None:
            # One row, not three: no mode decided anything.
            yield {**dict.fromkeys(COLUMNS, ""), "run_id": run_id,
                   "timestamp": timestamp, "adapter": "mininet", "case": 7,
                   **extra}
            continue
        for row in run.rows([r.trial], run_id, timestamp):
            yield {**row, **extra}


#: Read-tool mode. `depends_on` stays what the model declared;
#: `binding_source` says which key list this row's gate bound to.
READ_COLUMNS = [*COLUMNS, "binding_source", "reads", "deliberation_s",
                "reads_premise", "reads_target"]


def read_rows(results: list[AgentTrial], run_id: str,
              timestamp: str) -> Iterator[dict[str, object]]:
    """Nine rows per parsed trial (3 bindings x 3 modes); one per failure."""
    for r in results:
        reads = list(r.reads or [])
        reads_premise, reads_target = _covers(reads)
        extra = {
            **_agent_fields(r), "reads": json.dumps(reads),
            "deliberation_s": ("" if r.deliberation_s is None
                               else round(r.deliberation_s, 3)),
            "reads_premise": reads_premise, "reads_target": reads_target,
        }
        if r.trial is None:
            yield {**dict.fromkeys(READ_COLUMNS, ""), "run_id": run_id,
                   "timestamp": timestamp, "adapter": "mininet", "case": 10,
                   **extra}
            continue
        judged = {"declared": r.trial, "inferred": r.inferred, "whole": r.whole}
        for source, trial in judged.items():
            assert trial is not None  # set together with r.trial
            for row in run.rows([trial], run_id, timestamp):
                yield {**row, **extra, "binding_source": source}


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


#: Seconds to wait after each successive rate-limit refusal. Groq's free
#: tier is per minute, so the waits reach a full window before giving up.
RATE_WAITS = (5.0, 10.0, 20.0, 30.0, 60.0, 60.0)

T = TypeVar("T")


def retrying(call: Callable[[], T], retryable: type[BaseException],
             waits: tuple[float, ...] = RATE_WAITS,
             sleep: Callable[[float], None] = time.sleep,
             log: TextIO = sys.stderr) -> T:
    """`call()`, retried after each wait while it raises `retryable`.

    A wait inside a trial counts towards freshness like any deliberation;
    it shows in `deliberation_s`, so a freshness block it causes is
    attributable. `--pause` exists to keep such waits rare.
    """
    for wait in waits:
        try:
            return call()
        except retryable:
            print(f"rate limited; waiting {wait:.0f} s", file=log)
            sleep(wait)
    return call()  # the last refusal propagates


def groq_tool_proposer(model: str) -> ToolProposer:
    from groq import Groq, RateLimitError

    # Retries are ours, with waits sized to a per-minute limit; the SDK's
    # own are sub-second and spent by the time the window clears.
    client = Groq(max_retries=0)

    def create(messages: list[dict[str, Any]]) -> Any:
        # "required": every turn is a tool call, so the action can only
        # arrive through `submit`, never as free text to be fished out.
        return retrying(lambda: client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS,
            tool_choice="required"), RateLimitError)

    def propose(system: str, user: str, execute: Executor) -> tuple[str, str]:
        return tool_loop(create, system, user, execute)

    return propose


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--trials", type=int, default=1,
                        help="repeats of the 2 x 3 grid (6 model calls each)")
    parser.add_argument("--out", type=Path,
                        help="CSV to append to; omitted, rows go to stdout")
    parser.add_argument("--evidence", choices=EVIDENCE, default="dump",
                        help="dump: state in the prompt (Phase 7); reads: "
                             "read tools, judged declared/inferred/whole")
    parser.add_argument("--pause", type=float, default=None,
                        help="seconds between trials (default: 60 with "
                             "--evidence reads, to clear a per-minute token "
                             "limit; 0 otherwise)")
    args = parser.parse_args(argv)
    if args.pause is None:
        args.pause = 60.0 if args.evidence == "reads" else 0.0

    reads = args.evidence == "reads"
    propose: Proposer | ToolProposer = (
        groq_tool_proposer(args.model) if reads else groq_proposer(args.model))
    columns = READ_COLUMNS if reads else COLUMNS
    run_id = uuid.uuid4().hex[:12]
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    net = build_triangle()
    net.start()
    try:
        results = list(trials(net, propose, args.trials, run_id,
                              args.evidence, args.pause))
    finally:
        net.stop()
    records = list((read_rows if reads else rows)(results, run_id, timestamp))

    if args.out is None:
        run.write(stdout, records, with_header=True, columns=columns)
    else:
        run.append(args.out, records, columns=columns)
        print(f"appended {len(records)} rows to {args.out} (run {run_id})",
              file=sys.stderr)


if __name__ == "__main__":
    main()
