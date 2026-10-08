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
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, OrderedDict, deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from .. import ledger as ledger_mod
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
            self._ledger_cache = None
            self._ledger_dirty = True

        since = 0
        total = 0
        while True:
            batch = self.store.read(since=since, limit=2000)
            if not batch:
                break
            for ev in batch:
                self._apply_locked_entry(ev)
            since = int(batch[-1].get("seq") or since)
            total += len(batch)
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
