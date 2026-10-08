#!/usr/bin/env python3
"""A complete, runnable Parley participant.

This is a *reference*, not pseudocode. Run it and it joins a real parley, keeps a
conforming standing report, reads the chat, claims a task, takes a lock, edits a
synced file, records a knowledge contribution, lends a capability to the other
agents and asks one of them for help -- then leaves cleanly.

Read it top to bottom: it is laid out in the order AGENTS.md describes, and every
obligation from AGENTS.md sections 6 and 6A is implemented and labelled with the
O-number it satisfies.

Usage
-----
    # From anywhere, with the clone importable:
    export PYTHONPATH=/path/to/parley-clone

    # First run -- enrol:
    python3 agent.py --hub http://192.168.1.20:7777 \
        --invite "copper-otter-climbs-the-quiet-hill" \
        --name Cleo --workspace ~/work/parley-ws

    # Later runs -- reuse the stored credentials:
    python3 agent.py --workspace ~/work/parley-ws

    # Just watch, change nothing:
    python3 agent.py --workspace ~/work/parley-ws --observe

    # Take part in chat and files but lend nothing and ask nobody:
    python3 agent.py --workspace ~/work/parley-ws --no-exchange

Where the LLM goes
------------------
There is no model in here. `decide_next_action()` is the seam: it is a plain
function that looks at the session state and returns what to do next. Replace its
body with a call to your model, keep the surrounding machinery, and you have a
conforming LLM agent. Everything else in this file is the part you would otherwise
have to write yourself.

The Exchange
------------
This agent is an Exchange participant (AGENTS.md section 6A, SPEC section 15). It:

* announces one capability, `workspace.grep` -- read-only, declared `safe`, with an
  input schema (O11, O13);
* serves requests for it from a handler, under a consent policy it did not write
  (O14, O15);
* reads the registry before deciding it cannot do something (O12), and asks a peer
  for `workspace.grep` over *that* agent's copy of the workspace, which is a real
  cross-check that file sync agrees on both ends -- with a stated reason (O16).

Two instances of this file, in two workspaces on the same parley, will discover each
other and exchange. Run it with `--no-exchange` to stay a Base-profile participant:
even then it still *consumes* capability.* and request.* events and declines
anything addressed to it rather than ignoring it, which SPEC section 14 requires of
every participant.

Requires: Python 3.9+, and the `parley` package importable. No other dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

try:
    from parley.client.client import ParleyClient
    from parley.client.exchange import Provider, Requester
    from parley.client.sync import WorkspaceSync
    from parley.config import Credentials
    from parley.exchange import Capability, Decision, Policy
    from parley.ids import new_task_id
except ImportError as exc:  # pragma: no cover - a setup problem, not a runtime one
    sys.stderr.write(
        "cannot import the parley package: %s\n\n"
        "Point PYTHONPATH at your clone:\n"
        "    export PYTHONPATH=/path/to/parley-clone\n"
        "or run this from inside the clone.\n" % exc
    )
    raise SystemExit(1)


# --------------------------------------------------------------------------- #
# Configuration. Tuned to the policy defaults in SPEC 3.4; the Hub tells you its
# real values in `creds.policy` after enrolment, and you should prefer those.
# --------------------------------------------------------------------------- #

PSR_INTERVAL_S = 20.0        # < psr_max_age_s (30) with margin. Obligation O1.
HEARTBEAT_INTERVAL_S = 15.0  # policy.heartbeat_s
SYNC_INTERVAL_S = 2.0        # policy.poll_ms
THINK_INTERVAL_S = 10.0      # how often this agent reconsiders what to do
CHAT_HISTORY = 200           # how much chat we keep in memory
PUMP_INTERVAL_S = 2.0        # Exchange watchdog tick; see Provider.pump()

# Bounds for the capability we lend. A provider decides what it is willing to
# spend on a stranger's request; these are that decision, written down.
GREP_MAX_FILE_BYTES = 1 * 1024 * 1024
GREP_MAX_FILES = 2000
GREP_DEFAULT_HITS = 20
GREP_SKIP_DIRS = {".parley", ".git", "__pycache__", "node_modules", ".venv",
                  "venv", ".tox", "dist", "build", ".mypy_cache"}


# --------------------------------------------------------------------------- #
# Session state -- everything this agent knows about the parley.
#
# One lock guards the whole thing. The stream thread writes it; the main thread
# reads it. Keeping it to a single coarse lock is deliberate: a reference
# implementation should be obviously correct rather than cleverly concurrent.
# --------------------------------------------------------------------------- #

class Session:
    def __init__(self, me: str) -> None:
        self.lock = threading.Lock()
        self.me = me
        self.last_seq = 0
        self.chat: Deque[dict] = deque(maxlen=CHAT_HISTORY)
        self.agents: Dict[str, dict] = {}       # agent_id -> agent.hello body + name
        self.psr: Dict[str, dict] = {}          # agent_id -> latest status.update body
        self.locks: Dict[str, dict] = {}        # path -> {agent_id, intent, expires}
        self.tasks: Dict[str, dict] = {}        # task id -> {title, status, claimed_by}
        self.conflicts: List[dict] = []
        self.notices: List[str] = []
        # The Exchange registry, folded out of capability.* events: agent -> name
        # -> capability dict. `Requester.discover()` reads the same thing from
        # /v1/capabilities in one call; we fold it from the stream as well so this
        # file demonstrates where the data actually comes from, and so it keeps
        # working against a Hub that only publishes the snapshot.
        self.capabilities: Dict[str, Dict[str, dict]] = {}

    # -- writes, from the stream thread ------------------------------------- #

    def apply(self, event: dict) -> None:
        etype = event.get("type", "")
        body = event.get("body") or {}
        actor = event.get("actor", "")

        with self.lock:
            seq = event.get("seq")
            if isinstance(seq, int) and seq > self.last_seq:
                self.last_seq = seq

            if etype == "chat.message":
                self.chat.append(event)

            elif etype == "status.update":
                self.psr[actor] = body

            elif etype == "agent.hello":
                self.agents[actor] = dict(body)

            elif etype == "agent.offline":
                gone = body.get("agent_id")
                if gone:
                    self.agents.pop(gone, None)
                    self.psr.pop(gone, None)
                    # An agent going offline implicitly revokes everything it
                    # announced (SPEC 15.1). Keeping a dead agent's capabilities
                    # in the registry is how you end up asking a machine that is
                    # not there.
                    self.capabilities.pop(gone, None)
                    # Their locks are stale now; drop them so we do not avoid
                    # files nobody is holding.
                    for path in [p for p, h in self.locks.items() if h.get("agent_id") == gone]:
                        self.locks.pop(path, None)

            elif etype == "capability.announce":
                # SPEC 15.1: announce is TOTAL, not incremental. Replace the whole
                # catalogue for this agent; never merge. That is what makes
                # re-announcing on reconnect correct, and what makes a capability
                # that quietly went away stop being offered.
                table: Dict[str, dict] = {}
                for cap in body.get("capabilities") or []:
                    if isinstance(cap, dict) and isinstance(cap.get("name"), str):
                        table[cap["name"]] = cap
                self.capabilities[actor] = table

            elif etype == "capability.revoke":
                table = self.capabilities.get(actor) or {}
                for name in body.get("names") or []:
                    table.pop(name, None)

            elif etype == "lock.acquire":
                for path in body.get("paths") or []:
                    self.locks[path] = {
                        "agent_id": actor,
                        "intent": body.get("intent", ""),
                        "expires": time.time() + float(body.get("ttl_s") or 600),
                    }

            elif etype == "lock.release":
                for path in body.get("paths") or []:
                    held = self.locks.get(path)
                    if held and held.get("agent_id") == actor:
                        self.locks.pop(path, None)

            elif etype == "task.create":
                tid = body.get("id")
                if tid:
                    self.tasks[tid] = {
                        "id": tid,
                        "title": body.get("title", ""),
                        "detail": body.get("detail", ""),
                        "status": "todo",
                        "claimed_by": None,
                    }

            elif etype == "task.claim":
                task = self.tasks.get(body.get("id") or "")
                if task:
                    task["claimed_by"] = actor
                    task["status"] = "doing"

            elif etype == "task.release":
                task = self.tasks.get(body.get("id") or "")
                if task:
                    task["claimed_by"] = None
                    task["status"] = "todo"

            elif etype == "task.update":
                task = self.tasks.get(body.get("id") or "")
                if task and body.get("status"):
                    task["status"] = body["status"]

            elif etype == "task.done":
                task = self.tasks.get(body.get("id") or "")
                if task:
                    task["status"] = "done"

            elif etype == "file.conflict":
                self.conflicts.append(body)

            elif etype == "hub.notice":
                self.notices.append(str(body.get("text", "")))

            # Anything else -- including every x.* extension type -- is ignored
            # on purpose. SPEC 2.1 *requires* unknown types to be tolerated, not
            # merely allows it. Never raise here.

    # -- reads, from the main thread ---------------------------------------- #

    def snapshot(self) -> dict:
        """A consistent copy, so the decision logic never races the stream."""
        now = time.time()
        with self.lock:
            return {
                "last_seq": self.last_seq,
                "chat": list(self.chat),
                "agents": dict(self.agents),
                "psr": {k: dict(v) for k, v in self.psr.items()},
                "locks": {p: dict(h) for p, h in self.locks.items()
                          if h.get("expires", 0) > now},
                "tasks": {k: dict(v) for k, v in self.tasks.items()},
                "conflicts": list(self.conflicts),
                "capabilities": {a: dict(t) for a, t in self.capabilities.items()},
            }

    def who_offers(self, name: str, *, exclude_self: bool = True) -> List[str]:
        """O12: which agents have announced `name`. Empty means nobody can."""
        with self.lock:
            return sorted(
                agent_id for agent_id, table in self.capabilities.items()
                if name in table and not (exclude_self and agent_id == self.me)
            )

    def path_held_by_other(self, path: str) -> Optional[dict]:
        """Obligation O8: who, if anyone, holds this path -- other than me."""
        now = time.time()
        with self.lock:
            held = self.locks.get(path)
            if not held:
                return None
            if held.get("expires", 0) <= now:
                return None
            if held.get("agent_id") == self.me:
                return None
            return dict(held)


# --------------------------------------------------------------------------- #
# The agent
# --------------------------------------------------------------------------- #

class Agent:
    def __init__(self, client: ParleyClient, workspace: Path, *,
                 sync: Optional[WorkspaceSync] = None, observe: bool = False,
                 exchange: bool = True) -> None:
        self.client = client
        self.workspace = workspace
        self.sync = sync
        self.observe = observe
        self.session = Session(client.agent_id)
        self.stop = threading.Event()
        self.threads: List[threading.Thread] = []

        # -- the Exchange (AGENTS.md section 6A) ---------------------------- #
        # `Provider` serves requests addressed to us; `Requester` makes them. Both
        # are fed from the one event stream in `_stream_loop`. The consent policy
        # is loaded from <workspace>/.parley/policy.json and is NOT ours to write:
        # it belongs to whoever runs this agent. Absent, the defaults deny-by-
        # default for anything above `safe` (SPEC 15.4 rule 3).
        self.provider: Optional[Provider] = None
        self.requester: Optional[Requester] = None
        if exchange and not observe:
            self.provider = Provider(client, workspace, policy=Policy.load(workspace))
            self.provider.on_ask = self._on_consent_needed
            self.requester = Requester(client)
        self._asked_peer = False
        self._did_demo = False

        # Our own standing report. `_set_psr` is the only thing that mutates it,
        # so the re-emission timer always has something coherent to resend.
        self._psr: Dict[str, Any] = {
            "state": "planning",
            "headline": "Reading the roster and the recent chat",
        }
        self._psr_lock = threading.Lock()
        self._held_locks: List[str] = []
        self._claimed_task: Optional[str] = None

    # -- obligation O1: the standing report ---------------------------------- #

    def _set_psr(self, headline: str, *, state: str = "working", **extra: Any) -> None:
        """Change the standing report AND publish it immediately.

        SPEC 6.1: emit on every state change *and* at least every psr_max_age_s.
        The timer in `_psr_loop` covers the second half; this covers the first.
        """
        body: Dict[str, Any] = {"state": state, "headline": headline}
        for key, value in extra.items():
            if value is not None:
                body[key] = value

        with self._psr_lock:
            self._psr = body

        # Also mirror it to .parley/me.json. If this agent is ever run alongside
        # `parley run --psr-from .parley/me.json`, the daemon will keep it fresh
        # even while we are busy thinking. Belt and braces -- see
        # docs/STANDING-REPORT.md section 6.2 for why the split matters.
        self._write_me_json(body)

        if not self.observe:
            self._safe(lambda: self.client.status(
                body["headline"],
                state=body["state"],
                detail=body.get("detail", ""),
                focus=body.get("focus"),
                progress=body.get("progress"),
                task=body.get("task"),
                blocked_on=body.get("blocked_on"),
                needs=body.get("needs"),
                eta_s=body.get("eta_s"),
            ))
        log("PSR [%s] %s" % (body["state"], body["headline"]))

    def _write_me_json(self, body: dict) -> None:
        target = self.workspace / ".parley" / "me.json"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(target))
        except OSError as exc:
            log("could not write me.json: %s" % exc)

    def _psr_loop(self) -> None:
        """Re-emit the current report on a timer.

        This runs in its own thread precisely so that a long think in the main
        loop cannot make us look dead. Putting the timer inside the reasoning
        loop is the single most common way to end up rendered 'stale'.
        """
        while not self.stop.wait(PSR_INTERVAL_S):
            with self._psr_lock:
                body = dict(self._psr)
            if self.observe:
                continue
            self._safe(lambda: self.client.status(
                body["headline"],
                state=body["state"],
                detail=body.get("detail", ""),
                focus=body.get("focus"),
                progress=body.get("progress"),
                task=body.get("task"),
                blocked_on=body.get("blocked_on"),
                needs=body.get("needs"),
                eta_s=body.get("eta_s"),
            ))

    def _heartbeat_loop(self) -> None:
        """agent.heartbeat every heartbeat_s. Silence for 3x marks us offline."""
        while not self.stop.wait(HEARTBEAT_INTERVAL_S):
            if self.observe:
                continue
            self._safe(self.client.heartbeat)

    # -- reading the log ------------------------------------------------------ #

    def _stream_loop(self) -> None:
        """Consume the log and keep `self.session` current.

        `client.stream()` handles SSE, the long-poll fallback, reconnection with
        full-jitter backoff and resumption from the last seq. It does not raise
        on a transient drop -- it reconnects and keeps yielding. So this loop is
        deliberately boring.

        NOTE the ordering: apply the event to the file system *before* advancing
        our view of it. Crashing between the two costs us a replay, not an event.
        """
        for event in self.client.stream(since=0):
            if self.stop.is_set():
                break
            etype = str(event.get("type", ""))
            try:
                if self.sync is not None and etype.startswith("file."):
                    self.sync.apply_event(event)
            except Exception as exc:  # a bad event must not kill the stream
                log("sync could not apply %s: %s" % (event.get("type"), exc))
            self.session.apply(event)

            # The Exchange is fed last, and each half separately, so that a fault
            # in one cannot stop the other or stop the stream. Neither call blocks:
            # Provider.on_event only decides and queues; handlers run on their own
            # threads.
            if self.provider is not None:
                try:
                    self.provider.on_event(event)
                except Exception as exc:
                    log("provider could not handle %s: %s" % (etype, exc))
            elif etype == "request.create" and not self.observe:
                # Nothing to lend -- but SPEC 14 still requires an answer.
                self._decline_unserved(event)
            if self.requester is not None:
                try:
                    self.requester.on_event(event)
                except Exception as exc:
                    log("requester could not handle %s: %s" % (etype, exc))

    def _pump_loop(self) -> None:
        """Tick the Exchange. This is not optional (AGENTS.md O14).

        `Provider.pump()` is what guarantees an accepted request always gets a
        terminal event: it retries results that could not be posted, settles
        handlers that ran past timeout_s (abandoning the thread rather than
        waiting on it -- one wedged read must not wedge the agent), and
        auto-declines consent prompts nobody answered. Without this thread an
        agent can accept work and silently drop it, which is the one unforgivable
        behaviour in the Exchange and the only thing the Ledger subtracts for.
        """
        if self.provider is None:
            return
        while not self.stop.wait(PUMP_INTERVAL_S):
            try:
                self.provider.pump()
            except Exception as exc:
                log("exchange pump failed: %s" % exc)

    def _sync_loop(self) -> None:
        """Publish our own file changes. The other half of `_stream_loop`."""
        if self.sync is None:
            return
        while not self.stop.wait(SYNC_INTERVAL_S):
            try:
                for event in self.sync.scan_once():
                    log("published %s %s" % (event.get("type"), (event.get("body") or {}).get("path")))
            except Exception as exc:
                log("scan failed: %s" % exc)

    # -- obligations O2, O3, O7, O8: claiming work ---------------------------- #

    def announce_intent(self, headline: str, paths: List[str], why: str) -> bool:
        """O2 + O3 + O8: check, announce, lock -- in that order.

        Returns False if somebody else already holds one of the paths, in which
        case the caller must pick different work. That check is the whole point:
        a lock is only useful if agents read it before writing.
        """
        for path in paths:
            held = self.session.path_held_by_other(path)
            if held:
                # O8: do not just write anyway. Ask, and go do something else.
                self._safe(lambda: self.client.say(
                    "%s is locked by %s (intent: %s). I wanted it for: %s. "
                    "Shall I wait, or do you want to hand it over?"
                    % (path, held.get("agent_id"), held.get("intent") or "unstated", why),
                    refs=[{"kind": "file", "value": path}],
                ))
                log("yielding: %s is held by %s" % (path, held.get("agent_id")))
                return False

        # O2: say it before you start, so nobody duplicates the work.
        self._safe(lambda: self.client.say(
            "Taking %s -- %s. Shout if you are already in there."
            % (", ".join(paths), why),
            refs=[{"kind": "file", "value": p} for p in paths],
        ))

        # O3: the advisory lock. Not a mutex -- a social signal the Deck renders
        # and well-behaved agents respect.
        self._safe(lambda: self.client.emit("lock.acquire", {
            "paths": paths,
            "ttl_s": 600,
            "intent": why,
        }))
        self._held_locks.extend(p for p in paths if p not in self._held_locks)

        # O1: the report names the files, so the intent is visible even to an
        # agent that missed the chat message.
        self._set_psr(headline, state="working", focus=paths, detail=why, progress=0.1)
        return True

    def release(self, paths: List[str]) -> None:
        """O7: give back what you are holding, as soon as you stop."""
        paths = [p for p in paths if p in self._held_locks]
        if not paths:
            return
        self._safe(lambda: self.client.emit("lock.release", {"paths": paths}))
        self._held_locks = [p for p in self._held_locks if p not in paths]
        log("released %s" % ", ".join(paths))

    # -- obligation O4: recording what you learned ---------------------------- #

    def record(self, title: str, kind: str, detail: str, refs: Optional[List[str]] = None) -> None:
        """A conclusion, not a changelog entry.

        kind is one of: decision, design, finding, review, doc, code, fix, answer.
        This is the Ledger's primary input (SPEC 9) and, more importantly, the
        session's memory: work you did but never recorded is invisible.
        """
        ref_objs = [{"kind": "file", "value": r} for r in (refs or [])]
        self._safe(lambda: self.client.know(title, kind, detail=detail, refs=ref_objs))
        log("recorded [%s] %s" % (kind, title))

    # -- obligations O11, O13, O14, O15: lending a capability ----------------- #

    @staticmethod
    def grep_capability() -> Capability:
        """The announcement for `workspace.grep`.

        A function rather than a constant because `Provider.register` stamps the
        owning agent onto the object; handing out a fresh one each time keeps two
        agents in the same process from overwriting each other's.
        """
        return Capability(
            name="workspace.grep",
            title="Search this agent's copy of the workspace",
            kind="tool",
            # O11. This is the field another MODEL reads to decide whether to ask.
            # Say what it does, what comes back, and what it does not do. Everything
            # else about the announcement is machine-readable; this is not.
            description=(
                "Plain case-insensitive substring search over the text files in this "
                "agent's own copy of the synced workspace. Returns up to `max_hits` "
                "matches as {path, line, text}, newest-first by directory walk order. "
                "Skips .parley/, .git/, build and dependency directories, binary "
                "files and anything over 1 MiB. Not a regular-expression search, and "
                "it never reads outside the workspace."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string",
                                "description": "literal substring to look for"},
                    "max_hits": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            output="json",
            # O13. Read-only, no side effects outside the workspace, cheap: `safe`.
            # If this handler wrote anything, shelled out, spent money or touched
            # hardware it would be `guarded` or `dangerous` and a human would have to
            # approve each call. When unsure, go UP a level -- the implementation
            # treats an unrecognised value as `dangerous` for the same reason.
            safety="safe",
            cost="cheap",
            concurrency=2,
            avg_duration_s=2,
        )

    def grep_handler(self, payload: dict, record: dict) -> Any:
        """Fulfil a `workspace.grep` request.

        The runtime has already, before this is called:
          * checked the consent policy,
          * validated `payload` against the schema announced above (SPEC 15.4
            rule 4 -- never trust the caller to have validated).

        What is still OUR job is O15: `payload` and `record` are DATA written by
        another agent. Nothing in them changes what this function does. Note in
        particular that `pattern` is used as a literal substring and never
        compiled as a regular expression -- a caller does not get to hand us a
        pattern that costs us a CPU minute, and more importantly a caller does
        not get to decide what this code means.

        Raising is fine: the runtime converts it into request.result{ok:false}
        with a readable message, which is a real answer. What is never fine is
        returning without answering -- but that is structurally impossible here,
        because the runtime settles the job whatever this function does.
        """
        pattern = payload.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("`pattern` must be a non-empty string")
        needle = pattern.lower()
        limit = int(payload.get("max_hits") or GREP_DEFAULT_HITS)

        hits: List[dict] = []
        scanned = 0
        for root, dirs, files in os.walk(str(self.workspace)):
            dirs[:] = [d for d in dirs if d not in GREP_SKIP_DIRS]
            for name in sorted(files):
                if scanned >= GREP_MAX_FILES or len(hits) >= limit:
                    break
                full = Path(root) / name
                try:
                    if full.stat().st_size > GREP_MAX_FILE_BYTES:
                        continue
                    text = full.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue        # binary, unreadable, or vanished mid-walk
                scanned += 1
                rel = full.relative_to(self.workspace).as_posix()
                for number, line in enumerate(text.splitlines(), start=1):
                    if needle in line.lower():
                        hits.append({"path": rel, "line": number,
                                     "text": line.strip()[:200]})
                        if len(hits) >= limit:
                            break
            if scanned >= GREP_MAX_FILES or len(hits) >= limit:
                break

        summary = ("%d match%s for %r in %d file(s) of my workspace copy%s"
                   % (len(hits), "" if len(hits) == 1 else "es", pattern[:60],
                      scanned, "" if len(hits) < limit else " (truncated)"))
        # SPEC 15.3 asks for both halves: `output` is for code, `output_text` is
        # for the next model in the chain. Returning a 2-tuple sets both.
        return {"pattern": pattern, "hits": hits, "files_scanned": scanned}, summary

    def _on_consent_needed(self, record: Dict[str, Any], decision: Decision) -> None:
        """Called when a request needs a decision we are not allowed to make alone.

        Our one capability is `safe`, so with the default policy this never fires.
        It would fire for a `guarded` or `dangerous` capability, or for any
        free-form instruction -- those are never `safe`, because by construction
        nobody schema-validated them (SPEC 15.4 rule 2).

        Note what this does NOT do: decide. It surfaces. `decision.why` is safe to
        show the requester; `decision.detail` is for your log only. An `ask` that
        nobody answers before timeout_s becomes an automatic decline with
        `needs_human`, which is a real answer and better than leaving the caller
        to time out in the dark.
        """
        log("CONSENT NEEDED: %s asks for %s -- %r. %s  Answer with "
            "provider.accept(%r) or provider.decline(%r, reason, code)."
            % (record.get("from", "?"),
               record.get("capability") or "a free-form instruction",
               str(record.get("reason", ""))[:120], decision.why,
               record.get("id", ""), record.get("id", "")))

    def _decline_unserved(self, event: dict) -> None:
        """SPEC 14: a participant with nothing to lend MUST decline, not ignore.

        This is the `--no-exchange` path. Silence is indistinguishable from a
        crash, so it costs the caller its whole timeout_s and tells it nothing;
        one decline event costs us nothing and is a complete answer.
        """
        body = event.get("body") or {}
        if body.get("to") != self.session.me:
            return
        self._safe(lambda: self.client.emit("request.decline", {
            "id": body.get("id"),
            "reason": "This agent is running with the Exchange disabled and takes "
                      "no delegated work.",
            "code": "unknown_capability",
        }))
        log("declined %s (exchange disabled)" % body.get("id"))

    # -- obligations O12, O16: asking another agent --------------------------- #

    def ask_peer_for_grep(self, provider_id: str) -> None:
        """Ask a peer to grep ITS copy of the workspace, and say why (O16).

        A real cross-check: if sync is working, a search that matches here must
        match there. It is also the shape every request should have -- a named
        capability, input that matches the announced schema, a `reason` a human
        could act on, and a timeout the caller is willing to wait.
        """
        if self.requester is None:
            return
        self._asked_peer = True
        needle = self.client.agent_id[-8:]
        try:
            req_id = self.requester.ask(
                provider_id, "workspace.grep",
                {"pattern": needle, "max_hits": 5},
                # O16. `reason` is required. It is the text a human reads before
                # deciding whether this happens, and the audit trail is worthless
                # without it. "Need this" gets declined; this does not.
                reason=("Cross-checking file sync: my joining note names %s, so a "
                        "grep of your workspace copy should find it too. If it "
                        "does not, sync has diverged between us." % needle),
                timeout_s=60,
                priority=2,          # honest: this is a nice-to-have, not urgent
            )
        except Exception as exc:     # a malformed request is our bug, not theirs
            log("could not ask %s: %s" % (provider_id[-8:], exc))
            return

        # `wait()` publishes a conforming `waiting` PSR with blocked_on naming the
        # provider for the duration, and restores the previous one afterwards --
        # including if something raises. That is what makes the Deck's dependency
        # view mean anything.
        record = self.requester.wait(req_id, timeout_s=90)
        state = record.get("state")
        if state == "done":
            log("peer %s answered %s: %s"
                % (provider_id[-8:], req_id, record.get("output_text") or "(no summary)"))
        elif state == "declined":
            # A decline is a complete, correct answer and is never a fault.
            log("peer %s declined %s (%s): %s"
                % (provider_id[-8:], req_id, record.get("decline_code"),
                   record.get("decline_reason")))
        else:
            log("request %s ended as %s: %s"
                % (req_id, state, record.get("error") or "no further detail"))

    # -- the work ------------------------------------------------------------- #

    def edit_synced_file(self, rel_path: str, contents: str) -> None:
        """Write a file into the workspace.

        Note what is NOT here: no upload call, no file.put event, no blob hashing.
        You write the file with ordinary tools and the sync layer notices it,
        hashes it, uploads the blob and emits the event. Resist the urge to
        publish file contents through chat or the event body -- events are capped
        at 256 KiB and the blob store exists for exactly this.
        """
        target = self.workspace / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically even locally: another agent's scanner may be mid-poll,
        # and a half-written file that gets shipped is everybody's problem.
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(contents, encoding="utf-8")
        os.replace(str(tmp), str(target))
        log("wrote %s (%d bytes)" % (rel_path, len(contents.encode("utf-8"))))

    # -- the seam where a model would go -------------------------------------- #

    def decide_next_action(self, state: dict) -> Optional[dict]:
        """Decide what to do next. Replace this with your model.

        Takes a snapshot of the session; returns an action dict or None for
        "nothing to do right now". Kept as a pure-ish function of the state so
        that swapping in an LLM is a local change: serialise `state` into a
        prompt, parse the model's answer back into one of these action dicts.

        The scripted behaviour below exists so this file is runnable as-is and so
        each obligation has a visible demonstration.
        """
        # 1. An unclaimed task is the clearest signal of what the parley wants.
        for task in state["tasks"].values():
            if task["status"] == "todo" and not task["claimed_by"]:
                return {"kind": "claim_task", "task": task}

        # 2. A conflict involving us outranks new work: fix what is broken first.
        for conflict in state["conflicts"]:
            if conflict.get("ours", {}).get("agent") == self.session.me:
                return {"kind": "flag_conflict", "conflict": conflict}

        # 3. Someone addressed us and we have not answered.
        for event in reversed(state["chat"]):
            body = event.get("body") or {}
            if event.get("actor") == self.session.me:
                continue
            to = body.get("to") or []
            if self.session.me in to:
                return {"kind": "reply", "event": event}

        # 4. Nothing pending. Do our scripted demo of real work, once.
        #    `_did_demo` matters: _demo_work() releases its lock and clears its
        #    task when it finishes, so without the flag this branch would be true
        #    again on the next tick and the agent would create a new task every
        #    ten seconds for as long as it ran.
        if not self._did_demo and not self._claimed_task and not self._held_locks:
            return {"kind": "demo_work"}

        # 5. O12 -- look before you build. Before concluding there is something
        #    we cannot do, read the registry and see whether somebody else can.
        #    An agent that reimplements what the agent beside it does in one call
        #    is the exact waste the Exchange exists to prevent.
        if self.requester is not None and not self._asked_peer:
            peers = self.session.who_offers("workspace.grep")
            if peers:
                return {"kind": "ask_peer", "provider": peers[0]}

        return None

    def act(self, action: dict) -> None:
        kind = action["kind"]

        if kind == "claim_task":
            task = action["task"]
            self._safe(lambda: self.client.emit("task.claim", {"id": task["id"]}))
            self._claimed_task = task["id"]
            self._set_psr(
                task["title"][:80],
                state="working",
                task=task["id"],
                detail=task.get("detail") or "",
                progress=0.1,
            )
            self._safe(lambda: self.client.say(
                "Claimed %s: %s" % (task["id"], task["title"]),
                refs=[{"kind": "task", "value": task["id"]}],
            ))

        elif kind == "flag_conflict":
            conflict = action["conflict"]
            path = conflict.get("path", "?")
            kept = conflict.get("kept_as", "?")
            # O6-adjacent: a conflict is a thing somebody must decide. Say so
            # with enough detail that it can be answered in one message, and
            # never merge silently -- SPEC 7.6 keeps both versions on purpose.
            self._safe(lambda: self.client.say(
                "Conflict on %s. My version was displaced to %s. I am not merging "
                "blind -- whoever owns this file, say which side wins and I will "
                "delete the sidecar." % (path, kept),
                refs=[{"kind": "file", "value": path}],
            ))
            self._set_psr(
                "Waiting on a merge decision for %s" % path,
                state="blocked",
                focus=[path],
                blocked_on={"reason": "conflict on %s needs a human or the file's owner" % path},
                needs=["decision on which side of the %s conflict wins" % path],
            )
            self.session.conflicts.remove(conflict)

        elif kind == "reply":
            event = action["event"]
            text = (event.get("body") or {}).get("text", "")
            # O5: cite what you are responding to. reply_to threads it; refs is
            # what the Ledger counts as influence -- for *them*, not for us.
            self._safe(lambda: self.client.say(
                "Acknowledged: %r. I am a reference agent, so I have no opinion "
                "yet -- wire a model into decide_next_action() and I will." % text[:120],
                reply_to=event.get("id"),
                refs=[{"kind": "event", "value": event.get("id")}],
            ))

        elif kind == "ask_peer":
            self.ask_peer_for_grep(action["provider"])

        elif kind == "demo_work":
            self._demo_work()

    def _demo_work(self) -> None:
        """One full cycle: announce, lock, work, record, release.

        This is the shape every piece of real work should take.
        """
        self._did_demo = True
        rel = "notes/%s.md" % self.client.agent_id[-8:]
        why = "writing my joining note so the others know what I am for"

        # O2 + O3 + O8.
        if not self.announce_intent("Writing my joining note", [rel], why):
            # Someone holds it. Perfectly normal; go idle and reconsider later.
            self._set_psr("Idle -- the file I wanted is held; looking for other work",
                          state="idle")
            return

        # Create a task so the work is visible on the board, then claim it.
        task_id = new_task_id()
        self._safe(lambda: self.client.emit("task.create", {
            "id": task_id,
            "title": "Write a joining note",
            "detail": "Reference agent demonstrating the full obligation cycle.",
            "tags": ["demo"],
            "priority": 4,
        }))
        self._safe(lambda: self.client.emit("task.claim", {"id": task_id}))
        self._claimed_task = task_id

        self._set_psr("Writing my joining note", state="working",
                      focus=[rel], task=task_id, progress=0.5)

        snapshot = self.session.snapshot()
        self.edit_synced_file(rel, "\n".join([
            "# Joining note",
            "",
            "- agent: `%s`" % self.client.agent_id,
            "- session: `%s`" % self.client.session,
            "- fingerprint: `%s`" % self.client.fingerprint,
            "- joined at seq: %d" % snapshot["last_seq"],
            "",
            "I am the reference agent from `examples/generic-agent/`. I follow the",
            "obligations in `AGENTS.md` section 6 but I have no model wired in, so I",
            "will not take on real work. Replace `decide_next_action()` to change that.",
            "",
        ]))

        # Give the sync loop a moment to pick the file up, so the task.done and
        # the contribution land after the file.put rather than before it.
        time.sleep(SYNC_INTERVAL_S + 1.0)

        self._safe(lambda: self.client.emit("task.done", {
            "id": task_id,
            "result": "Joining note written to %s" % rel,
            "refs": [{"kind": "file", "value": rel}],
        }))
        self._claimed_task = None

        # O4: record the conclusion, not the keystrokes.
        self.record(
            "Reference agent joined and is following the PSR contract",
            "doc",
            "examples/generic-agent/agent.py is running against this parley. It keeps a "
            "standing report every %ds, takes advisory locks before editing, and releases "
            "them when it stops. It has no model wired in, so it will not claim real work."
            % int(PSR_INTERVAL_S),
            refs=[rel],
        )

        # O7: release as soon as you stop. An abandoned lock makes the Deck lie.
        self.release([rel])
        self._set_psr("Idle -- joining note done, available for work", state="idle")

    # -- lifecycle ------------------------------------------------------------ #

    def _safe(self, call: Any) -> Any:
        """Never let one failed emit kill the agent.

        A rate limit, a brief Hub restart or a transient network error must
        degrade us, not stop us. SPEC 0.2 R7: degrade, don't die.
        """
        try:
            return call()
        except Exception as exc:
            log("emit failed (%s): %s" % (type(exc).__name__, exc))
            return None

    def run(self, duration_s: Optional[float] = None) -> int:
        log("agent %s (%s) joining" % (self.client.agent_id, self.client.fingerprint))

        if self.sync is not None:
            log("pulling the workspace...")
            self._safe(self.sync.bootstrap)

        if not self.observe:
            self._safe(self.client.announce)

        # O11 -- announce what you alone can do, after agent.hello so a peer
        # replaying the log knows who we are before it learns what we offer.
        # `capability.announce` is total and idempotent, so re-announcing on a
        # reconnect is both correct and cheap.
        if self.provider is not None:
            self.provider.register(self.grep_capability(), self.grep_handler)
            self._safe(self.provider.announce)

        for target in (self._stream_loop, self._psr_loop, self._heartbeat_loop,
                       self._sync_loop, self._pump_loop):
            thread = threading.Thread(target=target, name=target.__name__, daemon=True)
            thread.start()
            self.threads.append(thread)

        # AGENTS.md section 7: announce yourself with an honest state, then look
        # around before doing anything.
        self._set_psr("Reading the roster and the recent chat", state="planning")
        time.sleep(2.0)   # let the stream replay before we form an opinion

        if not self.observe:
            self._safe(lambda: self.client.say(
                "%s here (reference agent, Python). Reading the board; I will take "
                "an unclaimed task if there is one." % self.client.agent_id[-8:]
            ))

        started = time.time()
        try:
            while not self.stop.is_set():
                if duration_s is not None and time.time() - started > duration_s:
                    log("duration reached")
                    break

                state = self.session.snapshot()
                self._report_what_we_see(state)

                if not self.observe:
                    action = self.decide_next_action(state)
                    if action:
                        self.act(action)

                self.stop.wait(THINK_INTERVAL_S)
        except KeyboardInterrupt:
            log("interrupted")

        return self.shutdown()

    def _report_what_we_see(self, state: dict) -> None:
        others = [a for a in state["agents"] if a != self.session.me]
        offered = sum(len(t) for a, t in state["capabilities"].items()
                      if a != self.session.me)
        log("seq=%d agents=%d chat=%d tasks=%d locks=%d conflicts=%d offered=%d"
            % (state["last_seq"], len(others), len(state["chat"]),
               len(state["tasks"]), len(state["locks"]), len(state["conflicts"]),
               offered))
        for agent_id, psr in state["psr"].items():
            if agent_id == self.session.me:
                continue
            log("  %s [%s] %s" % (agent_id[-8:], psr.get("state", "?"),
                                  psr.get("headline", "(no headline)")))
        for agent_id, table in state["capabilities"].items():
            if agent_id == self.session.me:
                continue
            for name, cap in sorted(table.items()):
                log("  %s offers %s [%s] -- %s"
                    % (agent_id[-8:], name, cap.get("safety", "?"),
                       str(cap.get("title") or "")[:60]))

    def shutdown(self) -> int:
        """Leave cleanly. O7, and then some.

        Order matters: release claims while we can still be heard, then say
        goodbye, then stop the threads. An agent that just vanishes leaves locks
        and claims that make the Deck lie for the next ten minutes.
        """
        log("shutting down")

        # O14, and the reason it comes first: everything we accepted is answered
        # BEFORE the transport closes. A result emitted after the socket is gone
        # is a result nobody receives, and an unanswered accept is the one
        # unforgivable Exchange behaviour. `shutdown()` settles every outstanding
        # job with ok:false and declines anything still awaiting consent.
        if self.provider is not None:
            self._safe(self.provider.shutdown)

        if not self.observe:
            if self._held_locks:
                self.release(list(self._held_locks))
            if self._claimed_task:
                self._safe(lambda: self.client.emit("task.release", {
                    "id": self._claimed_task,
                    "reason": "agent is stopping",
                }))
                self._claimed_task = None

            self._set_psr("Offline", state="offline")
            self._safe(lambda: self.client.say("Signing off."))

        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=3.0)

        if self.sync is not None:
            self._safe(self.sync.save_index)
        if not self.observe:
            self._safe(lambda: self.client.bye("done"))
        self._safe(self.client.close)
        log("bye")
        return 0


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #

def log(message: str) -> None:
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), message))
    sys.stderr.flush()


def connect(opts: argparse.Namespace) -> ParleyClient:
    """Reuse stored credentials when we have them; enrol when we do not.

    SPEC 3.4: the agent key is shown exactly once, at enrolment, and lives in
    <workspace>/.parley/credentials.json at mode 0600. Re-enrolling when you
    already have credentials just burns the enrolment rate limit.
    """
    workspace = Path(opts.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    creds_path = workspace / ".parley" / "credentials.json"
    if creds_path.is_file() and not opts.invite:
        log("using stored credentials from %s" % creds_path)
        return ParleyClient(workspace, Credentials.load(workspace))

    if not opts.invite:
        raise SystemExit(
            "No credentials in %s and no --invite given.\n"
            "First run needs:  --hub URL --invite \"the watchword\" --name YOURNAME\n"
            % workspace
        )
    if not opts.hub:
        raise SystemExit("--invite given but no --hub URL. See AGENTS.md section 4.")

    log("enrolling at %s" % opts.hub)
    client = ParleyClient.enroll(
        opts.hub,
        opts.invite,
        workspace,
        name=opts.name,
        kind=opts.kind,
        model=opts.model,
        capabilities=["chat", "sync", "tasks", "psr"],
        sealed=opts.seal,
        # If you were told the fingerprint in advance, pass it: enrolment then
        # fails loudly rather than silently joining the wrong Hub. SPEC 3.5.
        expect_fingerprint=opts.expect_fingerprint,
    )
    log("enrolled as %s" % client.agent_id)
    log("FINGERPRINT: %s  <- this must match what the host read out" % client.fingerprint)
    return client


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="A complete reference Parley participant.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--workspace", default=".",
                        help="the synced folder (default: current directory)")
    parser.add_argument("--hub", default="",
                        help="Hub URL; only needed on the first run")
    parser.add_argument("--invite", default="",
                        help="the watchword; only needed on the first run")
    parser.add_argument("--name", default="reference-agent",
                        help="what other participants see")
    parser.add_argument("--kind", default="generic",
                        help="what sort of agent you are")
    parser.add_argument("--model", default="",
                        help="model identifier, if you are backed by one")
    parser.add_argument("--expect-fingerprint", default="", dest="expect_fingerprint",
                        help="the three words you were told; enrolment fails if they differ")
    parser.add_argument("--seal", action="store_true",
                        help="sealed mode; required if the Hub was started with --seal")
    parser.add_argument("--no-sync", action="store_true", dest="no_sync",
                        help="take part in chat but do not sync files")
    parser.add_argument("--observe", action="store_true",
                        help="read everything, emit nothing (useful for debugging; "
                             "deliberately non-conforming -- it does not report, "
                             "and it cannot decline)")
    parser.add_argument("--no-exchange", action="store_true", dest="no_exchange",
                        help="do not lend capabilities and do not ask for any; "
                             "requests addressed to this agent are still declined")
    parser.add_argument("--duration", type=float, default=None,
                        help="stop after N seconds (default: run until Ctrl-C)")
    opts = parser.parse_args(argv)

    client = connect(opts)
    workspace = Path(opts.workspace).expanduser().resolve()

    sync = None
    if not opts.no_sync:
        sync = WorkspaceSync(client, workspace)

    agent = Agent(client, workspace, sync=sync, observe=opts.observe,
                  exchange=not opts.no_exchange)

    # SIGINT and SIGTERM both mean "leave cleanly", so the shutdown path runs and
    # our locks and claims are released rather than left to expire.
    def _signal(_signum: int, _frame: Any) -> None:
        log("signal received; leaving cleanly")
        agent.stop.set()

    signal.signal(signal.SIGINT, _signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal)

    return agent.run(duration_s=opts.duration)


if __name__ == "__main__":
    raise SystemExit(main())
