"""The Exchange: capability lending and delegated work (SPEC §15), pure logic.

An agent is only interesting to the others because of what it alone can reach — a
skill, an MCP server, a bench wired to real hardware, a private credential, a GPU.
This module is the arithmetic and the judgement behind lending that out: what a
capability *is*, who may ask for it, what state a request is in, and whether a
caller's input is shaped the way the provider said it must be.

**Everything here is pure.** No filesystem (except the two explicitly-named
:meth:`Policy.load`/:meth:`Policy.save`), no network, no clock, no randomness
(except the one id minter, which is isolated and never used by a decision path).
The Hub, the client daemon and the test suite all run the *same* code over the
*same* events, which is the only way the three of them can agree about who agreed
to what. Time arrives as an injected ``now``; state arrives as events.

Three pieces carry the weight:

:class:`Registry`
    The merged catalogue. ``announce`` is **total per agent** — it replaces that
    agent's whole previous list — which is why re-announcing after a reconnect is
    both correct and cheap, and why a capability that quietly disappeared (the USB
    device was unplugged) stops being offered without anyone sending a diff.

:func:`validate_input`
    A security boundary, not a convenience. It is handed a schema written by one
    agent and a value written by another, and both are hostile until proven
    otherwise. It **never raises**, it bounds recursion depth *and* total work, and
    it rejects any schema construct outside the documented subset rather than
    waving it through. Silently passing a value nobody could check is exactly the
    failure this function exists to prevent.

:class:`Policy`
    The consent model of SPEC §15.4. A request is a proposal, not a command. The
    rules that stop an agent becoming a confused deputy are implemented as hard
    ceilings applied *after* the policy file has had its say, so a policy file
    cannot talk the runtime into auto-running something dangerous. See
    :meth:`Policy.evaluate` for the order and the reasoning behind each step.

:class:`RequestTracker`
    The §15.3 state machine, driven entirely from the event log. Illegal
    transitions are recorded and ignored, never raised: this runs inside the Hub's
    ingest path and inside the client's stream thread, and a crash in either is far
    worse than a request that stays in the wrong state for one event.

This module deliberately imports nothing from the rest of ``parley``. It is shared
by the Hub, the client and the Deck's snapshot builder, and keeping it free-standing
means none of them can accidentally couple the Exchange to their own layer.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

LOG = logging.getLogger("parley.exchange")

__all__ = [
    "SAFETY_LEVELS",
    "CAPABILITY_KINDS",
    "DECLINE_CODES",
    "REQUEST_STATES",
    "TERMINAL_STATES",
    "OUTPUT_KINDS",
    "COST_LEVELS",
    "EVENT_TYPES",
    "DEFAULT_TIMEOUT_S",
    "MAX_TIMEOUT_S",
    "INSTRUCTION_CAPABILITY",
    "BadRequestSpec",
    "Capability",
    "Registry",
    "validate_input",
    "Decision",
    "Policy",
    "RequestTracker",
    "make_request",
    "new_request_id",
    "is_request_id",
]

#: SPEC §15.1. Closed set; anything else is treated as ``dangerous`` (fail closed).
SAFETY_LEVELS: Tuple[str, ...] = ("safe", "guarded", "dangerous")

#: SPEC §15.1. ``human`` means "a person at this machine will do it".
CAPABILITY_KINDS: Tuple[str, ...] = (
    "skill", "mcp", "hardware", "tool", "data", "compute", "human",
)

#: SPEC §15.3 ``request.decline.code``, plus ``cancelled`` for the terminal result
#: a provider must emit when the caller withdraws after an accept.
DECLINE_CODES: Tuple[str, ...] = (
    "unknown_capability", "bad_input", "policy", "busy", "unsafe",
    "offline", "needs_human", "cancelled", "other",
)

#: SPEC §15.3. ``pending`` and ``accepted`` are live; the rest are terminal.
REQUEST_STATES: Tuple[str, ...] = (
    "pending", "accepted", "done", "failed", "declined", "expired", "cancelled",
)

TERMINAL_STATES: Tuple[str, ...] = ("done", "failed", "declined", "expired", "cancelled")

OUTPUT_KINDS: Tuple[str, ...] = ("text", "json", "file", "none")

COST_LEVELS: Tuple[str, ...] = ("cheap", "moderate", "expensive")

#: Every event type the Exchange puts on the wire (SPEC §4.9). Exported so the
#: protocol layer, the Hub's router and the Deck can all agree on one list.
EVENT_TYPES: Tuple[str, ...] = (
    "capability.announce", "capability.revoke",
    "request.create", "request.accept", "request.decline",
    "request.progress", "request.result", "request.cancel",
    "request.taken", "request.expired",
)

#: Hub-authored event types in the set above: no agent may author these.
HUB_AUTHORED_TYPES: Tuple[str, ...] = ("request.taken", "request.expired")

DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 86400
DEFAULT_PRIORITY = 3

#: The name a free-form ``instruction`` request matches under in ``policy.json``.
#: A policy can therefore write ``{"capability": "instruction", "action": "deny"}``
#: to refuse natural-language delegation outright while still lending its skills.
INSTRUCTION_CAPABILITY = "instruction"

#: Bounds for :func:`validate_input`. These are not tuning knobs: they are what
#: stops a self-referential schema or a 10 000-deep value from costing the agent
#: its stack or its afternoon.
MAX_SCHEMA_DEPTH = 16
#: One step is roughly one value checked. A §2-legal 256 KiB body holds at most
#: about 128 000 JSON values, so this is generous for honest input while still
#: capping the worst case at a fraction of a second.
MAX_VALIDATION_STEPS = 50000
MAX_PROBLEMS = 24

#: Longest text we will echo back inside a problem message. A hostile value is not
#: allowed to turn a one-line error into a megabyte.
_CLIP = 64

#: How many terminal request records a tracker keeps before forgetting the oldest.
#: The log is the record; this is a cache in front of it.
MAX_RETAINED_TERMINAL = 500

#: How many of those the snapshot shows under ``requests.recent``.
RECENT_IN_SNAPSHOT = 50

#: Anomalies recorded per request before we stop writing them down.
MAX_ANOMALIES = 8

_HEX = "0123456789abcdef"


class BadRequestSpec(ValueError):
    """:func:`make_request` was asked to build a request that cannot be valid.

    A ``ValueError`` subclass rather than a ``parley.errors`` type on purpose: this
    module has no intra-package imports (see the module docstring), and callers
    that want a :class:`parley.errors.BadEvent` wrap it at the boundary.
    """


# --------------------------------------------------------------------------- ids


def new_request_id() -> str:
    """``req_`` + 8 hex (SPEC §15.3).

    The one place in this module that touches randomness. It is deliberately not
    called from any decision path — :func:`make_request` takes ``req_id`` so a test
    (or a retry that must stay idempotent) can pin it.
    """
    return "req_" + secrets.token_hex(4)


def is_request_id(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("req_"):
        return False
    body = value[4:]
    if not 4 <= len(body) <= 64:
        return False
    return all(ch in _HEX for ch in body)


# --------------------------------------------------------------------------- helpers


def _clip(text: Any, limit: int = _CLIP) -> str:
    try:
        flat = " ".join(str(text).split())
    except Exception:  # noqa: BLE001 - __str__ on a hostile object may do anything
        return "<unprintable>"
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if out != out or out in (float("inf"), float("-inf")):
        return default
    return out


def _as_str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _glob(pattern: str, value: str) -> bool:
    """Case-sensitive glob. ``fnmatch.fnmatch`` folds case on Windows; ids do not."""
    try:
        return fnmatch.fnmatchcase(value, pattern)
    except Exception:  # noqa: BLE001 - a pathological pattern must not raise
        return False


def _is_literal(pattern: str) -> bool:
    """True when a policy pattern names exactly one thing.

    SPEC §15.4 rule 1 says a ``guarded`` capability may be auto-accepted only when
    the policy "explicitly allows that specific capability for that specific
    requester". A pattern with a wildcard in it is by definition not specific, so
    this is the test that distinguishes a blanket grant from a named one.
    """
    return bool(pattern) and not any(ch in pattern for ch in "*?[")


# --------------------------------------------------------------------------- capability


@dataclass
class Capability:
    """One thing an agent will do for the others (SPEC §15.1).

    ``description`` is the highest-value field in the whole Exchange: it is what
    *another model* reads to decide whether to ask. ``docs/EXCHANGE.md`` has worked
    good and bad examples; :meth:`validate` only enforces that it exists.
    """

    name: str
    title: str = ""
    kind: str = "tool"
    description: str = ""
    input_schema: Optional[dict] = None
    output: str = "text"
    safety: str = "guarded"
    cost: str = "moderate"
    concurrency: int = 1
    exclusive: bool = False
    avg_duration_s: float = 0.0
    examples: List[dict] = field(default_factory=list)
    agent_id: str = ""
    agent_name: str = ""

    # -- shape ---------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        """Wire form. ``None`` values are omitted, per SPEC §1.3."""
        out: Dict[str, Any] = {
            "name": self.name,
            "title": self.title,
            "kind": self.kind,
            "description": self.description,
            "output": self.output,
            "safety": self.safety,
            "cost": self.cost,
            "concurrency": int(self.concurrency),
            "exclusive": bool(self.exclusive),
        }
        if self.input_schema is not None:
            out["input_schema"] = self.input_schema
        if self.avg_duration_s:
            out["avg_duration_s"] = float(self.avg_duration_s)
        if self.examples:
            out["examples"] = list(self.examples)
        if self.agent_id:
            out["agent_id"] = self.agent_id
        if self.agent_name:
            out["agent_name"] = self.agent_name
        return out

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Capability":
        """Build from a wire object, tolerantly.

        Nothing is coerced into looking valid: a bad ``safety`` survives as-is so
        :meth:`validate` can report it and :meth:`effective_safety` can fail closed
        on it. Quietly rewriting ``safety: "sfae"`` to ``"safe"`` would be the exact
        mistake SPEC §15.1 calls the worst thing an agent can do in the Exchange.
        """
        if not isinstance(d, Mapping):
            return cls(name="")
        schema = d.get("input_schema")
        examples = d.get("examples")
        return cls(
            name=_as_str(d.get("name")),
            title=_as_str(d.get("title")),
            kind=_as_str(d.get("kind"), "tool"),
            description=_as_str(d.get("description")),
            input_schema=dict(schema) if isinstance(schema, Mapping) else None,
            output=_as_str(d.get("output"), "text"),
            safety=_as_str(d.get("safety"), "guarded"),
            cost=_as_str(d.get("cost"), "moderate"),
            concurrency=max(1, _as_int(d.get("concurrency"), 1)),
            exclusive=bool(d.get("exclusive")),
            avg_duration_s=max(0.0, _as_float(d.get("avg_duration_s"), 0.0)),
            examples=[e for e in examples if isinstance(e, Mapping)] if isinstance(examples, list) else [],
            agent_id=_as_str(d.get("agent_id")),
            agent_name=_as_str(d.get("agent_name")),
        )

    def validate(self) -> List[str]:
        """``[]`` when the announcement is well-formed; otherwise what is wrong."""
        problems: List[str] = []
        if not self.name or not isinstance(self.name, str):
            problems.append("name is required")
        elif len(self.name) > 96:
            problems.append("name is longer than 96 characters")
        elif self.name != self.name.lower():
            problems.append("name must be lowercase (SPEC §15.1 `namespace.verb`)")
        elif not all(ch.isalnum() or ch in "._-" for ch in self.name):
            problems.append("name may only contain letters, digits, '.', '_' and '-'")
        if not self.title:
            problems.append("title is required: it is the line a human reads on the Deck")
        if not self.description:
            problems.append(
                "description is required: it is what another model reads to decide "
                "whether to ask, and a capability without one goes unused or misused"
            )
        if self.kind not in CAPABILITY_KINDS:
            problems.append("kind must be one of %s" % ", ".join(CAPABILITY_KINDS))
        if self.safety not in SAFETY_LEVELS:
            problems.append(
                "safety must be one of %s (an unrecognised value is treated as "
                "dangerous)" % ", ".join(SAFETY_LEVELS)
            )
        if self.output not in OUTPUT_KINDS:
            problems.append("output must be one of %s" % ", ".join(OUTPUT_KINDS))
        if self.cost not in COST_LEVELS:
            problems.append("cost must be one of %s" % ", ".join(COST_LEVELS))
        if self.concurrency < 1:
            problems.append("concurrency must be at least 1")
        if self.input_schema is not None and not isinstance(self.input_schema, Mapping):
            problems.append("input_schema must be a JSON object when present")
        elif self.input_schema is not None:
            problems.extend(
                "input_schema: " + p for p in schema_problems(self.input_schema)
            )
        return problems

    def effective_safety(self) -> str:
        """The safety level consent actually uses.

        An unrecognised value becomes ``dangerous``. That is the only safe
        direction: a typo in ``safety`` must cost a human approval, never buy an
        auto-accept.
        """
        return self.safety if self.safety in SAFETY_LEVELS else "dangerous"


# --------------------------------------------------------------------------- registry


class Registry:
    """The merged capability catalogue across every agent (SPEC §15.2).

    Not thread-safe by itself; the Hub holds its own state lock around it and the
    client only touches it from one thread. Keeping the lock outside means a caller
    can take a consistent snapshot of the registry *and* the request tracker
    together, which is what ``/v1/state`` needs.
    """

    def __init__(self) -> None:
        self._by_agent: Dict[str, Dict[str, Capability]] = {}
        self._names: Dict[str, str] = {}

    # -- mutation ------------------------------------------------------------
    def announce(self, agent_id: str, agent_name: str, caps: Sequence[Mapping[str, Any]]) -> None:
        """Replace ``agent_id``'s entire catalogue (SPEC §15.1: announce is total).

        Malformed entries are dropped with a warning rather than rejecting the whole
        announcement: one bad capability must not take the other nine off the Deck.
        """
        if not isinstance(agent_id, str) or not agent_id:
            return
        if isinstance(agent_name, str) and agent_name:
            self._names[agent_id] = agent_name
        name = self._names.get(agent_id, "")
        table: Dict[str, Capability] = {}
        if isinstance(caps, (list, tuple)):
            for raw in caps:
                if not isinstance(raw, Mapping):
                    continue
                cap = Capability.from_dict(raw)
                cap.agent_id = agent_id
                cap.agent_name = name
                problems = cap.validate()
                if problems:
                    LOG.warning(
                        "ignoring capability %r announced by %s: %s",
                        cap.name or "<unnamed>", agent_id, "; ".join(problems[:3]),
                    )
                    continue
                table[cap.name] = cap
        self._by_agent[agent_id] = table

    def revoke(self, agent_id: str, names: Sequence[str]) -> None:
        """Withdraw named capabilities — the USB device was unplugged (SPEC §15.1)."""
        table = self._by_agent.get(agent_id)
        if not table or not isinstance(names, (list, tuple)):
            return
        for name in names:
            if isinstance(name, str):
                table.pop(name, None)

    def drop_agent(self, agent_id: str) -> None:
        """An agent going offline implicitly revokes everything it announced."""
        self._by_agent.pop(agent_id, None)

    # -- reads ---------------------------------------------------------------
    def find(self, name: str, *, agent_id: str = "") -> List[Capability]:
        """Every announcement of ``name``, optionally from one agent only.

        A list, not an Optional: two agents can genuinely both hold ``zdrive.search``
        and the caller is the one entitled to choose between them.
        """
        out: List[Capability] = []
        for owner, table in sorted(self._by_agent.items()):
            if agent_id and owner != agent_id:
                continue
            cap = table.get(name)
            if cap is not None:
                out.append(cap)
        return out

    def all(self) -> List[Capability]:
        out: List[Capability] = []
        for owner in sorted(self._by_agent):
            for name in sorted(self._by_agent[owner]):
                out.append(self._by_agent[owner][name])
        return out

    def agents(self) -> List[str]:
        return sorted(self._by_agent)

    def to_dict(
        self,
        *,
        online: Optional[Mapping[str, bool]] = None,
        in_flight: Optional[Mapping[str, int]] = None,
    ) -> Dict[str, Any]:
        """The §15.2 wire shape, straight into ``/v1/state`` and ``/v1/capabilities``.

        ``in_flight`` is looked up first by ``"<agent_id>/<name>"`` and then by
        ``"<agent_id>"``, so a caller can supply per-capability counts where it has
        them and per-agent counts where it does not.
        """
        rows: List[Dict[str, Any]] = []
        for cap in self.all():
            row = cap.to_dict()
            row["agent_id"] = cap.agent_id
            row["agent_name"] = cap.agent_name
            row["online"] = bool((online or {}).get(cap.agent_id, True))
            counts = in_flight or {}
            key = "%s/%s" % (cap.agent_id, cap.name)
            row["in_flight"] = _as_int(
                counts.get(key, counts.get(cap.agent_id, 0)), 0
            )
            rows.append(row)
        return {"capabilities": rows, "count": len(rows)}


# --------------------------------------------------------------------------- schema


#: The JSON-Schema subset SPEC §15.1 defines, plus ``additionalProperties``.
SCHEMA_KEYWORDS = frozenset(
    ("type", "properties", "required", "enum", "minimum", "maximum",
     "items", "additionalProperties")
)

#: Annotation-only keywords. They describe, they never constrain, so honouring them
#: as no-ops costs nothing and refusing them would reject honest schemas.
SCHEMA_ANNOTATIONS = frozenset(("description", "title", "default", "examples"))

_TYPE_NAMES = frozenset(
    ("object", "array", "string", "number", "integer", "boolean", "null")
)


def _type_matches(name: str, value: Any) -> bool:
    if name == "object":
        return isinstance(value, Mapping)
    if name == "array":
        return isinstance(value, (list, tuple))
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    return False


def _type_of(value: Any) -> str:
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _type_matches(name, value):
            return name
    return type(value).__name__


def _json_equal(a: Any, b: Any) -> bool:
    """Equality that does not confuse ``True`` with ``1``, and never raises.

    ``True == 1`` in Python, so a naive ``value in enum`` would let ``true`` satisfy
    an ``enum`` of ``[1, 2]``. Comparison itself is wrapped because ``==`` on a
    self-referential or very deep structure recurses in C and can raise.
    """
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    try:
        return bool(a == b)
    except RecursionError:
        return False
    except Exception:  # noqa: BLE001 - a hostile __eq__ is still just "not equal"
        return False


def schema_problems(schema: Any) -> List[str]:
    """Report what is wrong with ``schema`` itself, ignoring any value.

    Used by :meth:`Capability.validate` so a provider learns at announce time that
    its schema is unusable, rather than discovering it when every caller is declined
    with ``bad_input``.
    """
    return _walk_schema(schema, _NOTHING, check_value=False)


class _Nothing(object):
    """Sentinel for "no value supplied"; ``None`` is a legitimate JSON value."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "<nothing>"


_NOTHING = _Nothing()


def validate_input(schema: Optional[Mapping[str, Any]], value: Any) -> List[str]:
    """Validate ``value`` against the provider's own ``input_schema`` (SPEC §15.4 rule 4).

    Returns a list of human-readable problems; ``[]`` means the value is acceptable.
    **Never raises**, whatever is handed to it.

    Three things make this a security boundary rather than a nicety:

    * *Fail closed on the schema.* A schema that is malformed, self-referential, or
      uses a keyword outside the documented subset produces problems, which means the
      value is rejected. The alternative — "I could not check it, so it is fine" —
      is how a provider ends up executing something nobody validated.
    * *Bounded depth.* Descent follows the schema, so a schema that contains itself
      would otherwise loop forever. :data:`MAX_SCHEMA_DEPTH` stops it, and the walk
      is iterative so even a bug in the bound cannot exhaust the interpreter stack.
    * *Bounded work.* :data:`MAX_VALIDATION_STEPS` caps the total node visits, so a
      10 000-element array against a 40-property schema costs a bounded amount of
      CPU instead of becoming a denial of service against the provider.

    ``schema=None`` means the capability declared none, and SPEC §15.1 makes that
    legal — an empty list comes back and the provider is on its own, which is
    exactly why announcing a schema is "strongly recommended".
    """
    if schema is None:
        return []
    return _walk_schema(schema, value, check_value=True)


def _walk_schema(schema: Any, value: Any, *, check_value: bool) -> List[str]:
    problems: List[str] = []
    if not isinstance(schema, Mapping):
        return ["input_schema is not a JSON object, so nothing can be validated against it"]

    # (schema, value, path, depth). An explicit stack, not recursion: a
    # self-referential schema must cost a bounded number of iterations, not the
    # interpreter's stack.
    stack: List[Tuple[Any, Any, str, int]] = [(schema, value, "", 0)]
    steps = 0
    truncated = False

    while stack:
        if len(problems) >= MAX_PROBLEMS:
            truncated = True
            break
        steps += 1
        if steps > MAX_VALIDATION_STEPS:
            problems.append(
                "input is too large or too deeply structured to validate "
                "(over %d checks)" % MAX_VALIDATION_STEPS
            )
            break
        node, node_value, path, depth = stack.pop()
        if depth > MAX_SCHEMA_DEPTH:
            problems.append(
                "%s: schema nests deeper than %d levels, which is not supported"
                % (path or "input", MAX_SCHEMA_DEPTH)
            )
            continue
        try:
            _check_node(node, node_value, path, depth, stack, problems, check_value)
        except Exception as exc:  # noqa: BLE001 - the contract is "never raises"
            LOG.debug("schema walk at %r failed safely: %s", path, exc)
            problems.append(
                "%s: could not be validated against this schema" % (path or "input")
            )

    if len(problems) > MAX_PROBLEMS:
        # One node can produce many problems at once (a `required` list of 200
        # names), so the loop's own check is not enough. A decline message is for
        # a human or a model to read, and neither reads two hundred lines.
        truncated = True
        problems = problems[:MAX_PROBLEMS]
    if truncated:
        problems.append("(further problems not listed)")
    return problems


def _label(path: str) -> str:
    return path or "input"


def _check_node(
    node: Any,
    value: Any,
    path: str,
    depth: int,
    stack: List[Tuple[Any, Any, str, int]],
    problems: List[str],
    check_value: bool,
) -> None:
    """Check one schema node against one value; push children onto ``stack``."""
    where = _label(path)
    if not isinstance(node, Mapping):
        problems.append("%s: schema node is not a JSON object" % where)
        return

    # -- the subset is closed ------------------------------------------------
    try:
        keys = set(node.keys())
    except Exception:  # noqa: BLE001
        problems.append("%s: schema node has unreadable keys" % where)
        return
    unsupported = sorted(
        str(k) for k in keys if k not in SCHEMA_KEYWORDS and k not in SCHEMA_ANNOTATIONS
    )
    if unsupported:
        problems.append(
            "%s: schema uses %s, which is outside the supported subset (%s)"
            % (where, ", ".join(repr(_clip(k, 32)) for k in unsupported[:4]),
               ", ".join(sorted(SCHEMA_KEYWORDS)))
        )
        return  # a schema we do not fully understand never passes a value

    present = value is not _NOTHING and check_value

    # -- type ----------------------------------------------------------------
    declared = node.get("type")
    names: List[str] = []
    if declared is not None:
        if isinstance(declared, str):
            names = [declared]
        elif isinstance(declared, (list, tuple)) and declared:
            names = [t for t in declared if isinstance(t, str)]
            if len(names) != len(declared):
                problems.append("%s: schema `type` list contains a non-string" % where)
                return
        else:
            problems.append("%s: schema `type` must be a string or a list of strings" % where)
            return
        bad = [n for n in names if n not in _TYPE_NAMES]
        if bad:
            problems.append(
                "%s: schema `type` %s is not one of %s"
                % (where, ", ".join(repr(b) for b in bad[:3]), ", ".join(sorted(_TYPE_NAMES)))
            )
            return
        if present and not any(_type_matches(n, value) for n in names):
            problems.append(
                "%s: expected %s, got %s"
                % (where, " or ".join(names), _type_of(value))
            )
            return

    # -- enum ----------------------------------------------------------------
    if "enum" in node:
        allowed = node.get("enum")
        if not isinstance(allowed, (list, tuple)) or not allowed:
            problems.append("%s: schema `enum` must be a non-empty list" % where)
            return
        if len(allowed) > 1000:
            problems.append("%s: schema `enum` has more than 1000 members" % where)
            return
        if present and not any(_json_equal(value, option) for option in allowed):
            shown = ", ".join(_clip(json_or_repr(o), 24) for o in list(allowed)[:8])
            if len(allowed) > 8:
                shown += ", …"
            problems.append("%s: must be one of %s" % (where, shown))
            return

    # -- numeric bounds ------------------------------------------------------
    for key, op, word in (("minimum", "lt", "below"), ("maximum", "gt", "above")):
        if key not in node:
            continue
        bound = node.get(key)
        if isinstance(bound, bool) or not isinstance(bound, (int, float)):
            problems.append("%s: schema `%s` must be a number" % (where, key))
            return
        if bound != bound or bound in (float("inf"), float("-inf")):
            problems.append("%s: schema `%s` must be a finite number" % (where, key))
            return
        if present and isinstance(value, (int, float)) and not isinstance(value, bool):
            if (op == "lt" and value < bound) or (op == "gt" and value > bound):
                problems.append(
                    "%s: %r is %s the %s of %r" % (where, value, word, key, bound)
                )

    # -- object ---------------------------------------------------------------
    properties = node.get("properties")
    if properties is not None and not isinstance(properties, Mapping):
        problems.append("%s: schema `properties` must be a JSON object" % where)
        return
    required = node.get("required")
    if required is not None:
        if not isinstance(required, (list, tuple)) or any(
            not isinstance(r, str) for r in required
        ):
            problems.append("%s: schema `required` must be a list of property names" % where)
            return
    extra_schema = node.get("additionalProperties")
    if extra_schema is not None and not isinstance(extra_schema, (bool, Mapping)):
        problems.append(
            "%s: schema `additionalProperties` must be true, false or a schema" % where
        )
        return

    if present and isinstance(value, Mapping):
        if required:
            for name in required:
                if name not in value:
                    problems.append(
                        "%s: required property %r is missing" % (where, _clip(name, 48))
                    )
        if isinstance(properties, Mapping):
            for name, sub in properties.items():
                if not isinstance(name, str):
                    problems.append("%s: schema `properties` has a non-string key" % where)
                    return
                if name in value:
                    stack.append((sub, value[name], _join(path, name), depth + 1))
        if extra_schema is not None and isinstance(properties, Mapping):
            known = set(properties.keys())
        else:
            known = set()
        if extra_schema is False:
            unknown = sorted(str(k) for k in value.keys() if k not in known)
            if unknown:
                problems.append(
                    "%s: unexpected propert%s %s"
                    % (where, "y" if len(unknown) == 1 else "ies",
                       ", ".join(repr(_clip(u, 32)) for u in unknown[:5]))
                )
        elif isinstance(extra_schema, Mapping):
            for name in sorted(str(k) for k in value.keys()):
                if name not in known:
                    stack.append((extra_schema, value[name], _join(path, name), depth + 1))
    elif not present and isinstance(properties, Mapping):
        # Schema-only walk: still descend so a broken sub-schema is reported.
        for name, sub in properties.items():
            stack.append((sub, _NOTHING, _join(path, str(name)), depth + 1))

    # -- array ----------------------------------------------------------------
    items = node.get("items")
    if items is None:
        return
    if isinstance(items, Mapping):
        if present and isinstance(value, (list, tuple)):
            for index, element in enumerate(value):
                stack.append((items, element, "%s[%d]" % (path or "input", index), depth + 1))
        elif not present:
            stack.append((items, _NOTHING, "%s[]" % (path or "input"), depth + 1))
    elif isinstance(items, (list, tuple)):
        # Tuple form: positional schemas. Elements past the end are unconstrained.
        for index, sub in enumerate(items):
            if present and isinstance(value, (list, tuple)):
                if index < len(value):
                    stack.append((sub, value[index], "%s[%d]" % (path or "input", index), depth + 1))
            elif not present:
                stack.append((sub, _NOTHING, "%s[%d]" % (path or "input", index), depth + 1))
    else:
        problems.append("%s: schema `items` must be a schema or a list of schemas" % where)


def _join(path: str, name: str) -> str:
    name = _clip(name, 48)
    return name if not path else "%s.%s" % (path, name)


def json_or_repr(value: Any) -> str:
    """Render an enum member for an error message, without ever raising."""
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return repr(value)


# --------------------------------------------------------------------------- consent


@dataclass
class Decision:
    """The outcome of :meth:`Policy.evaluate`.

    ``why`` is **requester-safe**: it is shown to the local operator *and* sent back
    as the ``request.decline`` reason, so it must never name a rule, a pattern or
    another agent. ``detail`` is the operator-only half — it goes in the log and on
    the Deck's consent prompt and never on the wire.
    """

    action: str
    why: str
    code: str = "policy"
    detail: str = ""
    safety: str = "guarded"
    retry_after_s: int = 0

    def as_tuple(self) -> Tuple[str, str]:
        return self.action, self.why


@dataclass
class Policy:
    """Local consent policy (SPEC §15.4), read from ``.parley/policy.json``.

    The policy file is advice from the operator, not a command: every hard rule in
    §15.4 is applied *after* it, as a ceiling. That ordering is the whole design.
    A policy file that says ``allow`` for a ``dangerous`` capability is a policy file
    to be overridden, not obeyed — and this class will say so in the log while
    quietly turning that ``allow`` into an ``ask``.
    """

    default: str = "ask"
    auto_accept_safe: bool = True
    rules: List[dict] = field(default_factory=list)
    max_in_flight: int = 4
    max_per_requester_per_hour: int = 60
    require_reason: bool = True
    never_auto_accept: List[str] = field(default_factory=lambda: ["dangerous"])

    # -- persistence ----------------------------------------------------------
    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Policy":
        if not isinstance(d, Mapping):
            return cls()
        action = _as_str(d.get("default"), "ask")
        if action not in ("allow", "ask", "deny"):
            if "default" in d:
                LOG.warning("policy `default` is %r; falling back to \"ask\"", d.get("default"))
            action = "ask"
        rules: List[dict] = []
        raw_rules = d.get("rules")
        if isinstance(raw_rules, (list, tuple)):
            for raw in raw_rules:
                if not isinstance(raw, Mapping):
                    continue
                rule_action = _as_str(raw.get("action"))
                if rule_action not in ("allow", "ask", "deny"):
                    LOG.warning(
                        "ignoring policy rule with action %r (want allow/ask/deny)",
                        raw.get("action"),
                    )
                    continue
                rules.append({
                    "requester": _as_str(raw.get("requester"), "*") or "*",
                    "capability": _as_str(raw.get("capability"), "*") or "*",
                    "action": rule_action,
                })
        never = d.get("never_auto_accept")
        if isinstance(never, (list, tuple)):
            never_list = [str(n) for n in never]
        else:
            never_list = ["dangerous"]
        if "dangerous" not in never_list:
            # SPEC §15.4 rule 1 is not negotiable, so it is restored rather than
            # honoured as written. Saying so out loud matters more than the fix.
            LOG.warning(
                "policy omits \"dangerous\" from never_auto_accept; restoring it "
                "(SPEC §15.4 forbids auto-accepting a dangerous capability)"
            )
            never_list.append("dangerous")
        return cls(
            default=action,
            auto_accept_safe=bool(d.get("auto_accept_safe", True)),
            rules=rules,
            max_in_flight=max(0, _as_int(d.get("max_in_flight"), 4)),
            max_per_requester_per_hour=max(0, _as_int(d.get("max_per_requester_per_hour"), 60)),
            require_reason=bool(d.get("require_reason", True)),
            never_auto_accept=never_list,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "default": self.default,
            "auto_accept_safe": bool(self.auto_accept_safe),
            "rules": [dict(r) for r in self.rules],
            "max_in_flight": int(self.max_in_flight),
            "max_per_requester_per_hour": int(self.max_per_requester_per_hour),
            "require_reason": bool(self.require_reason),
            "never_auto_accept": list(self.never_auto_accept),
        }

    @classmethod
    def load(cls, workspace: Path) -> "Policy":
        """``<workspace>/.parley/policy.json``; safe defaults when it is absent.

        A corrupt policy file falls back to the defaults rather than to "allow".
        An operator typo must never widen what this agent will do.
        """
        path = Path(workspace) / ".parley" / "policy.json"
        try:
            raw = path.read_bytes()
        except (OSError, ValueError):
            return cls()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            LOG.warning("ignoring unreadable policy at %s (%s); using safe defaults", path, exc)
            return cls()
        if not isinstance(parsed, dict):
            LOG.warning("ignoring policy at %s: expected a JSON object", path)
            return cls()
        return cls.from_dict(parsed)

    def save(self, workspace: Path) -> None:
        path = Path(workspace) / ".parley" / "policy.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8")

    # -- the decision ---------------------------------------------------------
    def decide(
        self,
        *,
        requester: str,
        capability: Optional[Capability],
        is_instruction: bool,
        in_flight: int,
        recent_from_requester: int,
        has_reason: bool,
    ) -> Tuple[str, str]:
        """``(action, why)`` where action is ``allow`` | ``ask`` | ``deny``.

        The 2-tuple SPEC and INTERNAL-API pin down. :meth:`evaluate` returns the
        same decision with the decline code and the operator-only detail attached;
        use that one inside the runtime and this one everywhere the documented
        signature matters.
        """
        return self.evaluate(
            requester=requester,
            capability=capability,
            is_instruction=is_instruction,
            in_flight=in_flight,
            recent_from_requester=recent_from_requester,
            has_reason=has_reason,
        ).as_tuple()

    def evaluate(
        self,
        *,
        requester: str,
        capability: Optional[Capability],
        is_instruction: bool,
        in_flight: int,
        recent_from_requester: int,
        has_reason: bool,
    ) -> Decision:
        """The full consent decision of SPEC §15.4, in order.

        The order is the argument, so here it is explicitly:

        1. **No reason, no request.** ``reason`` is mandatory because the consent
           decision depends on it and because an audit trail without it is worthless.
        2. **An unknown capability cannot be run.** Nothing to validate against,
           nothing to execute.
        3. **The policy file speaks**: first matching rule wins, globs on both
           ``requester`` and ``capability``; no match falls through to ``default``.
        4. **A ``deny`` is final.** Nothing below can widen it.
        5. **Capacity**, then **per-requester rate**. Both answer ``busy``, not
           ``policy``: being full is not a judgement about the caller.
        6. **The ceilings** (rule 1 and rule 2 of §15.4) downgrade an ``allow``:

           * ``dangerous`` can never be ``allow`` — not by rule, not by default,
             not by ``auto_accept_safe``. It becomes ``ask`` so a human decides.
           * ``guarded`` may only be ``allow`` when a rule names *this* requester
             and *this* capability literally. A blanket ``{"requester": "*",
             "capability": "*", "action": "allow"}`` is not "explicitly allows that
             specific capability for that specific requester", so it yields ``ask``.
           * anything listed in ``never_auto_accept`` becomes ``ask``.
           * a free-form ``instruction`` is never ``safe`` — by construction nobody
             schema-validated it — so it carries at least ``guarded``'s ceiling.
        7. **Deny by default beyond ``safe``.** With no rule matching, an unknown
           requester gets an auto-accept only for a ``safe`` capability and only when
           ``auto_accept_safe`` is on. A ``default`` of ``allow`` is honoured as
           ``ask`` for anything else: a newly-enrolled agent starts with no
           entitlements, and a default cannot be an entitlement.

        ``why`` is written to be read by the requester, because it is reused as the
        decline reason. It never names a rule or a pattern.
        """
        safety = self._effective_safety(capability, is_instruction)
        cap_name = capability.name if capability is not None else INSTRUCTION_CAPABILITY
        if is_instruction:
            cap_name = INSTRUCTION_CAPABILITY

        # 1 -- reason
        if self.require_reason and not has_reason:
            return Decision(
                "deny",
                "I need a `reason` saying why you are asking before I will consider a request.",
                code="policy",
                detail="require_reason is on and the request carried no reason",
                safety=safety,
            )

        # 2 -- a capability we do not have
        if capability is None and not is_instruction:
            return Decision(
                "deny",
                "I do not offer a capability by that name; check /v1/capabilities for what I do offer.",
                code="unknown_capability",
                detail="no such capability in this agent's catalogue",
                safety=safety,
            )

        # 3 -- the policy file
        raw_action, matched = self._match(requester, cap_name)

        # 4 -- deny is final
        if raw_action == "deny":
            return Decision(
                "deny",
                "My operator's policy does not let me do this for you.",
                code="policy",
                detail=self._describe(matched, "deny"),
                safety=safety,
            )

        # 5 -- capacity, then rate. Both are "busy", not a judgement about the caller.
        if self.max_in_flight and in_flight >= self.max_in_flight:
            return Decision(
                "deny",
                "I am already running as many requests as I take at once; try again shortly.",
                code="busy",
                detail="in_flight %d >= max_in_flight %d" % (in_flight, self.max_in_flight),
                safety=safety,
                retry_after_s=30,
            )
        if (self.max_per_requester_per_hour
                and recent_from_requester >= self.max_per_requester_per_hour):
            return Decision(
                "deny",
                "You have asked me {0} times in the last hour, which is my limit for one "
                "caller; try again later.".format(self.max_per_requester_per_hour),
                code="busy",
                detail="recent_from_requester %d >= max_per_requester_per_hour %d"
                       % (recent_from_requester, self.max_per_requester_per_hour),
                safety=safety,
                retry_after_s=300,
            )

        # 6/7 -- the ceilings
        return self._ceiling(raw_action, matched, safety, is_instruction)

    # -- internals ------------------------------------------------------------
    @staticmethod
    def _effective_safety(capability: Optional[Capability], is_instruction: bool) -> str:
        """SPEC §15.4 rules 1 and 2, as one number.

        A free-form instruction is at least ``guarded`` whatever it references,
        because nothing validated it. If it *also* names a dangerous capability, the
        dangerous reading wins: the stricter of the two always does.

        With no capability at all the floor is ``guarded``, not ``safe``. That case
        only reaches here for an instruction — a *capability call* naming something
        we do not have is refused as ``unknown_capability`` before consent is asked.
        """
        declared = capability.effective_safety() if capability is not None else "guarded"
        if not is_instruction:
            return declared
        if declared == "dangerous":
            return "dangerous"
        return "guarded"

    def _match(self, requester: str, cap_name: str) -> Tuple[str, Optional[dict]]:
        """First matching rule wins (SPEC §15.4); no match falls through to ``default``."""
        for rule in self.rules:
            if not _glob(rule.get("requester", "*"), requester):
                continue
            if not _glob(rule.get("capability", "*"), cap_name):
                continue
            return rule.get("action", "ask"), rule
        return self.default, None

    @staticmethod
    def _describe(matched: Optional[dict], action: str) -> str:
        if matched is None:
            return "no rule matched; policy default is %r" % action
        return "matched rule requester=%r capability=%r action=%r" % (
            matched.get("requester"), matched.get("capability"), matched.get("action"),
        )

    def _ceiling(
        self,
        raw_action: str,
        matched: Optional[dict],
        safety: str,
        is_instruction: bool,
    ) -> Decision:
        detail = self._describe(matched, raw_action)

        if safety == "dangerous":
            # Rule 1: never auto-accepted, ever, regardless of policy.
            if raw_action == "allow":
                LOG.warning(
                    "policy says allow for a dangerous capability; overriding to ask "
                    "(SPEC §15.4 rule 1). %s", detail
                )
                detail += "; overridden to ask because the capability is dangerous"
            return Decision(
                "ask",
                "This is a dangerous capability: it needs a person here to approve it, "
                "every time.",
                code="needs_human",
                detail=detail,
                safety=safety,
            )

        if safety in self.never_auto_accept:
            if raw_action == "allow":
                detail += "; overridden to ask by never_auto_accept"
            return Decision(
                "ask",
                "My operator has to approve this one before I can run it.",
                code="needs_human",
                detail=detail,
                safety=safety,
            )

        if safety == "guarded":
            specific = (
                matched is not None
                and _is_literal(str(matched.get("requester", "")))
                and _is_literal(str(matched.get("capability", "")))
            )
            if raw_action == "allow" and specific:
                return Decision(
                    "allow",
                    "My operator's policy names you for this capability.",
                    code="policy",
                    detail=detail,
                    safety=safety,
                )
            if raw_action == "allow":
                detail += (
                    "; a guarded capability needs a rule naming this requester and this "
                    "capability literally, so the blanket allow became ask"
                )
            return Decision(
                "ask",
                "This capability is guarded, so my operator approves it per caller.",
                code="needs_human",
                detail=detail,
                safety=safety,
            )

        # safety == "safe" -- SPEC §15.4 rule 3's one standing entitlement: an
        # unknown requester may use read-only capabilities and nothing else.
        if is_instruction:  # pragma: no cover - _effective_safety forbids it
            return Decision(
                "ask", "Free-form instructions always go past my operator first.",
                code="needs_human", detail=detail, safety="guarded",
            )
        if raw_action == "allow":
            return Decision(
                "allow",
                "This is a read-only capability my operator lets anyone use.",
                code="policy",
                detail=detail,
                safety=safety,
            )
        if matched is None and self.auto_accept_safe:
            return Decision(
                "allow",
                "This is a read-only capability my operator lets anyone use.",
                code="policy",
                detail=detail + "; auto_accept_safe",
                safety=safety,
            )
        return Decision(
            "ask", "My operator wants to see this one first.",
            code="needs_human", detail=detail, safety=safety,
        )


# --------------------------------------------------------------------------- tracker


class RequestTracker:
    """The SPEC §15.3 request state machine, driven purely from events.

    The Hub runs one of these over its whole log to materialise ``/v1/state``; the
    client runs one over the stream to know what it owes whom. Both see the same
    events in the same order, so both reach the same conclusion — which is what
    makes "who dropped a request" an answerable question rather than an accusation.

    **Nothing here raises on a bad event.** An accept from an agent the request was
    never addressed to, a result for a request nobody accepted, a second accept on
    a request already taken — all of them are recorded as anomalies on the record
    and otherwise ignored. This code sits inside the Hub's ingest loop and the
    client's stream thread; a traceback in either is a worse outcome than any
    confusion a malformed event could cause.
    """

    def __init__(self) -> None:
        self._records: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        self._terminal_order: List[str] = []
        self._taken: List[Dict[str, Any]] = []

    # -- ingest ---------------------------------------------------------------
    def apply(self, event: Mapping[str, Any], *, now: float) -> None:
        """Fold one event into the machine. Safe to call with anything."""
        if not isinstance(event, Mapping):
            return
        etype = event.get("type")
        if not isinstance(etype, str) or not etype.startswith("request."):
            return
        body = event.get("body")
        if not isinstance(body, Mapping):
            body = {}
        actor = _as_str(event.get("actor"))
        seq = _as_int(event.get("seq"), 0)
        ts = _as_str(event.get("ts"))
        req_id = _as_str(body.get("id"))
        if not req_id:
            return

        try:
            if etype == "request.create":
                self._on_create(req_id, actor, body, seq, ts, now)
                return
            record = self._records.get(req_id)
            if record is None:
                # A response to a request we never saw created. The log is the
                # authority and it will arrive; parking a stub would invent a
                # requester we do not know, so it is dropped with a note.
                LOG.debug("exchange: %s for unknown request %s", etype, req_id)
                return
            if etype == "request.accept":
                self._on_accept(record, actor, body, seq, ts, now)
            elif etype == "request.decline":
                self._on_decline(record, actor, body, seq, ts, now)
            elif etype == "request.progress":
                self._on_progress(record, actor, body, seq, ts)
            elif etype == "request.result":
                self._on_result(record, actor, body, seq, ts, now)
            elif etype == "request.cancel":
                self._on_cancel(record, actor, body, seq, ts, now)
            elif etype == "request.taken":
                self._on_taken(record, body, seq)
            elif etype == "request.expired":
                self._on_expired(record, seq, ts, now)
        except Exception as exc:  # noqa: BLE001 - never break the caller's loop
            LOG.warning("exchange: could not apply %s for %s (%s)", etype, req_id, exc)

    # -- transitions ----------------------------------------------------------
    def _on_create(
        self, req_id: str, actor: str, body: Mapping[str, Any],
        seq: int, ts: str, now: float,
    ) -> None:
        existing = self._records.get(req_id)
        if existing is not None:
            # Idempotent by id (SPEC §15.3). A re-send is the same request.
            if existing.get("from") != actor:
                self._anomaly(existing, "a second create for this id arrived from %s" % _short(actor))
            return
        capability = body.get("capability")
        instruction = body.get("instruction")
        timeout = _as_int(body.get("timeout_s"), DEFAULT_TIMEOUT_S)
        if timeout <= 0:
            timeout = DEFAULT_TIMEOUT_S
        timeout = min(timeout, MAX_TIMEOUT_S)
        record: Dict[str, Any] = {
            "id": req_id,
            "from": actor,
            "to": _as_str(body.get("to"), "any") or "any",
            "capability": capability if isinstance(capability, str) and capability else None,
            "instruction": instruction if isinstance(instruction, str) and instruction else None,
            "input": dict(body["input"]) if isinstance(body.get("input"), Mapping) else {},
            "reason": _as_str(body.get("reason")),
            "expects": _as_str(body.get("expects"), "text"),
            "state": "pending",
            "created_ts": float(now),
            "created_at": ts,
            "accepted_ts": 0.0,
            "accepted_at": "",
            "accepted_by": "",
            "eta_s": 0.0,
            "timeout_s": timeout,
            "priority": _clamp_priority(body.get("priority")),
            "progress": None,
            "note": "",
            "result": None,
            "output_text": "",
            "files": [],
            "error": None,
            "duration_s": 0.0,
            "seq": seq,
            "terminal_ts": 0.0,
            "terminal_at": "",
            "decline_code": "",
            "decline_reason": "",
            "refs": [r for r in body.get("refs", []) if isinstance(r, Mapping)]
                    if isinstance(body.get("refs"), (list, tuple)) else [],
            "anomalies": [],
        }
        self._records[req_id] = record
        self._order.append(req_id)

    def _on_accept(
        self, record: Dict[str, Any], actor: str, body: Mapping[str, Any],
        seq: int, ts: str, now: float,
    ) -> None:
        if not self._may_provide(record, actor):
            self._anomaly(record, "accept from %s, who was not asked" % _short(actor))
            return
        if record["state"] != "pending":
            if record["state"] == "accepted" and record["accepted_by"] == actor:
                return  # a duplicate accept from the holder is a no-op, not an error
            if record["state"] == "accepted":
                # `to: "any"`: the first accept won, the rest must be told.
                self._anomaly(record, "late accept from %s; already taken" % _short(actor))
                self._taken.append({
                    "type": "request.taken",
                    "body": {
                        "id": record["id"],
                        "by": record["accepted_by"],
                        "late": actor,
                        "reason": "another agent accepted this request first",
                    },
                })
                return
            self._anomaly(
                record, "accept from %s after the request was %s" % (_short(actor), record["state"])
            )
            return
        record["state"] = "accepted"
        record["accepted_by"] = actor
        record["accepted_ts"] = float(now)
        record["accepted_at"] = ts
        record["eta_s"] = max(0.0, _as_float(body.get("eta_s"), 0.0))
        record["accept_seq"] = seq

    def _on_decline(
        self, record: Dict[str, Any], actor: str, body: Mapping[str, Any],
        seq: int, ts: str, now: float,
    ) -> None:
        if not self._may_provide(record, actor):
            self._anomaly(record, "decline from %s, who was not asked" % _short(actor))
            return
        if record["state"] in TERMINAL_STATES:
            self._anomaly(
                record, "decline from %s after the request was %s" % (_short(actor), record["state"])
            )
            return
        if record["state"] == "accepted" and record["accepted_by"] != actor:
            self._anomaly(record, "decline from %s, who does not hold this request" % _short(actor))
            return
        code = _as_str(body.get("code"), "other")
        record["decline_code"] = code if code in DECLINE_CODES else "other"
        record["decline_reason"] = _clip(body.get("reason"), 400)
        record["declined_by"] = actor
        self._terminate(record, "declined", seq, ts, now)

    def _on_progress(
        self, record: Dict[str, Any], actor: str, body: Mapping[str, Any], seq: int, ts: str,
    ) -> None:
        if record["state"] != "accepted" or record["accepted_by"] != actor:
            self._anomaly(record, "progress from %s outside an accepted request" % _short(actor))
            return
        progress = body.get("progress")
        if isinstance(progress, (int, float)) and not isinstance(progress, bool):
            record["progress"] = max(0.0, min(1.0, float(progress)))
        note = body.get("note")
        if isinstance(note, str):
            record["note"] = _clip(note, 400)
        record["progress_seq"] = seq
        record["progress_at"] = ts

    def _on_result(
        self, record: Dict[str, Any], actor: str, body: Mapping[str, Any],
        seq: int, ts: str, now: float,
    ) -> None:
        if not self._may_provide(record, actor):
            self._anomaly(record, "result from %s, who was not asked" % _short(actor))
            return
        ok = bool(body.get("ok", True))
        error = body.get("error")
        files = body.get("files")
        payload = {
            "output": body.get("output"),
            "output_text": _as_str(body.get("output_text")),
        }
        if record["state"] in TERMINAL_STATES:
            # A result after a cancel (or any other terminal) is still worth
            # keeping: the work really was done. The *state* does not move, because
            # the first terminal event is the one the log committed to.
            record["result"] = payload
            record["late_result"] = True
            self._anomaly(
                record, "result arrived after the request was %s" % record["state"]
            )
            return
        if record["state"] == "pending":
            # SPEC §15.3 says a provider accepts first. One that answers straight
            # away has still answered, and dropping the answer would be the worse
            # failure, so it is honoured and marked.
            record["implicit_accept"] = True
            record["accepted_by"] = actor
            record["accepted_ts"] = float(now)
            record["accepted_at"] = ts
            self._anomaly(record, "result from %s without an accept" % _short(actor))
        elif record["accepted_by"] != actor:
            self._anomaly(record, "result from %s, who does not hold this request" % _short(actor))
            return
        record["result"] = payload
        record["output_text"] = payload["output_text"]
        record["files"] = [f for f in files if isinstance(f, str)] if isinstance(files, (list, tuple)) else []
        record["error"] = dict(error) if isinstance(error, Mapping) else None
        record["duration_s"] = max(0.0, _as_float(body.get("duration_s"), 0.0))
        record["ok"] = ok
        self._terminate(record, "done" if ok else "failed", seq, ts, now)

    def _on_cancel(
        self, record: Dict[str, Any], actor: str, body: Mapping[str, Any],
        seq: int, ts: str, now: float,
    ) -> None:
        if actor != record["from"]:
            self._anomaly(record, "cancel from %s, who did not make this request" % _short(actor))
            return
        record["cancel_reason"] = _clip(body.get("reason"), 400)
        record["cancel_ts"] = float(now)
        if record["state"] in TERMINAL_STATES:
            # Cancel racing a result: the result got there first and wins. Recording
            # the attempt keeps the Deck honest about what the caller tried to do.
            self._anomaly(record, "cancel arrived after the request was %s" % record["state"])
            return
        self._terminate(record, "cancelled", seq, ts, now)

    def _on_taken(self, record: Dict[str, Any], body: Mapping[str, Any], seq: int) -> None:
        holder = _as_str(body.get("by"))
        if holder and not record["accepted_by"]:
            record["accepted_by"] = holder
        record["taken_seq"] = seq

    def _on_expired(self, record: Dict[str, Any], seq: int, ts: str, now: float) -> None:
        if record["state"] in TERMINAL_STATES:
            return
        record["abandoned"] = record["state"] == "accepted"
        self._terminate(record, "expired", seq, ts, now)

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def _may_provide(record: Mapping[str, Any], actor: str) -> bool:
        """Only the addressee may respond — or anyone, when ``to`` is ``"any"``."""
        if not actor:
            return False
        to = record.get("to")
        if to == "any":
            return actor != record.get("from")
        return actor == to

    def _terminate(
        self, record: Dict[str, Any], state: str, seq: int, ts: str, now: float
    ) -> None:
        record["state"] = state
        record["terminal_ts"] = float(now)
        record["terminal_at"] = ts
        record["terminal_seq"] = seq
        self._terminal_order.append(record["id"])
        self._forget_old()

    def _forget_old(self) -> None:
        while len(self._terminal_order) > MAX_RETAINED_TERMINAL:
            dropped = self._terminal_order.pop(0)
            record = self._records.get(dropped)
            if record is not None and record.get("state") in TERMINAL_STATES:
                self._records.pop(dropped, None)
                try:
                    self._order.remove(dropped)
                except ValueError:
                    pass

    @staticmethod
    def _anomaly(record: Dict[str, Any], note: str) -> None:
        notes = record.setdefault("anomalies", [])
        if len(notes) < MAX_ANOMALIES:
            notes.append(note)
        LOG.debug("exchange: request %s: %s", record.get("id"), note)

    # -- expiry ---------------------------------------------------------------
    def expire_due(self, now: float) -> List[Dict[str, Any]]:
        """Partial ``request.expired`` events for everything past its ``timeout_s``.

        Returns ``{"type": ..., "body": {...}}`` for the Hub to sign and append; the
        records are **not** moved here, because the log is the authority and they
        move when that event comes back round through :meth:`apply`. That keeps one
        code path for the transition whether the Hub or a replay produced it.

        ``body.abandoned`` is the field that matters: an expired request the
        provider had *accepted* is the one unforgivable Exchange behaviour (SPEC
        §15.3) and the Ledger charges for it.
        """
        out: List[Dict[str, Any]] = []
        for req_id in list(self._order):
            record = self._records.get(req_id)
            if record is None or record["state"] not in ("pending", "accepted"):
                continue
            deadline = record["created_ts"] + float(record["timeout_s"])
            if now < deadline:
                continue
            accepted = record["state"] == "accepted"
            out.append({
                "type": "request.expired",
                "body": {
                    "id": req_id,
                    "from": record["from"],
                    "to": record["to"],
                    "provider": record["accepted_by"] or record["to"],
                    "was": record["state"],
                    "abandoned": accepted,
                    "timeout_s": int(record["timeout_s"]),
                    "reason": (
                        "accepted but never answered"
                        if accepted else "nobody answered within timeout_s"
                    ),
                },
            })
        return out

    def drain_taken(self) -> List[Dict[str, Any]]:
        """Partial ``request.taken`` events owed to agents whose accept lost a race.

        ``to: "any"`` means the first accept wins; the others are entitled to be told
        so, instead of sitting on work that was never theirs.
        """
        out, self._taken = self._taken, []
        return out

    # -- reads ----------------------------------------------------------------
    def state_of(self, req_id: str) -> str:
        record = self._records.get(req_id)
        return record["state"] if record else "unknown"

    def get(self, req_id: str) -> Optional[Dict[str, Any]]:
        record = self._records.get(req_id)
        return dict(record) if record is not None else None

    def in_flight_for(self, agent_id: str) -> int:
        """How much work ``agent_id`` has *taken on* and not finished.

        Accepted only. A pending request is not yet work — the agent has not agreed
        to it — and counting it would let a stranger fill another agent's quota just
        by asking.
        """
        count = 0
        for record in self._records.values():
            if record["state"] != "accepted":
                continue
            if (record["accepted_by"] or record["to"]) == agent_id:
                count += 1
        return count

    def addressed_to(
        self, agent_id: str, *, states: Sequence[str] = ("pending", "accepted")
    ) -> List[Dict[str, Any]]:
        """Every request this agent could act on, oldest first."""
        wanted = tuple(states)
        out: List[Dict[str, Any]] = []
        for req_id in self._order:
            record = self._records.get(req_id)
            if record is None or record["state"] not in wanted:
                continue
            to = record["to"]
            if to == agent_id:
                out.append(dict(record))
            elif to == "any" and record["from"] != agent_id:
                if record["state"] == "pending" or record["accepted_by"] == agent_id:
                    out.append(dict(record))
        return out

    def recent_from(
        self, requester: str, provider: str, *, now: float, window_s: float = 3600.0
    ) -> int:
        """How many requests ``requester`` sent ``provider`` in the last ``window_s``.

        Feeds ``max_per_requester_per_hour``. Declines and expiries count: the limit
        exists to bound how often one agent may *ask*, not how often it succeeds.
        """
        cutoff = now - window_s
        count = 0
        for record in self._records.values():
            if record["from"] != requester:
                continue
            if record["to"] != provider and record["to"] != "any":
                continue
            if record["created_ts"] >= cutoff:
                count += 1
        return count

    def counts(self) -> Dict[str, int]:
        out = {state: 0 for state in REQUEST_STATES}
        for record in self._records.values():
            state = record["state"]
            if state in out:
                out[state] += 1
        return out

    def to_dict(self) -> Dict[str, Any]:
        """The ``requests`` block of the §5 state snapshot (INTERNAL-API)."""
        in_flight: List[Dict[str, Any]] = []
        for req_id in self._order:
            record = self._records.get(req_id)
            if record is not None and record["state"] in ("pending", "accepted"):
                in_flight.append(dict(record))
        recent: List[Dict[str, Any]] = []
        for req_id in reversed(self._terminal_order[-RECENT_IN_SNAPSHOT:]):
            record = self._records.get(req_id)
            if record is not None:
                recent.append(dict(record))
        return {"in_flight": in_flight, "recent": recent, "counts": self.counts()}


def _clamp_priority(value: Any) -> int:
    priority = _as_int(value, DEFAULT_PRIORITY)
    return max(1, min(5, priority))


def _short(agent_id: str) -> str:
    """Display abbreviation. Never key off this — two agents could collide."""
    if not isinstance(agent_id, str):
        return "?"
    body = agent_id.split("_", 1)[-1]
    return body[:6] if body else "?"


# --------------------------------------------------------------------------- builder


def make_request(
    from_agent: str,
    to: str,
    *,
    capability: str = "",
    instruction: str = "",
    input: Optional[dict] = None,  # noqa: A002 - the wire field is called `input`
    reason: str,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    priority: int = DEFAULT_PRIORITY,
    expects: str = "text",
    refs: Optional[Sequence[Mapping[str, Any]]] = None,
    req_id: str = "",
) -> Dict[str, Any]:
    """Build a validated ``request.create`` body (SPEC §15.3).

    Raises :class:`BadRequestSpec` when neither or both of ``capability`` and
    ``instruction`` are given, or when ``reason`` is empty. ``reason`` is mandatory
    at construction time rather than at the receiver because an agent that cannot
    say why it is asking has not finished thinking about what it wants.

    ``req_id`` lets a caller pin the id. Reusing it on a retry after an ambiguous
    failure is what makes the whole exchange idempotent.
    """
    if not isinstance(from_agent, str) or not from_agent:
        raise BadRequestSpec("from_agent is required")
    to = to if isinstance(to, str) and to else "any"
    capability = capability.strip() if isinstance(capability, str) else ""
    instruction = instruction.strip() if isinstance(instruction, str) else ""
    if bool(capability) == bool(instruction):
        raise BadRequestSpec(
            "exactly one of `capability` or `instruction` must be given "
            "(SPEC §15.3); got %s"
            % ("both" if capability else "neither")
        )
    if not isinstance(reason, str) or not reason.strip():
        raise BadRequestSpec(
            "`reason` is required: the receiving agent's consent decision depends "
            "on it, and the audit trail is worthless without it (SPEC §15.3)"
        )
    if input is not None and not isinstance(input, Mapping):
        raise BadRequestSpec("`input` must be a JSON object when given")
    if req_id and not is_request_id(req_id):
        raise BadRequestSpec("req_id must look like req_<hex>, got %r" % (_clip(req_id),))

    timeout = _as_int(timeout_s, DEFAULT_TIMEOUT_S)
    if timeout <= 0:
        timeout = DEFAULT_TIMEOUT_S
    timeout = min(timeout, MAX_TIMEOUT_S)

    body: Dict[str, Any] = {
        "id": req_id or new_request_id(),
        "to": to,
        "reason": reason.strip(),
        "timeout_s": timeout,
        "priority": _clamp_priority(priority),
    }
    if capability:
        body["capability"] = capability
        body["input"] = dict(input or {})
    else:
        body["instruction"] = instruction
        if expects:
            body["expects"] = expects
    if refs:
        body["refs"] = [dict(r) for r in refs if isinstance(r, Mapping)]
    return body
