"""The materialised view the Deck and ``GET /v1/state`` are built from.

Why a materialised view at all: the Deck repaints from ``/v1/state`` on every
reconnect, and a parley can accumulate tens of thousands of events.  Replaying
the log per request would make the Deck quadratic in session length.  Instead
every appended event is folded into this structure once, in ``seq`` order, by the
single thread that holds the Hub's append lock.

Everything mutable here is guarded by ``self._lock``.  The one expensive thing --
the Ledger -- is computed *outside* that lock under ``self._ledger_lock``, because
``parley.ledger.compute`` is pure and can safely run against a snapshot of the
inputs while other threads keep appending.

Lock order inside this module (see ``server.py`` for the Hub-wide order):

    StateView._ledger_lock  ->  StateView._lock  ->  Store._lock

``_lock`` is never held while calling into the Store for the ledger read, and
never held while calling ``parley.ledger.compute``.

The Exchange (SPEC §15) adds no lock of its own.  The :class:`parley.exchange`
``Registry`` and ``RequestTracker`` are deliberately not thread-safe by
themselves; they live behind ``self._lock`` like everything else here, which is
what lets ``snapshot()`` take the registry and the request table at the same
instant instead of two instants a reader would have to reconcile.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, OrderedDict, deque
from typing import (
    Any, Callable, Deque, Dict, Iterable, List, Optional, Sequence, Set, Tuple,
)

from .. import ledger as ledger_mod
from ..client.exchange import build_registry
from ..exchange import Registry, RequestTracker
from ..jsonutil import now_rfc3339, parse_rfc3339
from ..version import WIRE_VERSION
from .store import Store

log = logging.getLogger("parley.hub.state")

#: Chat kept hot for the Deck's first paint.  Older chat comes from /v1/events.
CHAT_WINDOW = 200
#: Recent file writes shown in the Workspace panel.
FILES_RECENT = 60
CONFLICT_WINDOW = 200
NOTICE_WINDOW = 100
#: Paths kept in the heat map.  Trimmed to this on every overflow.
HEAT_PATHS = 300
#: Upper bound on the event-id -> author map used for reply/citation edges.
AUTHOR_MAP_MAX = 200_000
#: Upper bound on the (a, b, path) triples remembered for co-edit de-duplication.
COEDIT_MAX = 100_000
#: Hard ceiling on how much log the Ledger reads in one recompute.
LEDGER_MAX_EVENTS = 200_000
#: The Ledger is recomputed at most this often, however many events arrive.
LEDGER_MIN_INTERVAL_S = 2.0

#: The event types ``parley.client.exchange.build_registry`` folds.  ``rebuild``
#: collects exactly these while it pages through the log so it can hand the whole
#: (small) set to ``build_registry`` without holding the rest of the log resident
#: -- and so ``agent.heartbeat``, which shares the ``agent.`` prefix and arrives
#: every 15 s per agent, is never collected.
REGISTRY_EVENT_TYPES = (
    "agent.hello", "agent.offline", "agent.bye", "agent.revoked",
    "capability.announce", "capability.revoke",
)

#: Request fields a viewer token does not get (SPEC §3.7).  A viewer reads the
#: roster, the PSR, tasks, the Ledger and the file *index* -- metadata -- and gets
#: no file *content*; the payload of a delegated request is content by the same
#: measure.  Everything else about a request (who asked whom, for which
#: capability, why, how long ago, what state it is in) stays visible, because
#: that is precisely the Deck's §8.1 "Requests in flight" panel.
VIEWER_REDACTED_REQUEST_FIELDS = (
    "input", "instruction", "result", "output_text", "files",
)


def color_hue(agent_id: str) -> int:
    """SPEC §8.3 -- deterministic per-agent hue so every Deck agrees."""
    try:
        return int(agent_id[-4:], 16) % 360
    except (ValueError, IndexError):
        return sum(ord(c) for c in agent_id) % 360


def _as_float_ts(value: Any, fallback: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return parse_rfc3339(value)
        except (ValueError, TypeError):
            return fallback
    return fallback


def _redact_request(record: dict) -> dict:
    """One request record as a viewer token may see it (SPEC §3.7, §15.3).

    Metadata stays, payload goes.  A viewer is the Deck: it needs to show that
    Ada asked Bob for ``kvm.relay`` four minutes ago, why she says she needs it,
    and whether he has accepted -- and it has no more business reading the
    arguments she passed or the answer he gave than it has reading the bytes of a
    synced file, which §3.7 already withholds.  ``error`` keeps its ``code`` so a
    failure is still legible as a failure; its message and hint do not survive,
    because that is where a provider's internals end up.
    """
    out = {k: v for k, v in record.items() if k not in VIEWER_REDACTED_REQUEST_FIELDS}
    out["input"] = {}
    out["instruction"] = None
    out["result"] = None
    out["output_text"] = ""
    out["files"] = []
    error = record.get("error")
    if isinstance(error, dict):
        out["error"] = {"code": str(error.get("code") or "")}
    # Explicit rather than implied: the Deck must be able to tell "no output"
    # from "output it is not being shown", and a human must be able to tell why
    # a field is empty.
    out["redacted"] = True
    return out


class StateView:
    """Materialised view, updated incrementally by every appended event. Thread-safe."""

    def __init__(
        self,
        store: Store,
        policy: dict,
        *,
        identity: "Optional[dict]" = None,
        weights: "Optional[dict]" = None,
    ) -> None:
        self.store = store
        self.policy = dict(policy or {})
        #: {"session","name","fingerprint","hub_started"} -- filled in by the Hub.
        self.identity: Dict[str, Any] = dict(identity or {})
        self.weights = dict(weights or ledger_mod.DEFAULT_WEIGHTS)

        self._lock = threading.RLock()
        self._ledger_lock = threading.Lock()

        self._head_seq = 0
        self._psr: Dict[str, dict] = {}
        self._last_seen: Dict[str, float] = {}
        self._online: Dict[str, bool] = {}
        self._joined: Dict[str, str] = {}
        self._hello: Dict[str, dict] = {}

        self._chat: Deque[dict] = deque(maxlen=CHAT_WINDOW)
        self._tasks: "OrderedDict[str, dict]" = OrderedDict()
        self._locks: Dict[str, dict] = {}
        self._files_recent: Deque[dict] = deque(maxlen=FILES_RECENT)
        self._conflicts: Deque[dict] = deque(maxlen=CONFLICT_WINDOW)
        self._heat: Dict[str, float] = {}
        self._notices: Deque[dict] = deque(maxlen=NOTICE_WINDOW)
        self._decisions: "OrderedDict[str, dict]" = OrderedDict()

        #: SPEC §15.  The merged capability catalogue and the request state
        #: machine, both folded from the log and both guarded by ``self._lock``.
        self._registry = Registry()
        self._requests = RequestTracker()
        #: True only while :meth:`rebuild` is paging the log: the registry is
        #: rebuilt in one go from ``build_registry`` at the end of that pass, so
        #: the per-event path must not also fold it.
        self._replaying = False

        self._edges: Dict[Tuple[str, str], Dict[str, int]] = {}
        self._event_author: "OrderedDict[str, str]" = OrderedDict()
        self._file_authors: Dict[str, Set[str]] = {}
        self._coedit_seen: "OrderedDict[Tuple[str, str, str], bool]" = OrderedDict()
        self._prev_blocked: Dict[str, str] = {}

        self._ledger_cache: "Optional[dict]" = None
        self._ledger_dirty = True
        self._ledger_at = 0.0

    # ------------------------------------------------------------- properties

    @property
    def heartbeat_s(self) -> float:
        return float(self.policy.get("heartbeat_s", 15) or 15)

    @property
    def psr_max_age_s(self) -> float:
        return float(self.policy.get("psr_max_age_s", 30) or 30)

    def head_seq(self) -> int:
        with self._lock:
            return self._head_seq

    # ------------------------------------------------------------------ apply

    def apply(self, event: dict) -> None:
        """Fold one appended event into the view.

        Called by the Hub with the append lock held, so events arrive strictly in
        ``seq`` order and this method never has to reorder anything.
        """
        try:
            self._apply_locked_entry(event)
        except Exception:  # a malformed body must never stop the log
            log.exception("state apply failed for seq=%s type=%s",
                          event.get("seq"), event.get("type"))

    def _apply_locked_entry(self, event: dict) -> None:
        etype = str(event.get("type", ""))
        actor = str(event.get("actor", ""))
        body = event.get("body") or {}
        if not isinstance(body, dict):
            body = {}
        seq = int(event.get("seq") or 0)
        when = _as_float_ts(event.get("ts"), time.time())

        with self._lock:
            if seq > self._head_seq:
                self._head_seq = seq
            self._ledger_dirty = True

            eid = event.get("id")
            if isinstance(eid, str) and eid:
                self._remember_author(eid, actor)

            if actor and actor != "hub":
                self._last_seen[actor] = max(self._last_seen.get(actor, 0.0), when)
                if etype not in ("agent.bye",):
                    self._online[actor] = True
                self._joined.setdefault(actor, str(event.get("ts") or now_rfc3339()))

            # SPEC §15.1: an agent going offline implicitly revokes everything it
            # announced.  Done before the branch below so the drop happens exactly
            # where `build_registry` does it, for the same three types.
            if etype in ("agent.offline", "agent.bye", "agent.revoked") and not self._replaying:
                self._registry.drop_agent(str(body.get("agent_id") or actor))

            if etype == "agent.hello":
                self._hello[actor] = dict(body)
                self._online[actor] = True
            elif etype == "agent.heartbeat":
                pass  # last_seen already bumped above
            elif etype == "agent.bye":
                self._online[actor] = False
                self._release_locks_of(actor)
            elif etype == "agent.offline":
                gone = str(body.get("agent_id") or actor)
                self._online[gone] = False
                self._release_locks_of(gone)
            elif etype == "agent.revoked":
                gone = str(body.get("agent_id") or "")
                if gone:
                    self._online[gone] = False
                    self._release_locks_of(gone)
            elif etype == "status.update":
                self._apply_psr(actor, body, event)
            elif etype == "chat.message":
                self._apply_chat(actor, body, event)
            elif etype == "chat.reaction":
                target = body.get("target")
                if isinstance(target, str):
                    self._edge(actor, self._event_author.get(target, ""), "reply")
            elif etype in ("knowledge.contribution",):
                self._apply_refs(actor, body.get("refs"))
            elif etype == "file.put":
                self._apply_file_put(actor, body, event, seq)
            elif etype == "file.delete":
                path = str(body.get("path") or "")
                if path and not body.get("_rejected"):
                    self._heat[path] = self._heat.get(path, 0.0) + 0.5
            elif etype == "file.move":
                self._apply_file_move(actor, body, event, seq)
            elif etype == "file.conflict":
                self._conflicts.append(
                    {
                        "path": body.get("path", ""),
                        "kept_as": body.get("kept_as", ""),
                        "ours": body.get("ours", {}),
                        "theirs": body.get("theirs", {}),
                        "seq": seq,
                        "ts": event.get("ts", ""),
                    }
                )
            elif etype == "lock.acquire":
                self._apply_lock_acquire(actor, body, when)
            elif etype == "lock.release":
                for p in body.get("paths") or []:
                    held = self._locks.get(str(p))
                    if held and held.get("agent_id") == actor:
                        self._locks.pop(str(p), None)
            elif etype == "capability.announce":
                if not self._replaying:
                    caps = body.get("capabilities")
                    self._registry.announce(
                        actor,
                        str(self._hello.get(actor, {}).get("name") or ""),
                        caps if isinstance(caps, list) else [],
                    )
            elif etype == "capability.revoke":
                if not self._replaying:
                    names = body.get("names")
                    self._registry.revoke(actor, names if isinstance(names, list) else [])
            elif etype.startswith("request."):
                self._apply_request(actor, etype, body, event, seq, when)
            elif etype.startswith("task."):
                self._apply_task(actor, etype, body, event)
            elif etype.startswith("decision."):
                self._apply_decision(actor, etype, body, event)
            elif etype == "hub.notice":
                self._notices.append(
                    {
                        "ts": event.get("ts", ""),
                        "text": str(body.get("text", "")),
                        "level": str(body.get("level", "info")),
                    }
                )
            elif etype == "hub.policy":
                p = body.get("policy")
                if isinstance(p, dict):
                    self.policy.update(p)

            self._trim_heat()

    # ------------------------------------------------------- apply: internals

    def _remember_author(self, event_id: str, actor: str) -> None:
        self._event_author[event_id] = actor
        if len(self._event_author) > AUTHOR_MAP_MAX:
            self._event_author.popitem(last=False)

    def _edge(self, src: str, dst: str, kind: str, n: int = 1) -> None:
        if not src or not dst or src == dst or src == "hub" or dst == "hub":
            return
        slot = self._edges.setdefault((src, dst), {})
        slot[kind] = slot.get(kind, 0) + n

    def _apply_refs(self, actor: str, refs: Any) -> None:
        if not isinstance(refs, list):
            return
        for ref in refs[:64]:
            if not isinstance(ref, dict):
                continue
            if ref.get("kind") != "event":
                continue
            target = ref.get("value")
            if isinstance(target, str):
                self._edge(actor, self._event_author.get(target, ""), "citation")

    def _apply_chat(self, actor: str, body: dict, event: dict) -> None:
        self._chat.append(event)
        reply_to = body.get("reply_to") or body.get("thread")
        if isinstance(reply_to, str):
            self._edge(actor, self._event_author.get(reply_to, ""), "reply")
        self._apply_refs(actor, body.get("refs"))
        to = body.get("to")
        if isinstance(to, list):
            for who in to[:16]:
                if isinstance(who, str) and who != "all":
                    self._edge(actor, who, "reply")

    def _apply_psr(self, actor: str, body: dict, event: dict) -> None:
        self._psr[actor] = {
            "body": dict(body),
            "ts": str(event.get("ts") or now_rfc3339()),
            "at": _as_float_ts(event.get("ts"), time.time()),
        }
        state = body.get("state")
        if state == "offline":
            self._online[actor] = False
        blocked = body.get("blocked_on")
        target = ""
        if isinstance(blocked, dict):
            target = str(blocked.get("agent") or "")
        # Only count a *change* of blocker: a PSR is re-emitted every 30 s and
        # counting each one would make a long block dominate the graph.
        if self._prev_blocked.get(actor, "") != target:
            self._prev_blocked[actor] = target
            if target:
                self._edge(actor, target, "blocked_on")
        focus = body.get("focus")
        if isinstance(focus, list):
            for p in focus[:8]:
                if isinstance(p, str) and p:
                    self._heat[p] = self._heat.get(p, 0.0) + 0.5

    def _apply_file_put(self, actor: str, body: dict, event: dict, seq: int) -> None:
        path = str(body.get("path") or "")
        if not path:
            return
        rec = {
            "path": path,
            "hash": body.get("hash", ""),
            "size": int(body.get("size", 0) or 0),
            "author": actor,
            "seq": seq,
            "ts": event.get("ts", ""),
        }
        self._files_recent.append(rec)
        self._heat[path] = self._heat.get(path, 0.0) + 1.0
        authors = self._file_authors.setdefault(path, set())
        for other in authors:
            if other == actor:
                continue
            key = (actor, other, path)
            if key in self._coedit_seen:
                continue
            self._coedit_seen[key] = True
            if len(self._coedit_seen) > COEDIT_MAX:
                self._coedit_seen.popitem(last=False)
            self._edge(actor, other, "co_edit")
        authors.add(actor)

    def _apply_file_move(self, actor: str, body: dict, event: dict, seq: int) -> None:
        src = str(body.get("from") or "")
        dst = str(body.get("to") or "")
        if dst:
            self._apply_file_put(
                actor,
                {"path": dst, "hash": body.get("hash", ""), "size": body.get("size", 0)},
                event,
                seq,
            )
        if src:
            self._heat.pop(src, None)
            self._file_authors.pop(src, None)

    def _apply_lock_acquire(self, actor: str, body: dict, when: float) -> None:
        ttl = float(body.get("ttl_s", 600) or 600)
        ttl = max(1.0, min(ttl, 86400.0))
        granted = body.get("_granted")
        paths = granted if isinstance(granted, list) else body.get("paths")
        if not isinstance(paths, list):
            return
        for p in paths[:256]:
            if not isinstance(p, str) or not p:
                continue
            self._locks[p] = {
                "path": p,
                "agent_id": actor,
                "expires": when + ttl,
                "intent": str(body.get("intent", "")),
            }

    def _release_locks_of(self, agent_id: str) -> None:
        for p in [p for p, l in self._locks.items() if l.get("agent_id") == agent_id]:
            self._locks.pop(p, None)

    def _apply_request(
        self, actor: str, etype: str, body: dict, event: dict, seq: int, when: float
    ) -> None:
        """Fold one ``request.*`` event (SPEC §15.3) and draw its delegation edge.

        The tracker is the state machine; everything the Hub decides about a
        request -- who owes whom an answer, what has expired -- comes from it and
        not from a second copy of the rules living here.
        """
        self._requests.apply(event, now=when)
        if etype == "request.create":
            to = str(body.get("to") or "")
            # A `to: "any"` offer has no counterpart yet; its edge is drawn when
            # somebody accepts, so the graph shows delegation that happened
            # rather than delegation that was merely offered.
            if to and to != "any":
                self._edge(actor, to, "delegation")
        elif etype == "request.accept":
            req_id = body.get("id")
            record = self._requests.get(req_id) if isinstance(req_id, str) else None
            if (
                record is not None
                and record.get("to") == "any"
                and record.get("accepted_by") == actor
                and record.get("accept_seq") == seq
            ):
                self._edge(str(record.get("from") or ""), actor, "delegation")

    def _apply_task(self, actor: str, etype: str, body: dict, event: dict) -> None:
        tid = str(body.get("id") or "")
        if not tid:
            return
        task = self._tasks.get(tid)
        if task is None:
            task = {
                "id": tid,
                "title": "",
                "detail": "",
                "status": "todo",
                "progress": 0.0,
                "claimed_by": None,
                "tags": [],
                "priority": 3,
                "depends_on": [],
                "created_by": actor,
                "updated": event.get("ts", ""),
            }
            self._tasks[tid] = task
        task["updated"] = event.get("ts", "")
        if etype == "task.create":
            task["title"] = str(body.get("title", "") or task["title"])
            task["detail"] = str(body.get("detail", "") or task["detail"])
            if isinstance(body.get("tags"), list):
                task["tags"] = [str(t) for t in body["tags"][:16]]
            if isinstance(body.get("depends_on"), list):
                task["depends_on"] = [str(t) for t in body["depends_on"][:32]]
            try:
                task["priority"] = int(body.get("priority", 3))
            except (TypeError, ValueError):
                pass
        elif etype == "task.claim":
            task["claimed_by"] = actor
            if task["status"] == "todo":
                task["status"] = "doing"
        elif etype == "task.release":
            if task.get("claimed_by") == actor:
                task["claimed_by"] = None
            if task["status"] == "doing":
                task["status"] = "todo"
        elif etype == "task.update":
            status = body.get("status")
            if status in ("todo", "doing", "blocked", "review", "done"):
                task["status"] = status
            if isinstance(body.get("progress"), (int, float)):
                task["progress"] = max(0.0, min(1.0, float(body["progress"])))
        elif etype == "task.done":
            task["status"] = "done"
            task["progress"] = 1.0
            if not task.get("claimed_by"):
                task["claimed_by"] = actor

    def _apply_decision(self, actor: str, etype: str, body: dict, event: dict) -> None:
        did = str(body.get("id") or "")
        if not did:
            return
        dec = self._decisions.get(did)
        if dec is None:
            dec = {
                "id": did,
                "question": "",
                "options": [],
                "votes": {},
                "rationales": {},
                "resolved": False,
                "option": None,
                "tally": {},
                "quorum": "majority",
                "deadline": 0.0,
                "proposed_by": actor,
            }
            self._decisions[did] = dec
        if etype == "decision.propose":
            dec["question"] = str(body.get("question", ""))
            opts = body.get("options")
            if isinstance(opts, list):
                dec["options"] = [o for o in opts[:32] if isinstance(o, dict)]
            q = body.get("quorum")
            if q in ("any", "majority", "all"):
                dec["quorum"] = q
            try:
                dl = float(body.get("deadline_s") or 0)
            except (TypeError, ValueError):
                dl = 0.0
            dec["deadline"] = (_as_float_ts(event.get("ts"), time.time()) + dl) if dl > 0 else 0.0
        elif etype == "decision.vote":
            opt = body.get("option")
            if isinstance(opt, str) and opt and not dec["resolved"]:
                dec["votes"][actor] = opt
                if body.get("rationale"):
                    dec["rationales"][actor] = str(body["rationale"])[:2000]
        elif etype == "decision.resolve":
            dec["resolved"] = True
            dec["option"] = body.get("option")
            tally = body.get("tally")
            dec["tally"] = dict(tally) if isinstance(tally, dict) else {}

    def _trim_heat(self) -> None:
        if len(self._heat) <= HEAT_PATHS * 2:
            return
        keep = sorted(self._heat.items(), key=lambda kv: kv[1], reverse=True)[:HEAT_PATHS]
        self._heat = dict(keep)

    # ---------------------------------------------------------------- queries

    def touch(self, agent_id: str, when: float) -> None:
        """Record liveness from a request that did not append an event."""
        if not agent_id or agent_id == "hub":
            return
        with self._lock:
            if when > self._last_seen.get(agent_id, 0.0):
                self._last_seen[agent_id] = when
            self._online[agent_id] = True

    def is_online(self, agent_id: str) -> bool:
        with self._lock:
            return bool(self._online.get(agent_id))

    def active_agent_count(self) -> int:
        with self._lock:
            return sum(1 for v in self._online.values() if v)

    def due_offline(self, now: float) -> List[str]:
        """Agents that are marked online but have missed 3 heartbeats (SPEC §4.1)."""
        deadline = 3.0 * self.heartbeat_s
        with self._lock:
            return [
                a
                for a, on in self._online.items()
                if on and a != "hub" and (now - self._last_seen.get(a, 0.0)) > deadline
            ]

    def locks_held_by_others(self, paths: List[str], actor: str, now: float) -> Dict[str, str]:
        """{path: holder} for paths currently locked by somebody else."""
        out: Dict[str, str] = {}
        with self._lock:
            for p in paths:
                held = self._locks.get(p)
                if not held:
                    continue
                if held.get("expires", 0.0) <= now:
                    continue
                if held.get("agent_id") and held["agent_id"] != actor:
                    out[p] = held["agent_id"]
        return out

    def due_expiries(self, now: float) -> List[dict]:
        """Partial ``request.expired`` events the Hub owes (SPEC §15.3).

        The records are *not* moved here: the Hub signs and appends each one, and
        the transition happens when that event comes back through :meth:`apply`.
        One code path for the transition, whether it came from this tick or from
        a replay of the log.
        """
        with self._lock:
            return self._requests.expire_due(now)

    def due_taken(self) -> List[dict]:
        """Partial ``request.taken`` events owed to the losers of a ``to: "any"`` race."""
        with self._lock:
            return self._requests.drain_taken()

    def request_state(self, req_id: str) -> str:
        with self._lock:
            return self._requests.state_of(req_id)

    def due_decisions(self, now: float) -> List[Tuple[str, str, Dict[str, int]]]:
        """Decisions whose quorum is met or whose deadline has passed."""
        active = max(1, self.active_agent_count())
        out: List[Tuple[str, str, Dict[str, int]]] = []
        with self._lock:
            for did, dec in self._decisions.items():
                if dec.get("resolved"):
                    continue
                outcome = self._decision_outcome(dec, active, now)
                if outcome:
                    out.append((did, outcome[0], outcome[1]))
        return out

    @staticmethod
    def _decision_outcome(
        dec: dict, active: int, now: float
    ) -> "Optional[Tuple[str, Dict[str, int]]]":
        votes = dec.get("votes") or {}
        if not votes:
            # Nobody voted: a passed deadline resolves to no option at all, which
            # we express by declining to resolve.  A decision nobody answered is
            # more honestly left open than recorded as decided.
            return None
        tally = Counter(votes.values())
        top = max(tally.values())
        # Deterministic tie-break so two Hubs replaying the same log agree.
        best = sorted([o for o, c in tally.items() if c == top])[0]
        quorum = dec.get("quorum") or "majority"
        if quorum == "any":
            return best, dict(tally)
        if quorum == "all":
            if len(votes) >= active:
                return best, dict(tally)
        else:
            if top * 2 > active:
                return best, dict(tally)
        deadline = float(dec.get("deadline") or 0.0)
        if deadline and now >= deadline:
            return best, dict(tally)
        return None

    # -------------------------------------------------------------- rebuilding

    def rebuild(self) -> None:
        """Replay the entire log from the Store.

        Used at Hub start-up and after any operation that could have invalidated
        the incremental state.  Paged so a very long session does not need the
        whole log resident at once.
        """
        with self._lock:
            self._head_seq = 0
            self._psr.clear()
            self._last_seen.clear()
            self._online.clear()
            self._joined.clear()
            self._hello.clear()
            self._chat.clear()
            self._tasks.clear()
            self._locks.clear()
            self._files_recent.clear()
            self._conflicts.clear()
            self._heat.clear()
            self._notices.clear()
            self._decisions.clear()
            self._edges.clear()
            self._event_author.clear()
            self._file_authors.clear()
            self._coedit_seen.clear()
            self._prev_blocked.clear()
            self._registry = Registry()
            self._requests = RequestTracker()
            self._replaying = True
            self._ledger_cache = None
            self._ledger_dirty = True

        since = 0
        total = 0
        registry_log: List[dict] = []
        try:
            while True:
                batch = self.store.read(since=since, limit=2000)
                if not batch:
                    break
                for ev in batch:
                    if ev.get("type") in REGISTRY_EVENT_TYPES:
                        registry_log.append(ev)
                    self._apply_locked_entry(ev)
                since = int(batch[-1].get("seq") or since)
                total += len(batch)
        finally:
            # SPEC §15.2: the registry is a fold of `capability.*` over the log,
            # so a rebuild takes the one shared implementation of that fold rather
            # than a second, subtly different copy of it.  The request tracker is
            # the fresh one installed above, folded event by event on the way past.
            with self._lock:
                self._replaying = False
                self._registry = build_registry(registry_log)
        head = self.store.head_seq()
        with self._lock:
            self._head_seq = max(self._head_seq, head)
            # A rebuild happens when nobody is connected yet; nothing is "online"
            # until it says hello or heartbeats again.
            now = time.time()
            deadline = 3.0 * self.heartbeat_s
            for a in list(self._online):
                if now - self._last_seen.get(a, 0.0) > deadline:
                    self._online[a] = False
        log.info("state rebuilt from %d events (head=%d)", total, head)

    # --------------------------------------------------------------- snapshot

    def snapshot(self, *, for_viewer: bool = False) -> dict:
        """The exact shape ``GET /v1/state`` returns and the Deck consumes."""
        self._ensure_ledger()
        now = time.time()
        agents_rows = self.store.list_agents()
        file_stats = self.store.file_stats()

        with self._lock:
            agents = [self._agent_entry(rec, now, for_viewer) for rec in agents_rows]
            known = {a["agent_id"] for a in agents}
            # An agent that said hello but is not (yet) in the roster table --
            # possible after a store restore -- still belongs on the Deck.
            for aid, hello in self._hello.items():
                if aid in known:
                    continue
                agents.append(
                    self._agent_entry(
                        {
                            "agent_id": aid,
                            "name": hello.get("name", aid[:12]),
                            "kind": hello.get("kind", ""),
                            "model": hello.get("model", ""),
                            "os": hello.get("os", ""),
                            "host": hello.get("host", ""),
                            "status": "active",
                            "last_seen": self._last_seen.get(aid, 0.0),
                        },
                        now,
                        for_viewer,
                    )
                )

            locks = [
                dict(l)
                for l in sorted(self._locks.values(), key=lambda x: x["path"])
                if l.get("expires", 0.0) > now
            ]
            requests = self._requests_locked(for_viewer=for_viewer)
            snap = {
                "v": WIRE_VERSION,
                "session": self.identity.get("session", ""),
                "name": self.identity.get("name", ""),
                "fingerprint": self.identity.get("fingerprint", ""),
                "head_seq": self._head_seq,
                "server_time": now_rfc3339(),
                "hub_started": self.identity.get("hub_started", ""),
                "policy": dict(self.policy),
                "agents": agents,
                "chat": list(self._chat),
                "tasks": [self._task_entry(t) for t in self._tasks.values()],
                "locks": locks,
                "files": {
                    "count": file_stats["count"],
                    "bytes": file_stats["bytes"],
                    "recent": list(reversed(list(self._files_recent))),
                    "conflicts": list(reversed(list(self._conflicts)))[:50],
                    "heat": dict(
                        sorted(self._heat.items(), key=lambda kv: kv[1], reverse=True)[:HEAT_PATHS]
                    ),
                },
                "ledger": self._ledger_cache or {"lines": [], "weights": dict(self.weights),
                                                 "computed_at": now_rfc3339(), "event_count": 0},
                "capabilities": self._capabilities_locked(agents),
                "requests": requests,
                "graph": self._graph(agents),
                "decisions": [self._decision_entry(d) for d in self._decisions.values()],
                "notices": list(self._notices),
            }
        return snap

    def _agent_entry(self, rec: dict, now: float, for_viewer: bool) -> dict:
        aid = str(rec.get("agent_id", ""))
        hello = self._hello.get(aid, {})
        last_seen = max(float(rec.get("last_seen", 0.0) or 0.0), self._last_seen.get(aid, 0.0))
        status = str(rec.get("status", "active"))
        online = bool(self._online.get(aid)) and status == "active"
        if online and (now - last_seen) > 3.0 * self.heartbeat_s:
            online = False
        entry = {
            "agent_id": aid,
            "name": str(rec.get("name") or hello.get("name") or aid[:12]),
            "kind": str(rec.get("kind") or hello.get("kind") or ""),
            "model": str(rec.get("model") or hello.get("model") or ""),
            "os": str(rec.get("os") or hello.get("os") or ""),
            "host": str(rec.get("host") or hello.get("host") or ""),
            "color_hue": color_hue(aid),
            "status": status,
            "online": online,
            "last_seen": last_seen,
            "joined": str(rec.get("created") or self._joined.get(aid, "")),
            "psr": self._psr_entry(aid, now),
        }
        if not for_viewer:
            ws = rec.get("workspace_hint") or hello.get("workspace_hint")
            if ws:
                entry["workspace_hint"] = str(ws)
            caps = rec.get("capabilities") or hello.get("capabilities")
            if isinstance(caps, list):
                entry["capabilities"] = [str(c) for c in caps[:32]]
        return entry

    def _psr_entry(self, agent_id: str, now: float) -> "Optional[dict]":
        held = self._psr.get(agent_id)
        if not held:
            return None
        body = held["body"]
        age = max(0.0, now - held["at"])
        return {
            "state": body.get("state", "unknown"),
            "headline": body.get("headline", ""),
            "detail": body.get("detail", ""),
            "focus": list(body.get("focus") or []),
            "task": body.get("task"),
            "progress": body.get("progress"),
            "blocked_on": body.get("blocked_on"),
            "needs": list(body.get("needs") or []),
            "eta_s": body.get("eta_s"),
            "since": body.get("since") or held["ts"],
            "age_s": round(age, 3),
            # SPEC §6.1: stale past 3 x psr_max_age_s.
            "stale": age > 3.0 * self.psr_max_age_s,
        }

    @staticmethod
    def _task_entry(task: dict) -> dict:
        return {
            "id": task["id"],
            "title": task.get("title", ""),
            "detail": task.get("detail", ""),
            "status": task.get("status", "todo"),
            "progress": task.get("progress", 0.0),
            "claimed_by": task.get("claimed_by"),
            "tags": list(task.get("tags") or []),
            "priority": task.get("priority", 3),
        }

    @staticmethod
    def _decision_entry(dec: dict) -> dict:
        return {
            "id": dec["id"],
            "question": dec.get("question", ""),
            "options": list(dec.get("options") or []),
            "votes": dict(dec.get("votes") or {}),
            "resolved": bool(dec.get("resolved")),
            "option": dec.get("option"),
        }

    # -------------------------------------------------------------- exchange

    def capabilities(self) -> dict:
        """``GET /v1/capabilities`` (SPEC §15.2): the merged registry.

        Identical for an agent and for a viewer.  A capability catalogue is a
        published advertisement -- it exists to be read by somebody deciding
        whether to ask -- so there is nothing in it to hold back from the Deck.
        """
        now = time.time()
        agents_rows = self.store.list_agents()
        with self._lock:
            # Only `agent_id` and `online` are used; the viewer form is the one
            # with nothing extra in it.
            agents = [self._agent_entry(rec, now, True) for rec in agents_rows]
            return self._capabilities_locked(agents)

    def requests(
        self,
        *,
        state: "Optional[Sequence[str]]" = None,
        to: str = "",
        from_agent: str = "",
        for_viewer: bool = False,
    ) -> dict:
        """``GET /v1/requests`` (SPEC §15.3), with the three documented filters.

        ``to`` matches the addressee *and* whoever accepted a ``to: "any"``
        offer, because from the caller's point of view that agent is who the
        request went to.
        """
        with self._lock:
            return self._requests_locked(
                for_viewer=for_viewer, state=state, to=to, from_agent=from_agent
            )

    def _capabilities_locked(self, agents: List[dict]) -> dict:
        online: Dict[str, bool] = {a["agent_id"]: bool(a["online"]) for a in agents}
        for aid, flag in self._online.items():
            online.setdefault(aid, bool(flag))
        return self._registry.to_dict(
            online=online,
            in_flight=self._in_flight_by_capability(),
        )

    def _in_flight_by_capability(self) -> Dict[str, int]:
        """``{"<agent>/<capability>": n}`` for work a provider has *accepted*.

        Keyed per capability only, never per agent: ``Registry.to_dict`` falls
        back from ``"<agent>/<name>"`` to ``"<agent>"``, so a bare per-agent entry
        would make every *other* capability that agent holds report the agent's
        whole load as its own.  A pending request is not counted -- the provider
        has not agreed to it, and SPEC §15.1 ``concurrency`` bounds accepted work.
        """
        counts: Dict[str, int] = {}
        for record in self._requests.to_dict()["in_flight"]:
            if record.get("state") != "accepted":
                continue
            name = record.get("capability")
            if not name:
                continue  # a free-form instruction belongs to no capability row
            provider = str(record.get("accepted_by") or record.get("to") or "")
            if not provider or provider == "any":
                continue
            key = provider + "/" + str(name)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _requests_locked(
        self,
        *,
        for_viewer: bool,
        state: "Optional[Sequence[str]]" = None,
        to: str = "",
        from_agent: str = "",
    ) -> dict:
        data = self._requests.to_dict()
        wanted = tuple(state or ())

        def keep(record: dict) -> bool:
            if wanted and record.get("state") not in wanted:
                return False
            if to and to not in (record.get("to"), record.get("accepted_by")):
                return False
            if from_agent and record.get("from") != from_agent:
                return False
            return True

        return {
            "in_flight": self._request_rows(data["in_flight"], keep, for_viewer),
            "recent": self._request_rows(data["recent"], keep, for_viewer),
            # The tally is always of the whole session, filtered or not: it is the
            # Deck's summary line, and a count that moved with the filter would be
            # read as "this is all there is".
            "counts": dict(data["counts"]),
        }

    @staticmethod
    def _request_rows(
        rows: Iterable[dict], keep: "Callable[[dict], bool]", for_viewer: bool
    ) -> List[dict]:
        out: List[dict] = []
        for record in rows:
            if not keep(record):
                continue
            out.append(_redact_request(record) if for_viewer else record)
        return out

    def _graph(self, agents: List[dict]) -> dict:
        nodes = [{"id": a["agent_id"], "label": a["name"]} for a in agents]
        known = {n["id"] for n in nodes}
        edges = []
        for (src, dst), kinds in self._edges.items():
            if src not in known or dst not in known:
                continue
            edges.append(
                {
                    "source": src,
                    "target": dst,
                    "weight": float(sum(kinds.values())),
                    "kinds": {
                        "reply": int(kinds.get("reply", 0)),
                        "citation": int(kinds.get("citation", 0)),
                        "co_edit": int(kinds.get("co_edit", 0)),
                        "blocked_on": int(kinds.get("blocked_on", 0)),
                        "delegation": int(kinds.get("delegation", 0)),
                    },
                }
            )
        edges.sort(key=lambda e: (-e["weight"], e["source"], e["target"]))
        return {"nodes": nodes, "edges": edges}

    # ----------------------------------------------------------------- ledger

    def _ensure_ledger(self, force: bool = False) -> None:
        """Recompute the Ledger if it is dirty, at most every few seconds.

        Must be called with ``self._lock`` NOT held: ``parley.ledger.compute`` is
        pure but reads the whole log, and holding the view lock for that long
        would stall the append path.
        """
        with self._ledger_lock:
            with self._lock:
                dirty = self._ledger_dirty
                cached = self._ledger_cache
                age = time.time() - self._ledger_at
            if cached is not None and not force and (not dirty or age < LEDGER_MIN_INTERVAL_S):
                return
            try:
                events = self.store.read(since=0, limit=LEDGER_MAX_EVENTS)
                files = self.store.list_files()
                result = ledger_mod.compute(events, files, dict(self.weights))
                computed = result.to_dict()
            except Exception:
                log.exception("ledger recompute failed; serving the previous result")
                return
            with self._lock:
                self._ledger_cache = computed
                self._ledger_at = time.time()
                self._ledger_dirty = False

    def ledger(self, *, force: bool = False) -> dict:
        self._ensure_ledger(force=force)
        with self._lock:
            return dict(self._ledger_cache or {})
