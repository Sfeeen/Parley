"""The Exchange, client side: lending capabilities out and asking for help (SPEC §15).

:mod:`parley.exchange` decides *whether* a request should run. This module is what
actually runs it, and its problems are operational rather than logical.

The one unforgivable behaviour
------------------------------
SPEC §15.3: "silently dropping an accepted request is the one unforgivable Exchange
behaviour". That is not met with care, it is met with structure. Once
:meth:`Provider.accept` has put ``request.accept`` on the log, a :class:`_Job`
exists, and **four independent mechanisms** each guarantee a terminal event:

1. The worker thread settles the job in a ``finally``, so a handler that raises,
   returns, or is killed by an exception in our own glue still answers.
2. :meth:`Provider.pump` is a watchdog on the wall clock. A handler that hangs past
   ``timeout_s`` is settled *without* its thread, which is abandoned rather than
   waited on — a wedged USB read must not wedge the agent.
3. :meth:`Provider.shutdown` sweeps every job still outstanding and answers it with
   ``ok:false``. Exiting is not an excuse to leave a peer blocked.
4. Settling is idempotent under the lock (``job.settled``), and an emit that fails
   because the Hub is unreachable goes on :attr:`Provider._unsent` and is retried by
   ``pump``. A transient network failure therefore cannot turn into an abandonment.

The only remaining way to drop an accepted request is to kill the process, and the
Hub covers that with ``request.expired``, which names who did it.

Threads
-------
Handlers never run on the stream thread. Each accepted request gets its own daemon
thread, and the number alive at once is bounded by ``max_workers`` (a semaphore) and,
per capability, by the ``concurrency`` the provider announced. Excess is declined
``busy`` with a ``retry_after_s`` rather than queued forever, because a caller that
knows it was refused can go elsewhere and a caller sitting in a queue cannot.

One lock, ``self._lock`` (an ``RLock``), guards the catalogue, the job table and the
pending-consent table. It is **never** held across an ``emit`` — the network call is
always made after the decision is taken and the lock released, which is what keeps a
slow Hub from stalling the stream thread that feeds :meth:`Provider.on_event`.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import errors
from ..config import workspace_state_dir
from ..exchange import (
    DEFAULT_TIMEOUT_S,
    Capability,
    Decision,
    Policy,
    Registry,
    RequestTracker,
    make_request,
    validate_input,
)
from ..jsonutil import atomic_write, dumps, loads, now_rfc3339

log = logging.getLogger("parley.client.exchange")

#: ``(input, request_record) -> output``. May raise: the runtime turns that into
#: ``request.result{ok:false}``. Returning ``(output, output_text)`` sets both halves
#: of the result, which is the shape SPEC §15.3 asks for — structure for code, prose
#: for the next model in the chain.
Handler = Callable[[dict, dict], Any]

#: Where result files land in the workspace, matching the SPEC §15.3 example.
HANDOFF_DIR = "handoff"

#: A result body larger than this travels as a workspace file instead. The §2 limit
#: is 256 KiB for the *whole* body; this leaves room for the envelope and for an
#: ``output_text`` summary alongside the pointer.
MAX_INLINE_OUTPUT_BYTES = 160 * 1024

#: Answer this many seconds before the nominal deadline, so our own failed result
#: beats the Hub's ``request.expired`` and the record says "failed", not "abandoned".
TERMINAL_GRACE_S = 2.0

#: Default size of the worker pool. Small on purpose: an agent lending a bench or a
#: GPU has one of the thing, not eight.
DEFAULT_MAX_WORKERS = 4

#: Pigeonhole sidecars (SPEC §15.5), so a file-only agent finds its work without
#: parsing the whole log.
REQUESTS_FILE = "requests.json"
PENDING_FILE = "pending.json"
CAPABILITIES_FILE = "capabilities.json"


class _Job:
    """One accepted request and everything needed to guarantee it gets an answer."""

    __slots__ = (
        "req_id", "record", "capability", "handler", "started", "deadline",
        "thread", "settled", "permit", "cancelled", "manual",
    )

    def __init__(
        self,
        req_id: str,
        record: Dict[str, Any],
        capability: Optional[Capability],
        handler: Optional[Handler],
        started: float,
        deadline: float,
        manual: bool,
    ) -> None:
        self.req_id = req_id
        self.record = record
        self.capability = capability
        self.handler = handler
        self.started = started
        self.deadline = deadline
        self.thread: Optional[threading.Thread] = None
        self.settled = False
        self.permit = False
        self.cancelled = False
        self.manual = manual

    @property
    def cap_name(self) -> str:
        return self.capability.name if self.capability is not None else ""


class Provider:
    """Announces this agent's capabilities and executes delegated work under policy.

    Create one per :class:`~parley.client.client.ParleyClient`. Register what this
    agent can lend, call :meth:`announce`, feed it the event stream through
    :meth:`on_event`, and call :meth:`pump` on a tick. :class:`~parley.client.runtime.Runtime`
    does all four; an agent embedding the client directly does them itself.
    """

    def __init__(
        self,
        client: Any,
        workspace: Path,
        *,
        policy: Optional[Policy] = None,
        max_workers: int = DEFAULT_MAX_WORKERS,
        tracker: Optional[RequestTracker] = None,
    ) -> None:
        self.client = client
        self.workspace = Path(workspace).resolve()
        self.state_dir = workspace_state_dir(self.workspace)
        self.policy = policy if policy is not None else Policy.load(self.workspace)
        self.tracker = tracker if tracker is not None else RequestTracker()

        #: Called with ``(record, decision)`` when a request needs the operator.
        #: The runtime points this at the log and the Deck; a harness can point it
        #: anywhere. It must not block — it runs on the stream thread.
        self.on_ask: Optional[Callable[[Dict[str, Any], Decision], None]] = None

        self._lock = threading.RLock()
        self._caps: Dict[str, Capability] = {}
        self._handlers: Dict[str, Handler] = {}
        self._jobs: Dict[str, _Job] = {}
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._unsent: List[Tuple[str, Dict[str, Any]]] = []
        self._pool = threading.Semaphore(max(1, int(max_workers)))
        self._max_workers = max(1, int(max_workers))
        self._abandoned = 0
        self._closed = False
        self._sidecars_dirty = True

        self.stats: Dict[str, int] = {
            "accepted": 0, "declined": 0, "done": 0, "failed": 0,
            "timed_out": 0, "asked": 0, "abandoned_threads": 0,
        }

    # ------------------------------------------------------------------ #
    # catalogue
    # ------------------------------------------------------------------ #
    @property
    def agent_id(self) -> str:
        return getattr(self.client, "agent_id", "")

    def register(self, cap: Capability, handler: Optional[Handler] = None) -> None:
        """Add a capability, with the function that fulfils it.

        ``handler=None`` means *manual* fulfilment: the runtime surfaces the request
        through ``.parley/requests.json`` and ``parley fulfil`` instead of running
        anything. That is how a ``kind: "human"`` capability works, and how a
        pigeonhole agent lends a skill it implements in its own loop.

        Raises :class:`~parley.errors.BadEvent` on a malformed announcement. An
        announcement is a promise to other agents; publishing a broken one is worse
        than refusing to publish it.
        """
        problems = cap.validate()
        if problems:
            raise errors.BadEvent(
                "capability {0!r} is not well-formed".format(cap.name or "<unnamed>"),
                detail={"problems": problems},
                hint="SPEC §15.1 lists the fields; `description` is the one that matters most.",
            )
        cap.agent_id = self.agent_id
        cap.agent_name = getattr(getattr(self.client, "creds", None), "name", "") or ""
        with self._lock:
            self._caps[cap.name] = cap
            if handler is not None:
                self._handlers[cap.name] = handler
            else:
                self._handlers.pop(cap.name, None)

    def unregister(self, name: str) -> None:
        with self._lock:
            self._caps.pop(name, None)
            self._handlers.pop(name, None)

    def register_from_file(self, path: Optional[Path] = None) -> int:
        """Load ``.parley/capabilities.json`` — the pigeonhole announcement path.

        Every capability loaded this way is manual: a file cannot carry a function.
        Returns how many were registered; a malformed entry is logged and skipped so
        one typo does not silence the whole catalogue.
        """
        target = Path(path) if path is not None else (self.state_dir / CAPABILITIES_FILE)
        try:
            raw = target.read_bytes()
        except OSError:
            return 0
        try:
            doc = loads(raw)
        except errors.ParleyError as exc:
            log.warning("capabilities file %s is not valid JSON (%s)", target, exc)
            return 0
        if isinstance(doc, dict):
            items = doc.get("capabilities")
        else:
            items = doc
        if not isinstance(items, list):
            log.warning("capabilities file %s must hold a list of capabilities", target)
            return 0
        count = 0
        for item in items:
            if not isinstance(item, Mapping):
                continue
            cap = Capability.from_dict(item)
            try:
                self.register(cap, None)
            except errors.BadEvent as exc:
                log.warning(
                    "ignoring capability %r from %s: %s",
                    cap.name or "<unnamed>", target, exc,
                )
                continue
            count += 1
        return count

    def catalogue(self) -> List[Capability]:
        with self._lock:
            return [self._caps[name] for name in sorted(self._caps)]

    def announce(self) -> Optional[dict]:
        """Publish the whole catalogue (SPEC §15.1: announce is total, not a diff).

        Safe to call as often as you like, and correct to call on every reconnect:
        the receiver replaces whatever it had, so a capability that disappeared
        while we were away stops being offered without anyone sending a revoke.
        """
        caps = [cap.to_dict() for cap in self.catalogue()]
        body = {"capabilities": caps}
        try:
            event = self.client.emit("capability.announce", body)
        except errors.ParleyError as exc:
            log.warning("could not announce %d capabilit(ies): %s", len(caps), exc)
            return None
        log.info("announced %d capabilit(ies) to the parley", len(caps))
        return event

    def revoke(self, names: Sequence[str]) -> Optional[dict]:
        """Withdraw capabilities — the device was unplugged, the MCP server died."""
        wanted = [n for n in names if isinstance(n, str)]
        for name in wanted:
            self.unregister(name)
        try:
            return self.client.emit("capability.revoke", {"names": wanted})
        except errors.ParleyError as exc:
            log.warning("could not revoke %s: %s", ", ".join(wanted), exc)
            return None

    # ------------------------------------------------------------------ #
    # the event stream
    # ------------------------------------------------------------------ #
    def on_event(self, event: Mapping[str, Any]) -> None:
        """Route one event. Runs on the stream thread, so it never blocks on work."""
        if not isinstance(event, Mapping):
            return
        etype = event.get("type")
        if not isinstance(etype, str) or not etype.startswith("request."):
            return
        now = time.time()
        self.tracker.apply(event, now=now)
        body = event.get("body")
        if not isinstance(body, Mapping):
            return
        req_id = body.get("id")
        if not isinstance(req_id, str) or not req_id:
            return
        actor = event.get("actor")

        try:
            if etype == "request.create":
                if actor != self.agent_id and self._is_for_me(req_id):
                    self._consider(req_id)
            elif etype == "request.cancel":
                self._on_cancel(req_id)
            elif etype == "request.taken":
                if body.get("by") != self.agent_id:
                    self._drop_pending(req_id, "another agent took it first")
            elif etype == "request.accept":
                if actor != self.agent_id:
                    self._drop_pending(req_id, "another agent accepted it first")
            elif etype == "request.expired":
                self._on_expired(req_id)
        except Exception as exc:  # noqa: BLE001 - the stream must never break
            log.error("exchange: could not handle %s for %s: %s", etype, req_id, exc)

    def _is_for_me(self, req_id: str) -> bool:
        record = self.tracker.get(req_id)
        if record is None:
            return False
        to = record.get("to")
        if to == self.agent_id:
            return True
        if to != "any":
            return False
        # `to: "any"` is an open offer; only answer it if we actually hold the thing.
        name = record.get("capability")
        if not name:
            return True  # a free-form instruction anyone could attempt
        with self._lock:
            return name in self._caps

    # ------------------------------------------------------------------ #
    # consent
    # ------------------------------------------------------------------ #
    def _consider(self, req_id: str) -> None:
        """Decide what to do with a new request, then do it. Never raises."""
        record = self.tracker.get(req_id)
        if record is None:
            return
        name = record.get("capability")
        is_instruction = not name and bool(record.get("instruction"))

        with self._lock:
            cap = self._caps.get(name) if name else None
            running_here = sum(
                1 for job in self._jobs.values()
                if job.cap_name == name and not job.settled
            )
            in_flight = sum(1 for job in self._jobs.values() if not job.settled)

        # 1 -- a capability we do not have. Say so plainly: the caller can re-read
        #      /v1/capabilities and ask someone who does.
        if name and cap is None:
            self._decline(
                req_id,
                "I do not offer a capability called {0!r}.".format(name[:64]),
                "unknown_capability",
            )
            return

        # 2 -- consent. A deny here is final and nothing below can widen it.
        now = time.time()
        decision = self.policy.evaluate(
            requester=record.get("from", ""),
            capability=cap,
            is_instruction=is_instruction,
            in_flight=in_flight,
            recent_from_requester=self.tracker.recent_from(
                record.get("from", ""), self.agent_id, now=now
            ),
            has_reason=bool(record.get("reason")),
        )
        log.debug(
            "exchange: %s from %s -> %s (%s)",
            req_id, record.get("from"), decision.action, decision.detail,
        )
        if decision.action == "deny":
            self._decline(req_id, decision.why, decision.code,
                          retry_after_s=decision.retry_after_s)
            return

        # 3 -- SPEC §15.4 rule 4: validate the caller's input against *our* schema
        #      before anything executes. Never trust the caller to have done it.
        if cap is not None:
            problems = validate_input(cap.input_schema, record.get("input"))
            if problems:
                self._decline(
                    req_id,
                    "Your `input` does not match my schema: " + "; ".join(problems[:4]),
                    "bad_input",
                )
                return

        # 4 -- the provider's own capacity, per capability (SPEC §15.6).
        if cap is not None and running_here >= max(1, cap.concurrency):
            self._decline(
                req_id,
                "I can only run {0} of this at a time and I am already busy.".format(
                    cap.concurrency
                ),
                "busy",
                retry_after_s=int(cap.avg_duration_s) or 30,
            )
            return

        if decision.action == "allow":
            self.accept(req_id)
            return
        self._park_for_consent(req_id, record, decision)

    def _park_for_consent(
        self, req_id: str, record: Dict[str, Any], decision: Decision
    ) -> None:
        """An ``ask``: surface it and wait, rather than deciding on the operator's behalf."""
        entry = {
            "id": req_id,
            "from": record.get("from", ""),
            "capability": record.get("capability"),
            "instruction": record.get("instruction"),
            "input": record.get("input", {}),
            "reason": record.get("reason", ""),
            "safety": decision.safety,
            "why": decision.why,
            "detail": decision.detail,
            "asked_at": now_rfc3339(),
            "asked_ts": time.time(),
            "deadline_ts": record.get("created_ts", time.time()) + float(record.get("timeout_s", DEFAULT_TIMEOUT_S)),
            "timeout_s": record.get("timeout_s", DEFAULT_TIMEOUT_S),
            "decline_code": decision.code or "needs_human",
        }
        with self._lock:
            self._pending[req_id] = entry
            self.stats["asked"] += 1
            self._sidecars_dirty = True
        log.info(
            "request %s from %s needs approval: %s",
            req_id, record.get("from"), decision.why,
        )
        hook = self.on_ask
        if hook is not None:
            try:
                hook(dict(record), decision)
            except Exception as exc:  # noqa: BLE001 - a bad hook is not a dropped request
                log.error("exchange: on_ask hook raised (%s)", exc)

    @property
    def pending_consent(self) -> List[Dict[str, Any]]:
        """Requests waiting on the operator, oldest first."""
        with self._lock:
            return sorted(
                (dict(e) for e in self._pending.values()),
                key=lambda e: e.get("asked_ts", 0.0),
            )

    def _drop_pending(self, req_id: str, why: str) -> None:
        with self._lock:
            entry = self._pending.pop(req_id, None)
            if entry is not None:
                self._sidecars_dirty = True
        if entry is not None:
            log.info("no longer considering %s: %s", req_id, why)

    # ------------------------------------------------------------------ #
    # accept / decline / fulfil
    # ------------------------------------------------------------------ #
    def accept(self, req_id: str, eta_s: float = 0) -> Optional[dict]:
        """Commit to a request and start the work.

        From the moment this returns, a terminal ``request.result`` is owed and the
        machinery in this module guarantees one. If the handler cannot even be
        started, a failed result is emitted before this call returns.
        """
        record = self.tracker.get(req_id)
        if record is None:
            log.warning("cannot accept %s: no such request in the log", req_id)
            return None
        with self._lock:
            if req_id in self._jobs:
                return None  # already ours; accepting twice is a no-op, not an error
            self._pending.pop(req_id, None)
            name = record.get("capability")
            cap = self._caps.get(name) if name else None
            handler = self._handlers.get(name) if name else None
            manual = handler is None
            timeout = float(record.get("timeout_s", DEFAULT_TIMEOUT_S))
            started = time.monotonic()
            job = _Job(
                req_id=req_id,
                record=record,
                capability=cap,
                handler=handler,
                started=started,
                deadline=started + max(1.0, timeout - TERMINAL_GRACE_S),
                manual=manual,
            )
            self._jobs[req_id] = job
            self.stats["accepted"] += 1
            self._sidecars_dirty = True

        body: Dict[str, Any] = {"id": req_id}
        eta = float(eta_s or 0.0) or float(
            cap.avg_duration_s if cap is not None else 0.0
        )
        if eta > 0:
            body["eta_s"] = eta
        event = self._emit("request.accept", body)

        if manual:
            log.info(
                "accepted %s for manual fulfilment; answer with `parley fulfil %s`",
                req_id, req_id,
            )
            self.write_sidecars()
            return event

        if not self._start(job):
            # Could not even get a thread. Answer now rather than owe an answer.
            self._settle(
                job, ok=False,
                error={
                    "code": "busy",
                    "message": "no worker thread was available",
                    "hint": "The provider is at its worker limit; retry shortly.",
                },
            )
        self.write_sidecars()
        return event

    def _start(self, job: _Job) -> bool:
        if not self._pool.acquire(blocking=False):
            return False
        job.permit = True
        thread = threading.Thread(
            target=self._run, args=(job,), name="parley-exchange-" + job.req_id[:12],
        )
        thread.daemon = True
        job.thread = thread
        try:
            thread.start()
        except RuntimeError as exc:  # thread limit reached
            log.error("could not start a worker for %s (%s)", job.req_id, exc)
            self._release(job)
            return False
        return True

    def _release(self, job: _Job) -> None:
        """Give the worker permit back, exactly once."""
        with self._lock:
            if not job.permit:
                return
            job.permit = False
        self._pool.release()

    def _run(self, job: _Job) -> None:
        """The worker. Every exit path settles the job — that is the whole point."""
        ok = False
        output: Any = None
        output_text = ""
        error: Optional[Dict[str, Any]] = None
        try:
            handler = job.handler
            if handler is None:  # pragma: no cover - _start is never called for manual
                raise RuntimeError("no handler registered")
            result = handler(dict(job.record.get("input") or {}), dict(job.record))
            output, output_text = _split_result(result)
            ok = True
        except BaseException as exc:  # noqa: BLE001 - a handler may raise anything
            # A handler that raises becomes a failed result with a useful message.
            # Never a silent drop, and never a traceback the requester cannot read.
            error = {
                "code": "handler_error",
                "message": "{0}: {1}".format(type(exc).__name__, exc)[:400],
                "hint": "The provider's handler raised; see that agent's log for the traceback.",
            }
            log.error("handler for %s raised: %s", job.req_id, error["message"])
            log.debug("%s", traceback.format_exc())
        finally:
            self._release(job)
            self._settle(job, ok=ok, output=output, output_text=output_text, error=error)

    def decline(self, req_id: str, reason: str, code: str = "policy") -> Optional[dict]:
        """Refuse a request. Declining is always acceptable and never a fault (SPEC §15.3)."""
        return self._decline(req_id, reason, code)

    def _decline(
        self, req_id: str, reason: str, code: str, *, retry_after_s: int = 0
    ) -> Optional[dict]:
        with self._lock:
            self._pending.pop(req_id, None)
            job = self._jobs.pop(req_id, None)
            self._sidecars_dirty = True
            self.stats["declined"] += 1
        if job is not None:
            job.settled = True
            self._release(job)
        body: Dict[str, Any] = {"id": req_id, "reason": reason[:400], "code": code}
        if retry_after_s > 0:
            body["retry_after_s"] = int(retry_after_s)
        log.info("declined %s (%s): %s", req_id, code, reason)
        event = self._emit("request.decline", body)
        self.write_sidecars()
        return event

    def progress(self, req_id: str, progress: Optional[float] = None, note: str = "") -> Optional[dict]:
        """Optional, encouraged for anything slow (SPEC §15.3)."""
        body: Dict[str, Any] = {"id": req_id}
        if progress is not None:
            body["progress"] = max(0.0, min(1.0, float(progress)))
        if note:
            body["note"] = note[:400]
        return self._emit("request.progress", body)

    def fulfil(
        self,
        req_id: str,
        *,
        output: Any = None,
        output_text: str = "",
        files: Optional[List[str]] = None,
        ok: bool = True,
        error: Optional[dict] = None,
    ) -> Optional[dict]:
        """Answer a manually-fulfilled request (``parley fulfil``, or a harness)."""
        with self._lock:
            job = self._jobs.get(req_id)
        if job is None:
            log.warning("cannot fulfil %s: this agent has not accepted it", req_id)
            return None
        return self._settle(
            job, ok=ok, output=output, output_text=output_text,
            error=error, files=files,
        )

    # ------------------------------------------------------------------ #
    # the terminal event
    # ------------------------------------------------------------------ #
    def _settle(
        self,
        job: _Job,
        *,
        ok: bool,
        output: Any = None,
        output_text: str = "",
        error: Optional[Mapping[str, Any]] = None,
        files: Optional[Sequence[str]] = None,
    ) -> Optional[dict]:
        """Emit the one terminal ``request.result`` this job owes. Idempotent.

        Everything that can produce a terminal event funnels through here, and the
        ``settled`` flag is checked and set under the lock. The watchdog and a
        late-finishing handler can therefore race freely: exactly one result is
        emitted and the loser is a no-op.
        """
        with self._lock:
            if job.settled:
                return None
            job.settled = True
            self._jobs.pop(job.req_id, None)
            self._sidecars_dirty = True
            self.stats["done" if ok else "failed"] += 1

        duration = max(0.0, time.monotonic() - job.started)
        body: Dict[str, Any] = {
            "id": job.req_id,
            "ok": bool(ok),
            "duration_s": round(duration, 3),
        }
        extra_files = list(files or [])
        if ok:
            payload, text, spilled = self._shrink(job.req_id, output, output_text)
            if payload is not None:
                body["output"] = payload
            if text:
                body["output_text"] = text
            extra_files.extend(spilled)
        else:
            body["error"] = dict(error or {
                "code": "other",
                "message": "the provider did not say why",
            })
            if output_text:
                body["output_text"] = output_text[:4000]
        if extra_files:
            body["files"] = extra_files
        return self._emit("request.result", body)

    def _shrink(
        self, req_id: str, output: Any, output_text: str
    ) -> Tuple[Any, str, List[str]]:
        """Keep the result inside the §2 256 KiB body limit.

        Anything bigger goes through the workspace as a file and is referenced in
        ``result.files`` — which is exactly the path SPEC §15.3 prescribes, and the
        reason file sync and the Exchange are the same system rather than two.
        """
        text = output_text[:8000] if isinstance(output_text, str) else ""
        if output is None:
            return None, text, []
        try:
            encoded = dumps(output).encode("utf-8")
        except (TypeError, ValueError) as exc:
            log.warning("result for %s is not JSON-serialisable (%s)", req_id, exc)
            return None, (text or "the provider's output could not be serialised"), []
        if len(encoded) <= MAX_INLINE_OUTPUT_BYTES:
            return output, text, []

        rel = "{0}/{1}/output.json".format(HANDOFF_DIR, req_id)
        target = self.workspace / HANDOFF_DIR / req_id / "output.json"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(target, encoded)
        except OSError as exc:
            log.error("could not spill the result for %s to %s (%s)", req_id, target, exc)
            return None, (
                text or "the result was too large to send and could not be written to the workspace"
            ), []
        log.info(
            "result for %s is %d bytes; sent through the workspace as %s",
            req_id, len(encoded), rel,
        )
        pointer = {
            "_too_large": True,
            "bytes": len(encoded),
            "file": rel,
        }
        if not text:
            text = "The full result is {0} bytes and is in the workspace at {1}.".format(
                len(encoded), rel
            )
        return pointer, text, [rel]

    def _emit(self, etype: str, body: Dict[str, Any]) -> Optional[dict]:
        """Publish, or queue for retry. Never raises.

        A terminal event that cannot be sent is not lost: it goes on ``_unsent`` and
        ``pump`` keeps trying. That is the difference between "the Hub was down for
        ten seconds" and "this agent abandoned a request".
        """
        try:
            return self.client.emit(etype, body)
        except errors.ParleyError as exc:
            if etype in ("request.result", "request.decline"):
                with self._lock:
                    self._unsent.append((etype, body))
                log.warning(
                    "could not send %s for %s (%s); queued for retry",
                    etype, body.get("id"), exc,
                )
            else:
                log.debug("could not send %s for %s (%s)", etype, body.get("id"), exc)
            return None
        except Exception as exc:  # noqa: BLE001
            log.error("unexpected failure sending %s (%s)", etype, exc)
            return None

    # ------------------------------------------------------------------ #
    # the tick
    # ------------------------------------------------------------------ #
    def pump(self, now: Optional[float] = None) -> None:
        """Watchdog + retry queue + sidecars. Call this on the daemon's tick.

        Three jobs, in order of how badly they matter:

        1. Drain ``_unsent``, so a result that could not be posted still lands.
        2. Settle anything past its deadline. A handler that hangs is abandoned,
           not awaited — the agent answers and carries on.
        3. Auto-decline an ``ask`` nobody answered in time, with ``needs_human``
           exactly as SPEC §15.4 prescribes.
        """
        wall = time.time() if now is None else float(now)
        mono = time.monotonic()

        with self._lock:
            queued, self._unsent = self._unsent, []
        for etype, body in queued:
            try:
                self.client.emit(etype, body)
                log.info("re-sent %s for %s", etype, body.get("id"))
            except errors.ParleyError:
                with self._lock:
                    self._unsent.append((etype, body))
            except Exception as exc:  # noqa: BLE001
                log.error("could not re-send %s (%s)", etype, exc)

        with self._lock:
            overdue = [
                job for job in self._jobs.values()
                if not job.settled and not job.manual and mono >= job.deadline
            ]
        for job in overdue:
            alive = job.thread is not None and job.thread.is_alive()
            if alive:
                # Abandon the thread rather than block on it. It is a daemon thread,
                # it holds no lock of ours, and the permit it occupies is returned
                # here so the pool does not shrink for the life of the process.
                with self._lock:
                    self.stats["abandoned_threads"] += 1
                    self._abandoned += 1
                self._release(job)
                job.cancelled = True
            self.stats["timed_out"] += 1
            log.warning(
                "request %s ran past its timeout of %ss; answering with a failure",
                job.req_id, job.record.get("timeout_s"),
            )
            self._settle(
                job, ok=False,
                error={
                    "code": "timeout",
                    "message": "the provider's handler did not finish within timeout_s",
                    "hint": "Raise timeout_s, or ask for a smaller piece of work.",
                },
            )

        with self._lock:
            stale = [
                entry for entry in self._pending.values()
                if wall >= entry.get("deadline_ts", 0.0)
            ]
        for entry in stale:
            self._decline(
                entry["id"],
                "Nobody here approved this in time, so I have to decline it.",
                "needs_human",
            )

        self.write_sidecars()

    # ------------------------------------------------------------------ #
    # pigeonhole sidecars (SPEC §15.5)
    # ------------------------------------------------------------------ #
    def write_sidecars(self, *, force: bool = False) -> None:
        """Rewrite ``.parley/requests.json`` and ``.parley/pending.json``.

        A file-only agent must be able to find its work without parsing the whole
        log, and the whole log is the only other place this information exists.
        """
        with self._lock:
            if not (force or self._sidecars_dirty):
                return
            self._sidecars_dirty = False
            pending = [dict(e) for e in self._pending.values()]
            held = set(self._jobs)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.debug("cannot create %s (%s)", self.state_dir, exc)
            return

        mine = self.tracker.addressed_to(self.agent_id)
        requests = {
            "updated": now_rfc3339(),
            "agent": self.agent_id,
            "requests": [
                {
                    "id": r["id"],
                    "from": r["from"],
                    "capability": r["capability"],
                    "instruction": r["instruction"],
                    "input": r["input"],
                    "reason": r["reason"],
                    "state": r["state"],
                    "accepted_by_me": r["id"] in held,
                    "timeout_s": r["timeout_s"],
                    "priority": r["priority"],
                    "created_at": r["created_at"],
                }
                for r in mine
            ],
            "note": (
                "In-flight requests addressed to you. Answer by appending a "
                "request.accept then a request.result line to outbox.jsonl, or "
                "a request.decline. SPEC §15.3."
            ),
        }
        awaiting = {
            "updated": now_rfc3339(),
            "agent": self.agent_id,
            "pending": sorted(pending, key=lambda e: e.get("asked_ts", 0.0)),
            "note": (
                "These need a human or an explicit decision before they run. "
                "Accept with `parley requests --accept ID`, or append a "
                "request.accept / request.decline line to outbox.jsonl."
            ),
        }
        for name, doc in ((REQUESTS_FILE, requests), (PENDING_FILE, awaiting)):
            try:
                atomic_write(self.state_dir / name, (dumps(doc) + "\n").encode("utf-8"))
            except OSError as exc:
                log.debug("could not write %s (%s)", name, exc)

    # ------------------------------------------------------------------ #
    # cancellation, expiry, shutdown
    # ------------------------------------------------------------------ #
    def _on_cancel(self, req_id: str) -> None:
        """SPEC §15.3: having accepted, we MUST still emit a terminal result."""
        self._drop_pending(req_id, "the caller withdrew it")
        with self._lock:
            job = self._jobs.get(req_id)
        if job is None:
            return
        job.cancelled = True
        alive = job.thread is not None and job.thread.is_alive()
        if alive:
            # Python cannot interrupt a thread, and pretending otherwise would be
            # worse than saying so. The permit comes back and the thread is left to
            # finish into a settle that is already a no-op.
            self._release(job)
            with self._lock:
                self.stats["abandoned_threads"] += 1
        self._settle(
            job, ok=False,
            error={
                "code": "cancelled",
                "message": "the caller cancelled this request",
                "hint": "",
            },
        )

    def _on_expired(self, req_id: str) -> None:
        with self._lock:
            job = self._jobs.pop(req_id, None)
            self._pending.pop(req_id, None)
            self._sidecars_dirty = True
        if job is not None and not job.settled:
            job.settled = True
            self._release(job)
            log.error(
                "request %s expired while this agent held it - that is the one "
                "unforgivable Exchange behaviour and the Ledger will charge for it",
                req_id,
            )

    def shutdown(self, *, timeout_s: float = 2.0) -> None:
        """Answer everything outstanding before the process goes away.

        Called from the daemon's shutdown path *before* the transport closes, which
        is why ``Runtime`` orders it that way: a result emitted after the socket is
        gone is a result nobody receives.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            jobs = list(self._jobs.values())
            pending = [dict(e) for e in self._pending.values()]

        deadline = time.monotonic() + max(0.0, timeout_s)
        for job in jobs:
            thread = job.thread
            if thread is None or not thread.is_alive():
                continue
            remaining = deadline - time.monotonic()
            if remaining > 0:
                thread.join(timeout=remaining)

        for job in jobs:
            if job.settled:
                continue
            self._settle(
                job, ok=False,
                error={
                    "code": "offline",
                    "message": "the provider shut down before finishing this request",
                    "hint": "Ask again when the agent is back; nothing was left half-done "
                            "on purpose.",
                },
            )
        for entry in pending:
            self._decline(
                entry["id"],
                "I am shutting down and cannot get this approved.",
                "offline",
            )
        with self._lock:
            queued, self._unsent = self._unsent, []
        for etype, body in queued:
            try:
                self.client.emit(etype, body)
            except Exception as exc:  # noqa: BLE001 - we are already on the way out
                log.error(
                    "could not deliver a terminal %s for %s on shutdown (%s); the hub "
                    "will mark it expired", etype, body.get("id"), exc,
                )
        self.write_sidecars(force=True)


def _split_result(result: Any) -> Tuple[Any, str]:
    """``(output, output_text)`` from whatever a handler returned.

    A 2-tuple sets both halves; a bare string is prose; anything else is structure.
    SPEC §15.3 asks for both where possible — the first is for code, the second is
    for the next model in the chain.
    """
    if isinstance(result, tuple) and len(result) == 2:
        output, text = result
        return output, text if isinstance(text, str) else ""
    if isinstance(result, str):
        return None, result
    return result, ""


# --------------------------------------------------------------------------- #
# the calling side
# --------------------------------------------------------------------------- #


class Requester:
    """Ask another agent to do something, and wait for the answer.

    Feed :meth:`on_event` from the stream if you have one — :class:`Runtime` does.
    Without a feed, :meth:`wait` falls back to polling ``/v1/events``, so a one-shot
    script with no daemon still works.
    """

    def __init__(self, client: Any, *, tracker: Optional[RequestTracker] = None) -> None:
        self.client = client
        self.tracker = tracker if tracker is not None else RequestTracker()
        self._cond = threading.Condition()
        self._fed = False
        self._since = 0

    @property
    def agent_id(self) -> str:
        return getattr(self.client, "agent_id", "")

    # -- posting --------------------------------------------------------------
    def ask(
        self,
        to: str,
        capability: str,
        input: Optional[dict] = None,  # noqa: A002 - the wire field is called `input`
        *,
        reason: str,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        priority: int = 3,
        refs: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> str:
        """A structured capability call. Returns the request id."""
        body = make_request(
            self.agent_id, to, capability=capability, input=dict(input or {}),
            reason=reason, timeout_s=timeout_s, priority=priority, refs=refs,
        )
        self._post(body)
        return body["id"]

    def instruct(
        self,
        to: str,
        instruction: str,
        *,
        reason: str,
        timeout_s: int = 600,
        expects: str = "text",
        priority: int = 3,
    ) -> str:
        """A free-form instruction, for when no capability fits.

        Expect it to need the other operator's approval: SPEC §15.4 rule 2 says a
        free-form instruction is never treated as ``safe``, because by construction
        nobody validated it against a schema.
        """
        body = make_request(
            self.agent_id, to, instruction=instruction, reason=reason,
            timeout_s=timeout_s, priority=priority, expects=expects,
        )
        self._post(body)
        return body["id"]

    def _post(self, body: Dict[str, Any]) -> None:
        event = self.client.emit("request.create", body)
        if isinstance(event, Mapping):
            self.tracker.apply(event, now=time.time())
        log.info(
            "asked %s for %s (%s)",
            body.get("to"), body.get("capability") or "a free-form instruction",
            body.get("id"),
        )

    def cancel(self, req_id: str, reason: str = "") -> Optional[dict]:
        body: Dict[str, Any] = {"id": req_id}
        if reason:
            body["reason"] = reason[:400]
        try:
            return self.client.emit("request.cancel", body)
        except errors.ParleyError as exc:
            log.warning("could not cancel %s (%s)", req_id, exc)
            return None

    # -- the stream -----------------------------------------------------------
    def on_event(self, event: Mapping[str, Any]) -> None:
        if not isinstance(event, Mapping):
            return
        etype = event.get("type")
        if not isinstance(etype, str) or not etype.startswith("request."):
            return
        self.tracker.apply(event, now=time.time())
        seq = event.get("seq")
        with self._cond:
            self._fed = True
            if isinstance(seq, int) and seq > self._since:
                self._since = seq
            self._cond.notify_all()

    # -- waiting --------------------------------------------------------------
    def wait(self, req_id: str, *, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        """Block until the request reaches a terminal state; return its record.

        For the duration the caller's PSR says ``waiting`` with ``blocked_on`` naming
        the provider, which is what makes the Deck's dependency view mean anything —
        an agent that is stuck on a peer should look stuck on that peer. The previous
        PSR is restored afterwards **including on an exception**, because a crash
        here must not leave the agent permanently claiming to be blocked.

        Returns the record even if the wait times out locally; check ``["state"]``.
        """
        record = self.tracker.get(req_id)
        limit = timeout_s
        if limit is None:
            base = float(record.get("timeout_s", DEFAULT_TIMEOUT_S)) if record else DEFAULT_TIMEOUT_S
            limit = base + 30.0
        deadline = time.monotonic() + max(0.0, float(limit))

        previous = getattr(self.client, "last_psr", None)
        self._announce_waiting(record, req_id)
        try:
            while True:
                record = self.tracker.get(req_id)
                if record is not None and record["state"] not in ("pending", "accepted"):
                    return record
                if time.monotonic() >= deadline:
                    log.warning("stopped waiting for %s after %.0fs", req_id, float(limit))
                    return record or {"id": req_id, "state": "unknown"}
                with self._cond:
                    fed = self._fed
                    self._cond.wait(0.5 if fed else 0.25)
                if not fed:
                    self._poll()
        finally:
            self._restore_psr(previous)

    def _poll(self) -> None:
        """Catch up from ``/v1/events`` when nothing is feeding us the stream."""
        try:
            events = self.client.events(since=self._since, limit=500)
        except errors.ParleyError as exc:
            log.debug("could not poll for request events (%s)", exc)
            return
        now = time.time()
        for event in events:
            if not isinstance(event, Mapping):
                continue
            seq = event.get("seq")
            if isinstance(seq, int) and seq > self._since:
                self._since = seq
            etype = event.get("type")
            if isinstance(etype, str) and etype.startswith("request."):
                self.tracker.apply(event, now=now)

    def _announce_waiting(self, record: Optional[Mapping[str, Any]], req_id: str) -> None:
        provider = str((record or {}).get("to") or "?")
        what = (record or {}).get("capability") or "a delegated instruction"
        try:
            self.client.status(
                "Waiting on {0} for {1}".format(_short(provider), str(what)[:40]),
                state="waiting",
                detail="Request {0}; nothing to do here until it answers.".format(req_id),
                blocked_on={"agent": provider, "reason": str(what)[:120]},
            )
        except errors.ParleyError as exc:
            log.debug("could not publish the waiting PSR (%s)", exc)

    def _restore_psr(self, previous: Optional[Mapping[str, Any]]) -> None:
        try:
            if previous:
                self.client.emit("status.update", dict(previous))
            else:
                self.client.status(
                    "Back from waiting on a request", state="idle",
                )
        except errors.ParleyError as exc:
            log.debug("could not restore the PSR after waiting (%s)", exc)
        except Exception as exc:  # noqa: BLE001 - never mask the caller's exception
            log.error("could not restore the PSR after waiting (%s)", exc)

    # -- discovery ------------------------------------------------------------
    def discover(self, *, kind: str = "", agent_id: str = "") -> List[Capability]:
        """Read the merged registry (SPEC §15.2) and return it as capabilities.

        An agent SHOULD consult this before doing something the hard way. It is a
        read of ``/v1/capabilities``, falling back to the ``capabilities`` block of
        ``/v1/state`` for a Hub that only publishes the snapshot.
        """
        doc: Any = None
        try:
            doc = self.client.transport.get_json("/v1/capabilities")
        except errors.ParleyError as exc:
            log.debug("/v1/capabilities not available (%s); trying /v1/state", exc)
            try:
                doc = (self.client.state() or {}).get("capabilities")
            except errors.ParleyError as exc2:
                log.warning("could not read the capability registry (%s)", exc2)
                return []
        rows = doc.get("capabilities") if isinstance(doc, Mapping) else doc
        if not isinstance(rows, list):
            return []
        out = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            cap = Capability.from_dict(row)
            if kind and cap.kind != kind:
                continue
            if agent_id and cap.agent_id != agent_id:
                continue
            out.append(cap)
        return out


def _short(agent_id: str) -> str:
    body = str(agent_id).split("_", 1)[-1]
    return body[:6] if body else "?"


def build_registry(events: Sequence[Mapping[str, Any]]) -> Registry:
    """Fold ``capability.*`` events into a :class:`Registry`.

    Shared by the Hub's snapshot builder and by any client that would rather read
    the log than call ``/v1/capabilities``. Lives here rather than in the pure core
    because it is a convenience over a sequence of events, not part of the model.
    """
    registry = Registry()
    names: Dict[str, str] = {}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        actor = event.get("actor")
        body = event.get("body")
        if not isinstance(actor, str) or not isinstance(body, Mapping):
            continue
        etype = event.get("type")
        if etype == "agent.hello":
            name = body.get("name")
            if isinstance(name, str) and name:
                names[actor] = name
        elif etype == "capability.announce":
            caps = body.get("capabilities")
            registry.announce(actor, names.get(actor, ""), caps if isinstance(caps, list) else [])
        elif etype == "capability.revoke":
            caps = body.get("names")
            registry.revoke(actor, caps if isinstance(caps, list) else [])
        elif etype in ("agent.offline", "agent.bye", "agent.revoked"):
            target = body.get("agent_id")
            registry.drop_agent(target if isinstance(target, str) and target else actor)
    return registry
