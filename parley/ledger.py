"""The Ledger: explainable knowledge-contribution scoring (SPEC §9).

**What this measures, honestly.** The Ledger measures *recorded contribution* — what an
agent put into the log and what of its work is still standing in the workspace. It does
**not** measure quality, correctness, insight or worth. A wrong decision scores the same
as a right one; a hundred lines of generated boilerplate outscore one surgical fix. Treat
the number as "who put what on the record", never as a performance review, and say so
wherever it is displayed.

**Why it is built this way.** SPEC R6 requires every number the Deck shows to be traceable
back to events in the log. So there are no learned weights and no model here: six additive
components, fixed published weights, and an ``evidence`` list that names the exact ``seq``
of every event that produced a point. ``parley ledger --why`` and the Deck's hover panel
render that list directly — neither has to re-derive anything.

That requirement binds hardest on the **one negative term**. ``service`` subtracts
``abandoned_request_penalty`` for every request an agent accepted and then never answered
(SPEC §15). A penalty a user cannot trace would violate R6 outright, so it is not a silent
adjustment to a total: it is an ordinary ``evidence`` entry, with the ``seq`` of the Hub's
``request.expired`` event and a label that says in words what happened and who was left
waiting. Anything that takes points away has to be at least as explainable as anything that
gives them.

**Gaming resistance.** Any scoreboard an autonomous agent can see is a scoreboard it will
try to climb. The deliberate defences are:

* *Self-citation earns nothing.* Citing your own event — the first thing a clever agent
  tries — awards zero. Only a citation from a different agent counts.
* *Presence is hard-capped.* Chat is worth 0.05 a message up to a ceiling of 10 points, so
  no amount of chatter beats substance.
* *Superseded contributions do not double-count.* A ``supersedes`` chain collapses to its
  tip, and — importantly — ``supersedes`` is only honoured when the superseding event has
  the **same actor**, so one agent cannot zero out a rival's contributions by claiming to
  have replaced them.
* *Lines are capped per file.* A single generated 50 000-line file is worth at most
  ``surviving_lines_cap_per_file`` lines.
* *Only surviving lines count.* Code that was replaced by someone else stops paying.
* *Delivery requires a claim.* ``task.done`` scores only for the agent that holds the task.
* *Conflict sidecars are excluded*, so a divergence cannot be farmed for a second copy of
  the same content.
* *Service is capped per requester-pair.* Two agents cannot take turns asking each other
  for trivial work: past ``service_cap_per_requester`` points from one caller, further
  results from that caller are worth nothing. Serving yourself is worth nothing at all.

``compute()`` is **pure**: no filesystem, no network, no clock, no randomness. Everything it
needs arrives as an argument, including the reference time used for decay. That makes it
trivially testable and lets the Hub recompute it incrementally on every append.

This module deliberately has **no intra-package imports**. It is the one piece of arithmetic
the Deck, the CLI and the Hub all depend on; keeping it self-contained means it can be
reviewed, vendored or re-implemented in another language without dragging the rest of the
package along. The ten-line RFC 3339 reader below is the whole cost of that choice.
"""

from __future__ import annotations

import copy
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

LOG = logging.getLogger("parley.ledger")

__all__ = [
    "DEFAULT_WEIGHTS",
    "COMPONENTS",
    "LedgerLine",
    "LedgerResult",
    "compute",
    "load_weights",
    "merge_weights",
    "count_lines",
    "is_binary",
]

#: SPEC §9. Overridable per workspace in ``.parley/ledger.json`` (see :func:`load_weights`).
DEFAULT_WEIGHTS: Dict[str, Any] = {
    "contribution_weights": {
        "decision": 8,
        "design": 6,
        "finding": 5,
        "fix": 3,
        "review": 3,
        "doc": 2,
        "code": 2,
        "answer": 1,
    },
    "surviving_line_points": 0.02,
    "surviving_lines_cap_per_file": 400,
    "task_done_points": 2.0,
    "citation_received_points": 0.5,
    "chat_message_points": 0.05,
    "chat_points_cap": 10.0,
    "service_points": 3.0,
    "service_priority_bonus": 0.5,
    "service_cap_per_requester": 20.0,
    "abandoned_request_penalty": -5.0,
    "decay_half_life_days": 0,
}

#: The six components, in display order. Every ``LedgerLine`` carries all six, always.
COMPONENTS: Tuple[str, ...] = (
    "contributions",
    "authored",
    "delivery",
    "influence",
    "service",
    "presence",
)

#: Hub-authored events have this actor. The Hub is not a participant and never scores.
HUB_ACTOR = "hub"

#: SPEC §7.6 sidecar infix. Displaced copies are not fresh authorship.
CONFLICT_MARKER = ".parley-conflict-"

#: Points are stored rounded to this many decimals so JSON round-trips are stable.
_PRECISION = 6

_SECONDS_PER_DAY = 86400.0


# --------------------------------------------------------------------------- helpers


def is_binary(data: bytes) -> bool:
    """A NUL byte in the first 8 KiB is the same heuristic git uses, and it is enough.

    Being wrong here is cheap in one direction only: a file wrongly called binary scores
    nothing, which is far better than a file wrongly called text scoring an agent for the
    "lines" of a PNG.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("is_binary() wants bytes")
    return b"\x00" in bytes(data[:8192])


def count_lines(data: bytes) -> Optional[int]:
    """Lines in ``data``, or ``None`` when it is binary.

    Exported because the Hub's file index is what feeds :func:`compute` — the indexer calls
    this once when it stores a blob and records the result as ``lines``/``binary`` on the
    file record. ``compute`` itself may not read a blob; it is pure.

    A trailing newline does not create an extra line, and an empty file has zero lines.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("count_lines() wants bytes")
    raw = bytes(data)
    if is_binary(raw):
        return None
    if not raw:
        return 0
    n = raw.count(b"\n")
    if not raw.endswith(b"\n"):
        n += 1
    return n


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, bool):
            return default
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def _round(value: float) -> float:
    return round(float(value) + 0.0, _PRECISION)


def _to_unix(ts: Any) -> Optional[float]:
    """Parse the SPEC §1.2 wire timestamp to unix seconds, tolerantly.

    Deliberately local rather than imported (see the module docstring). Accepts the exact
    ``2026-10-08T12:34:56.789Z`` form plus the obvious near-misses (no milliseconds, a
    ``+00:00`` offset) because a timestamp is author-supplied and therefore untrusted.
    Returns ``None`` rather than raising: a malformed ``ts`` must cost that one event its
    decay adjustment, not blow up the whole scoreboard.
    """
    if not isinstance(ts, str) or len(ts) < 19:
        return None
    text = ts.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1]
    elif len(text) >= 6 and text[-6] in "+-" and text[-3] == ":":
        # An explicit offset. Fold it into the value and keep working in UTC.
        sign = 1 if text[-6] == "+" else -1
        try:
            off = sign * (int(text[-5:-3]) * 3600 + int(text[-2:]) * 60)
        except ValueError:
            return None
        base = _to_unix(text[:-6] + "Z")
        return None if base is None else base - off
    date_part, sep, time_part = text.partition("T")
    if not sep:
        date_part, sep, time_part = text.partition(" ")
        if not sep:
            return None
    try:
        year, month, day = (int(x) for x in date_part.split("-"))
        hh, mm, rest = time_part.split(":")
        seconds = float(rest)
        hour, minute = int(hh), int(mm)
    except (ValueError, TypeError):
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    # Days from the civil epoch (Howard Hinnant's algorithm): no datetime, no tz database,
    # no surprises on a platform with a narrow time_t.
    y = year - (1 if month <= 2 else 0)
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    days = era * 146097 + doe - 719468
    return days * _SECONDS_PER_DAY + hour * 3600 + minute * 60 + seconds


def merge_weights(weights: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Overlay ``weights`` on :data:`DEFAULT_WEIGHTS` without mutating either.

    ``contribution_weights`` merges key-by-key so a workspace can re-price a single kind
    without having to restate the whole table.
    """
    merged = copy.deepcopy(DEFAULT_WEIGHTS)
    if not weights:
        return merged
    for key, value in weights.items():
        if key == "contribution_weights":
            if isinstance(value, Mapping):
                for kind, points in value.items():
                    merged["contribution_weights"][str(kind)] = _as_float(points, 0.0)
            continue
        merged[key] = value
    merged["surviving_line_points"] = _as_float(
        merged.get("surviving_line_points"), DEFAULT_WEIGHTS["surviving_line_points"]
    )
    merged["surviving_lines_cap_per_file"] = _as_int(
        merged.get("surviving_lines_cap_per_file"),
        DEFAULT_WEIGHTS["surviving_lines_cap_per_file"],
    )
    merged["task_done_points"] = _as_float(
        merged.get("task_done_points"), DEFAULT_WEIGHTS["task_done_points"]
    )
    merged["citation_received_points"] = _as_float(
        merged.get("citation_received_points"), DEFAULT_WEIGHTS["citation_received_points"]
    )
    merged["chat_message_points"] = _as_float(
        merged.get("chat_message_points"), DEFAULT_WEIGHTS["chat_message_points"]
    )
    merged["chat_points_cap"] = _as_float(
        merged.get("chat_points_cap"), DEFAULT_WEIGHTS["chat_points_cap"]
    )
    merged["service_points"] = _as_float(
        merged.get("service_points"), DEFAULT_WEIGHTS["service_points"]
    )
    merged["service_priority_bonus"] = _as_float(
        merged.get("service_priority_bonus"), DEFAULT_WEIGHTS["service_priority_bonus"]
    )
    merged["service_cap_per_requester"] = _as_float(
        merged.get("service_cap_per_requester"), DEFAULT_WEIGHTS["service_cap_per_requester"]
    )
    # The published default is negative and SPEC §9 describes the term as "minus the
    # penalty", so both spellings are in the wild. Normalising to a negative number
    # means a workspace that writes `5` gets a penalty of five points rather than a
    # five-point reward for abandoning a request, which would be a very funny bug.
    merged["abandoned_request_penalty"] = -abs(
        _as_float(merged.get("abandoned_request_penalty"),
                  DEFAULT_WEIGHTS["abandoned_request_penalty"])
    )
    merged["decay_half_life_days"] = _as_float(
        merged.get("decay_half_life_days"), DEFAULT_WEIGHTS["decay_half_life_days"]
    )
    return merged


def load_weights(workspace: Path) -> Dict[str, Any]:
    """``<workspace>/.parley/ledger.json`` overlaid on the defaults.

    The one impure function here. A missing file is normal; a corrupt one is logged and
    ignored, because a typo in a tuning file must not take the Deck down.
    """
    path = Path(workspace) / ".parley" / "ledger.json"
    try:
        raw = path.read_bytes()
    except (OSError, ValueError):
        return merge_weights(None)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        LOG.warning("ignoring unreadable ledger weights at %s: %s", path, exc)
        return merge_weights(None)
    if not isinstance(parsed, dict):
        LOG.warning("ignoring ledger weights at %s: expected a JSON object", path)
        return merge_weights(None)
    return merge_weights(parsed)


# --------------------------------------------------------------------------- results


@dataclass
class LedgerLine:
    """One agent's row. ``evidence`` always carries all five component keys."""

    agent_id: str
    name: str
    total: float
    share: float
    components: Dict[str, float] = field(default_factory=dict)
    evidence: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "total": _round(self.total),
            "share": self.share,
            "components": {k: _round(self.components.get(k, 0.0)) for k in COMPONENTS},
            "evidence": {k: list(self.evidence.get(k, [])) for k in COMPONENTS},
        }


@dataclass
class LedgerResult:
    lines: List[LedgerLine] = field(default_factory=list)
    weights: Dict[str, Any] = field(default_factory=dict)
    computed_at: str = ""
    event_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lines": [line.to_dict() for line in self.lines],
            "weights": copy.deepcopy(self.weights),
            "computed_at": self.computed_at,
            "event_count": self.event_count,
            "total_points": _round(sum(line.total for line in self.lines)),
            "components": list(COMPONENTS),
            "caveat": (
                "Measures recorded contribution, not quality or worth."
            ),
        }

    def line(self, agent_id: str) -> Optional[LedgerLine]:
        for line in self.lines:
            if line.agent_id == agent_id:
                return line
        return None

    def why(self, agent_id: str) -> str:
        """The terminal rendering behind ``parley ledger --why`` (SPEC §9, R6)."""
        line = self.line(agent_id)
        if line is None:
            return "No ledger entry for {0}.".format(agent_id)
        out = [
            "{0}  ({1})".format(line.name, line.agent_id),
            "  total {0:.2f} points   share {1:.2f}%".format(line.total, line.share),
            "",
        ]
        for component in COMPONENTS:
            points = line.components.get(component, 0.0)
            entries = line.evidence.get(component, [])
            out.append("  {0:<14} {1:>8.2f}".format(component, points))
            for entry in entries:
                out.append(
                    "      seq {0:<6} {1:+.2f}  {2}".format(
                        entry.get("seq", 0),
                        _as_float(entry.get("points")),
                        entry.get("label", ""),
                    )
                )
            if not entries:
                out.append("      (nothing recorded)")
        out.append("")
        out.append("  The Ledger measures recorded contribution, not quality or worth.")
        return "\n".join(out)


# --------------------------------------------------------------------------- compute


def _sort_key(indexed: Tuple[int, Mapping[str, Any]]) -> Tuple[int, int]:
    index, event = indexed
    return (_as_int(event.get("seq"), 0), index)


def _refs_of(body: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    refs = body.get("refs")
    if not isinstance(refs, (list, tuple)):
        return []
    return [r for r in refs if isinstance(r, Mapping)]


def _apportion(values: Sequence[float], precision: int = 2) -> List[float]:
    """Largest-remainder apportionment, so the printed shares sum to exactly 100.00.

    Naive ``v / total * 100`` rounded for display routinely sums to 99.99 or 100.01, which
    looks like a bug in a stacked bar. When every total is zero the share is split evenly:
    "nobody has done anything, everyone is equal" is the sane reading, and it keeps the
    sums-to-100 invariant unconditional so the Deck never has a special case.
    """
    n = len(values)
    if n == 0:
        return []
    scale = 10 ** precision
    target = 100 * scale
    total = sum(v for v in values if v > 0.0)
    if total <= 0.0:
        base = [target // n] * n
        for i in range(target - sum(base)):
            base[i] += 1
        return [b / float(scale) for b in base]
    raw = [(v / total) * target if v > 0.0 else 0.0 for v in values]
    floors = [int(math.floor(x)) for x in raw]
    remainder = target - sum(floors)
    order = sorted(range(n), key=lambda i: (-(raw[i] - floors[i]), i))
    for i in order[: max(0, remainder)]:
        floors[i] += 1
    return [f / float(scale) for f in floors]


class _Scorer:
    """Mutable accumulator for one agent. Internal; the public shape is LedgerLine."""

    __slots__ = ("points", "evidence")

    def __init__(self) -> None:
        self.points = {component: 0.0 for component in COMPONENTS}
        self.evidence = {component: [] for component in COMPONENTS}  # type: Dict[str, List[Dict[str, Any]]]

    def add(
        self,
        component: str,
        seq: int,
        label: str,
        points: float,
        *,
        event_id: str = "",
        decay: float = 1.0,
        **extra: Any
    ) -> None:
        awarded = _round(points)
        self.points[component] += awarded
        entry = {"seq": int(seq), "label": label, "points": awarded, "id": event_id}
        if decay != 1.0:
            entry["decay"] = _round(decay)
            entry["base_points"] = _round(points / decay) if decay else 0.0
        entry.update(extra)
        self.evidence[component].append(entry)


def compute(
    events: Iterable[Mapping[str, Any]],
    files: Optional[Mapping[str, Mapping[str, Any]]] = None,
    weights: Optional[Mapping[str, Any]] = None,
    *,
    computed_at: Optional[str] = None,
    now: Optional[float] = None,
    max_evidence: int = 0
) -> LedgerResult:
    """Score every agent that appears in ``events`` or authored a file in ``files``.

    Pure. No I/O, no clock, no randomness — call it as often as you like.

    :param events: the log, in any order; it is sorted by ``seq`` internally so the result
        does not depend on how the caller happened to iterate.
    :param files: the Hub's current file index, ``path -> {hash,size,author,seq,ts,lines?,
        binary?}``. ``lines`` and ``binary`` come from :func:`count_lines` at index time;
        a record without either scores no authored substance, because ``compute`` may not
        open a blob to find out.
    :param weights: overlaid on :data:`DEFAULT_WEIGHTS` via :func:`merge_weights`.
    :param computed_at: stamped onto the result. Defaults to the ``ts`` of the highest-seq
        event, which keeps the function pure and makes the result reproducible from the log
        alone.
    :param now: unix seconds, the reference point for ``decay_half_life_days``. Defaults to
        ``computed_at``. Injected rather than read from the clock so decay is testable.
    :param max_evidence: truncate each component's evidence list to this many entries,
        replacing the tail with one summary line. ``0`` (the default) keeps everything,
        because SPEC R6 wants every point traceable; the Hub may lower it for very long
        sessions to keep ``/v1/state`` small.
    """
    w = merge_weights(weights)
    contribution_weights = w["contribution_weights"]
    line_points = w["surviving_line_points"]
    line_cap = w["surviving_lines_cap_per_file"]
    done_points = w["task_done_points"]
    citation_points = w["citation_received_points"]
    chat_points = w["chat_message_points"]
    chat_cap = w["chat_points_cap"]
    service_points = w["service_points"]
    service_bonus = w["service_priority_bonus"]
    service_cap = w["service_cap_per_requester"]
    abandon_penalty = w["abandoned_request_penalty"]
    half_life = w["decay_half_life_days"]

    ordered = [e for _, e in sorted(
        ((i, e) for i, e in enumerate(events) if isinstance(e, Mapping)), key=_sort_key
    )]
    file_index = {
        str(p): r for p, r in (files or {}).items() if isinstance(r, Mapping)
    }

    if computed_at is None:
        computed_at = ""
        for event in reversed(ordered):
            ts = event.get("ts")
            if isinstance(ts, str) and ts:
                computed_at = ts
                break
    if now is None and half_life > 0:
        now = _to_unix(computed_at)

    # ---- pass one: index the log ------------------------------------------------
    author_of_event = {}  # type: Dict[str, str]
    seq_of_event = {}  # type: Dict[str, int]
    names = {}  # type: Dict[str, str]
    agents = set()  # type: set
    supersede_claims = []  # type: List[Tuple[str, str, str]]
    contribution_ids = set()  # type: set
    blame = {}  # type: Dict[str, Dict[str, Any]]
    #: SPEC §15: who asked for what, who took it on, and who answered. Built in pass
    #: one because the award in pass two needs the *requester* and the *priority*,
    #: and both of those live on the `request.create` the result refers back to.
    requests = {}  # type: Dict[str, Dict[str, Any]]
    accepted_by = {}  # type: Dict[str, str]
    answered = set()  # type: set

    for event in ordered:
        actor = event.get("actor")
        if not isinstance(actor, str) or not actor:
            continue
        seq = _as_int(event.get("seq"), 0)
        event_id = event.get("id")
        if isinstance(event_id, str) and event_id:
            author_of_event[event_id] = actor
            seq_of_event[event_id] = seq
        if actor != HUB_ACTOR:
            agents.add(actor)
        etype = event.get("type")
        body = event.get("body")
        if not isinstance(body, Mapping):
            body = {}
        if etype == "agent.hello":
            name = body.get("name")
            if isinstance(name, str) and name.strip():
                names[actor] = name.strip()
        elif etype == "knowledge.contribution":
            if isinstance(event_id, str) and event_id:
                contribution_ids.add(event_id)
                target = body.get("supersedes")
                if isinstance(target, str) and target:
                    supersede_claims.append((actor, event_id, target))
        elif etype == "file.put":
            path = body.get("path")
            if isinstance(path, str) and path:
                blame[path] = {"author": actor, "seq": seq, "ts": event.get("ts", "")}
        elif etype == "file.move":
            src, dst = body.get("from"), body.get("to")
            if isinstance(src, str):
                blame.pop(src, None)
            if isinstance(dst, str) and dst:
                blame[dst] = {"author": actor, "seq": seq, "ts": event.get("ts", "")}
        elif etype == "file.delete":
            path = body.get("path")
            if isinstance(path, str):
                blame.pop(path, None)
        elif etype == "request.create":
            req_id = body.get("id")
            if isinstance(req_id, str) and req_id and req_id not in requests:
                capability = body.get("capability")
                requests[req_id] = {
                    "from": actor,
                    "to": body.get("to") if isinstance(body.get("to"), str) else "any",
                    "priority": _as_int(body.get("priority"), 3),
                    "what": capability if isinstance(capability, str) and capability
                            else "a free-form instruction",
                    "seq": seq,
                }
        elif etype == "request.accept":
            req_id = body.get("id")
            if isinstance(req_id, str) and req_id and req_id not in accepted_by:
                accepted_by[req_id] = actor
        elif etype in ("request.result", "request.decline"):
            req_id = body.get("id")
            if isinstance(req_id, str) and req_id:
                answered.add(req_id)

    # `supersedes` is a *replacement*, so a whole chain of restatements is worth one
    # contribution: the links are unioned into a group and only the newest member of each
    # group scores. Without that, re-posting the same contribution fifty times, each
    # superseding the original, would pay fifty times over — which is the first gaming
    # move the rule is supposed to prevent.
    #
    # The edge is only honoured when the *same* agent issued it. Otherwise any agent could
    # delete a rival's contributions from the scoreboard by claiming to have replaced them.
    parent = {}  # type: Dict[str, str]

    def _find(node: str) -> str:
        root = node
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(node, node) != root:  # path compression
            parent[node], node = root, parent[node]
        return root

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    for actor, superseder, target in supersede_claims:
        if target not in contribution_ids:
            continue  # superseding something that is not a scored contribution is a no-op
        if author_of_event.get(target) != actor:
            continue
        parent.setdefault(superseder, superseder)
        parent.setdefault(target, target)
        _union(superseder, target)

    newest_in_group = {}  # type: Dict[str, Tuple[int, str]]
    for event_id in parent:
        root = _find(event_id)
        candidate = (seq_of_event.get(event_id, 0), event_id)
        if candidate > newest_in_group.get(root, (-1, "")):
            newest_in_group[root] = candidate
    survivors = set(eid for _, eid in newest_in_group.values())
    superseded = set(eid for eid in parent if eid not in survivors)

    for record in file_index.values():
        author = record.get("author")
        if isinstance(author, str) and author and author != HUB_ACTOR:
            agents.add(author)
    for info in blame.values():
        agents.add(info["author"])
    agents.discard(HUB_ACTOR)

    scorers = {agent: _Scorer() for agent in agents}

    def scorer(agent_id: str) -> Optional[_Scorer]:
        if agent_id == HUB_ACTOR:
            return None
        if agent_id not in scorers:
            scorers[agent_id] = _Scorer()
        return scorers[agent_id]

    # ---- pass two: award --------------------------------------------------------
    tasks_claimed_by = {}  # type: Dict[str, str]
    tasks_paid = set()  # type: set
    chat_earned = {agent: 0.0 for agent in agents}  # type: Dict[str, float]
    chat_suppressed = {}  # type: Dict[str, Dict[str, Any]]
    #: (provider, requester) -> points already paid. The cap is per *pair*, which is
    #: what stops two agents farming each other with trivial back-and-forth.
    service_earned = {}  # type: Dict[Tuple[str, str], float]
    service_paid = set()  # type: set

    for event in ordered:
        actor = event.get("actor")
        if not isinstance(actor, str) or not actor or actor == HUB_ACTOR:
            continue
        etype = event.get("type")
        if not isinstance(etype, str):
            continue
        seq = _as_int(event.get("seq"), 0)
        event_id = event.get("id") if isinstance(event.get("id"), str) else ""
        body = event.get("body")
        if not isinstance(body, Mapping):
            body = {}
        decay = _decay_factor(event.get("ts"), now, half_life)
        me = scorer(actor)
        if me is None:
            continue

        if etype == "knowledge.contribution":
            kind = body.get("kind")
            kind = kind.strip().lower() if isinstance(kind, str) else ""
            title = body.get("title")
            title = title if isinstance(title, str) else ""
            if event_id and event_id in superseded:
                me.add(
                    "contributions",
                    seq,
                    "superseded contribution {0!r} (no points)".format(_clip(title)),
                    0.0,
                    event_id=event_id,
                    kind=kind,
                    superseded=True,
                )
            elif kind not in contribution_weights:
                me.add(
                    "contributions",
                    seq,
                    "contribution of unweighted kind {0!r}: {1!r}".format(kind, _clip(title)),
                    0.0,
                    event_id=event_id,
                    kind=kind,
                )
            else:
                base = _as_float(contribution_weights[kind], 0.0)
                me.add(
                    "contributions",
                    seq,
                    "{0}: {1!r}".format(kind, _clip(title)),
                    base * decay,
                    event_id=event_id,
                    decay=decay,
                    kind=kind,
                )

        elif etype == "task.claim":
            task_id = body.get("id")
            if isinstance(task_id, str) and task_id:
                tasks_claimed_by[task_id] = actor

        elif etype == "task.release":
            task_id = body.get("id")
            if isinstance(task_id, str) and tasks_claimed_by.get(task_id) == actor:
                tasks_claimed_by.pop(task_id, None)

        elif etype == "task.done":
            task_id = body.get("id")
            task_id = task_id if isinstance(task_id, str) else ""
            holder = tasks_claimed_by.get(task_id)
            if not task_id:
                pass
            elif task_id in tasks_paid:
                me.add(
                    "delivery",
                    seq,
                    "task {0} was already completed (no points)".format(task_id),
                    0.0,
                    event_id=event_id,
                    task=task_id,
                )
            elif holder != actor:
                me.add(
                    "delivery",
                    seq,
                    "task {0} closed but not claimed by this agent (no points)".format(task_id),
                    0.0,
                    event_id=event_id,
                    task=task_id,
                )
            else:
                tasks_paid.add(task_id)
                me.add(
                    "delivery",
                    seq,
                    "delivered task {0}".format(task_id),
                    done_points * decay,
                    event_id=event_id,
                    decay=decay,
                    task=task_id,
                )

        elif etype == "request.result":
            _award_service(
                me, body, actor, seq, event_id, decay,
                requests=requests,
                accepted_by=accepted_by,
                earned=service_earned,
                paid=service_paid,
                base_points=service_points,
                priority_bonus=service_bonus,
                cap=service_cap,
            )

        if etype in ("chat.message", "knowledge.contribution"):
            # SPEC §9: influence is "times *another* agent cited this agent's event".
            seen = set()  # type: set
            for ref in _refs_of(body):
                kind = ref.get("kind")
                value = ref.get("value")
                if not isinstance(value, str) or not value:
                    continue
                key = (kind, value)
                if key in seen:
                    continue  # one event citing the same thing twice is one citation
                seen.add(key)
                if kind == "event":
                    target_agent = author_of_event.get(value)
                    target_seq = seq_of_event.get(value, 0)
                    label = "cited by {0} in seq {1}".format(_short(actor), seq)
                elif kind == "file":
                    record = file_index.get(value) or blame.get(value) or {}
                    target_agent = record.get("author")
                    target_seq = _as_int(record.get("seq"), 0)
                    label = "file {0!r} cited by {1} in seq {2}".format(
                        _clip(value), _short(actor), seq
                    )
                else:
                    continue
                if not isinstance(target_agent, str) or not target_agent:
                    continue
                if target_agent == actor:
                    continue  # self-citation is worth exactly nothing
                if target_agent == HUB_ACTOR:
                    continue
                target = scorer(target_agent)
                if target is None:
                    continue
                target.add(
                    "influence",
                    target_seq or seq,
                    label,
                    citation_points * decay,
                    event_id=value if kind == "event" else "",
                    decay=decay,
                    cited_by=actor,
                    citing_seq=seq,
                    ref_kind=kind,
                    ref_value=value,
                )

        if etype == "chat.message":
            earned = chat_earned.get(actor, 0.0)
            room = chat_cap - earned
            award = chat_points * decay
            if room <= 0.0:
                record = chat_suppressed.setdefault(actor, {"count": 0, "seq": seq})
                record["count"] += 1
                record["seq"] = seq
            else:
                award = min(award, room)
                chat_earned[actor] = earned + award
                text = body.get("text")
                me.add(
                    "presence",
                    seq,
                    "chat: {0!r}".format(_clip(text if isinstance(text, str) else "")),
                    award,
                    event_id=event_id,
                    decay=decay,
                )

    # ---- the one negative term --------------------------------------------------
    # SPEC §15.3 calls silently dropping an accepted request the one unforgivable
    # Exchange behaviour, and §9 makes it the only thing in the Ledger that costs
    # points. The charge is raised strictly off the Hub's `request.expired` event:
    # a request still legitimately in flight when `compute` runs has *not* been
    # abandoned, and guessing otherwise would punish an agent for being slow.
    for event in ordered:
        if event.get("type") != "request.expired":
            continue
        body = event.get("body")
        if not isinstance(body, Mapping):
            continue
        req_id = body.get("id")
        if not isinstance(req_id, str) or req_id in answered:
            continue
        provider = accepted_by.get(req_id)
        if not isinstance(provider, str) or not provider or provider == HUB_ACTOR:
            continue  # nobody accepted it: nobody promised anything
        me = scorer(provider)
        if me is None:
            continue
        info = requests.get(req_id, {})
        requester = info.get("from", "")
        if requester == provider:
            continue  # abandoning your own request harms nobody but yourself
        seq = _as_int(event.get("seq"), 0)
        me.add(
            "service",
            seq,
            "accepted request {0} from {1} ({2}) and never answered it".format(
                req_id, _short(requester) if requester else "another agent",
                _clip(str(info.get("what", "unknown work")), 40),
            ),
            abandon_penalty,
            event_id=event.get("id") if isinstance(event.get("id"), str) else "",
            request=req_id,
            abandoned=True,
            penalty=True,
        )

    for actor, record in chat_suppressed.items():
        me = scorer(actor)
        if me is None:
            continue
        me.add(
            "presence",
            record["seq"],
            "{0} further chat message(s) earned nothing: presence cap of {1:g} reached".format(
                record["count"], chat_cap
            ),
            0.0,
            capped=True,
            suppressed=record["count"],
        )

    # ---- authored substance -----------------------------------------------------
    for path in sorted(file_index):
        record = file_index[path]
        if CONFLICT_MARKER in path:
            continue  # a preserved divergence is the same content twice (SPEC §7.6)
        author = record.get("author")
        if not isinstance(author, str) or not author:
            author = blame.get(path, {}).get("author")
        if not isinstance(author, str) or not author or author == HUB_ACTOR:
            continue
        me = scorer(author)
        if me is None:
            continue
        file_seq = _as_int(record.get("seq"), 0) or _as_int(
            blame.get(path, {}).get("seq"), 0
        )
        if record.get("binary"):
            me.add(
                "authored",
                file_seq,
                "{0}: binary, lines not counted".format(path),
                0.0,
                path=path,
                binary=True,
            )
            continue
        lines = record.get("lines")
        if lines is None:
            me.add(
                "authored",
                file_seq,
                "{0}: no line count recorded, not scored".format(path),
                0.0,
                path=path,
            )
            continue
        total_lines = max(0, _as_int(lines, 0))
        counted = min(total_lines, max(0, line_cap))
        label = "{0}: {1} surviving line(s)".format(path, total_lines)
        if counted < total_lines:
            label += " (capped at {0})".format(line_cap)
        # Surviving lines are deliberately NOT decayed: they describe the workspace as it
        # stands today, not something that happened in the past. Code that is still there
        # has not got less true with age.
        me.add(
            "authored",
            file_seq,
            label,
            counted * line_points,
            path=path,
            lines=total_lines,
            counted_lines=counted,
        )

    # ---- assemble ---------------------------------------------------------------
    lines_out = []  # type: List[LedgerLine]
    for agent_id in sorted(scorers):
        acc = scorers[agent_id]
        components = {c: _round(acc.points[c]) for c in COMPONENTS}
        evidence = {}
        for component in COMPONENTS:
            entries = sorted(acc.evidence[component], key=lambda e: (e["seq"], e["label"]))
            if max_evidence and len(entries) > max_evidence:
                dropped = entries[max_evidence:]
                entries = entries[:max_evidence] + [
                    {
                        "seq": dropped[-1]["seq"],
                        "label": "{0} further entries not shown".format(len(dropped)),
                        "points": _round(sum(_as_float(e["points"]) for e in dropped)),
                        "id": "",
                        "truncated": True,
                    }
                ]
            evidence[component] = entries
        lines_out.append(
            LedgerLine(
                agent_id=agent_id,
                name=names.get(agent_id, agent_id),
                total=_round(sum(components.values())),
                share=0.0,
                components=components,
                evidence=evidence,
            )
        )

    for line, share in zip(lines_out, _apportion([ln.total for ln in lines_out])):
        line.share = share
    lines_out.sort(key=lambda ln: (-ln.total, ln.name.lower(), ln.agent_id))

    return LedgerResult(
        lines=lines_out,
        weights=w,
        computed_at=computed_at or "",
        event_count=len(ordered),
    )


def _award_service(
    me: "_Scorer",
    body: Mapping[str, Any],
    actor: str,
    seq: int,
    event_id: str,
    decay: float,
    *,
    requests: Mapping[str, Mapping[str, Any]],
    accepted_by: Mapping[str, str],
    earned: Dict[Tuple[str, str], float],
    paid: set,
    base_points: float,
    priority_bonus: float,
    cap: float
) -> None:
    """Score one ``request.result`` as the SPEC §15 **service** component.

    The defences, in the order an agent would try to get round them:

    * *Only the provider is paid*, and only when the request was addressed to it or
      it is the agent that accepted an open ``to: "any"`` offer. Emitting a result
      for somebody else's request buys nothing.
    * *Serving yourself is worth nothing*, exactly as self-citation is.
    * *Once per request id.* A provider that re-sends its result is idempotent, not
      twice as useful.
    * *Failures pay nothing but cost nothing.* ``ok: false`` is still an answer, so
      it earns no points and incurs no abandonment penalty. A provider should never
      be better off staying silent than admitting a failure.
    * *Capped per requester-pair.* Past ``service_cap_per_requester`` from one
      caller the well runs dry, which is what makes mutual farming pointless. The
      capped result still gets an evidence line saying so, because a point that
      was not awarded is as much a part of the explanation as one that was.

    ``priority`` scales the award additively (``base + bonus × (priority − 3)``)
    rather than multiplicatively: a priority-1 errand is worth less than a
    priority-5 one, but it is never worth *nothing*, which a multiplier would make
    it at the bottom of the scale.
    """
    req_id = body.get("id")
    if not isinstance(req_id, str) or not req_id:
        return
    info = requests.get(req_id)
    if info is None:
        return  # a result for a request that is not in this log; nothing to price
    holder = accepted_by.get(req_id, "")
    addressed = info.get("to")
    if holder:
        if holder != actor:
            return
    elif addressed != actor:
        return
    requester = info.get("from", "")
    if not isinstance(requester, str) or not requester or requester == actor:
        return
    if req_id in paid:
        return
    paid.add(req_id)

    what = _clip(str(info.get("what", "work")), 40)
    if not body.get("ok", True):
        me.add(
            "service",
            seq,
            "answered {0} for {1} with a failure ({2}): no points, no penalty".format(
                req_id, _short(requester), what
            ),
            0.0,
            event_id=event_id,
            request=req_id,
            requester=requester,
        )
        return

    priority = _as_int(info.get("priority"), 3)
    priority = max(1, min(5, priority))
    award = max(0.0, base_points + priority_bonus * (priority - 3)) * decay

    key = (actor, requester)
    already = earned.get(key, 0.0)
    room = cap - already
    if room <= 0.0:
        me.add(
            "service",
            seq,
            "served {0} again ({1}): the {2:g}-point cap for one requester is reached".format(
                _short(requester), what, cap
            ),
            0.0,
            event_id=event_id,
            request=req_id,
            requester=requester,
            capped=True,
        )
        return
    award = min(award, room)
    earned[key] = already + award
    me.add(
        "service",
        seq,
        "served {0}: {1} (priority {2})".format(_short(requester), what, priority),
        award,
        event_id=event_id,
        decay=decay,
        request=req_id,
        requester=requester,
        priority=priority,
    )


def _decay_factor(ts: Any, now: Optional[float], half_life_days: float) -> float:
    if not half_life_days or half_life_days <= 0 or now is None:
        return 1.0
    when = _to_unix(ts)
    if when is None:
        return 1.0
    age_days = (now - when) / _SECONDS_PER_DAY
    if age_days <= 0.0:
        return 1.0
    return float(0.5 ** (age_days / float(half_life_days)))


def _clip(text: str, limit: int = 72) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _short(agent_id: str) -> str:
    return agent_id[-6:] if len(agent_id) > 6 else agent_id
