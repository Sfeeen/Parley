"""The long-running participant daemon - what ``parley run`` runs.

One process, five concerns, each on its own thread so that a slow one cannot
stall the others:

==============  =====================================================
Thread          Responsibility
==============  =====================================================
``stream``      Consume the Hub's event log; apply ``file.*`` to disk and
                mirror everything into the pigeonhole.
``sync``        Poll the workspace and publish local changes.
``house``       Heartbeats, the PSR freshness contract, the outbox drain,
                ``me.json``, and the ``state.json``/``roster.json`` mirrors.
main            Install signal handlers, supervise, shut down cleanly.
==============  =====================================================

Supervision policy
------------------
A daemon that is half dead is worse than one that exited. Every worker runs
inside :meth:`_guard`, which logs the full traceback, records the failure and
sets the shutdown event; the main thread then stops the rest, emits
``agent.bye`` and returns a non-zero exit code. The operator (or the shell loop
restarting us) therefore always learns that something broke. Routine, expected
failures - the Hub being unreachable, a file vanishing mid-scan - are handled
*inside* the workers and never reach the guard, because R7 says degrade, do not
die.

Shutdown
--------
``SIGINT`` and, where the OS has it, ``SIGTERM`` (and ``SIGBREAK`` on Windows)
set a single :class:`threading.Event`. Every blocking wait in this package is an
``Event.wait``, not a bare ``sleep``, so shutdown is prompt rather than
"whenever the poll interval happens to elapse". A second signal force-exits, for
the case where the network stack is wedged and a clean ``agent.bye`` would hang.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .. import errors
from ..config import Credentials, DEFAULTS, workspace_state_dir
from ..jsonutil import atomic_write, dumps, loads
from .client import ParleyClient
from .pigeonhole import Pigeonhole
from .sync import WorkspaceSync

log = logging.getLogger("parley.client.runtime")

#: Exit codes, SPEC 11.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH = 3
EXIT_UNREACHABLE = 4

CURSOR_FILE = "stream.json"

#: How often the house thread wakes. Everything it does is cheap and rate
#: limited internally; a short tick keeps outbox latency low for a pigeonhole
#: agent, which is the interactive path.
TICK_S = 0.5

#: Persist the stream cursor at most this often, and at least every N events.
CURSOR_SAVE_INTERVAL_S = 2.0
CURSOR_SAVE_EVERY = 50


class Runtime:
    """Supervises the client's background work for the life of a parley."""

    def __init__(
        self,
        workspace: Path,
        *,
        sync: bool = True,
        pigeonhole: bool = True,
        psr_from: str = "",
        client: Optional[ParleyClient] = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.state_dir = workspace_state_dir(self.workspace)
        self.want_sync = bool(sync)
        self.want_pigeonhole = bool(pigeonhole)
        #: `parley run --psr-from FILE` (SPEC 11). A bare filename is read from
        #: `.parley/`, which is where an agent naturally writes it; a path with
        #: a separator is taken as given so a harness can point at its own file.
        self.psr_from = str(psr_from or "")

        self.client: Optional[ParleyClient] = client
        self.sync: Optional[WorkspaceSync] = None
        self.pigeonhole: Optional[Pigeonhole] = None

        self._stop = threading.Event()
        self._threads: Dict[str, threading.Thread] = {}
        self._failure: Optional[Tuple[str, BaseException]] = None
        self._failure_lock = threading.Lock()
        self._signal_count = 0
        self._cursor_path = self.state_dir / CURSOR_FILE
        self._since = 0
        self._since_lock = threading.Lock()
        self._last_cursor_save = 0.0
        self._events_since_save = 0
        self._exit_code = EXIT_OK
        self._connected = False
        # Policy defaults stand in until `_setup` learns the Hub's real ones, so
        # that a failure during startup can never leave these undefined.
        self.policy: Dict[str, Any] = dict(DEFAULTS)
        self.heartbeat_s = float(DEFAULTS.get("heartbeat_s", 15) or 15)
        self.psr_max_age_s = float(DEFAULTS.get("psr_max_age_s", 30) or 30)

    # ------------------------------------------------------------------ #
    # public
    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        """Ask every thread to finish. Safe to call from any thread or a signal."""
        self._stop.set()

    def run(self) -> int:
        """Block until stopped; return a SPEC 11 exit code."""
        try:
            self._setup()
        except errors.ParleyError as exc:
            code = _exit_code_for(exc)
            log.error("cannot start: %s", exc)
            hint = getattr(exc, "hint", "")
            if hint:
                log.error("hint: %s", hint)
            return code
        except Exception as exc:  # noqa: BLE001
            log.error("cannot start: %s", exc)
            log.debug("%s", traceback.format_exc())
            return EXIT_ERROR

        self._install_signals()
        self._start_threads()
        log.info(
            "parley daemon running in %s as %s (sync=%s, pigeonhole=%s)",
            self.workspace, self.client.creds.name if self.client else "?",
            self.want_sync, self.want_pigeonhole,
        )
        try:
            while not self._stop.wait(0.5):
                with self._failure_lock:
                    failed = self._failure
                if failed is not None:
                    self._exit_code = EXIT_ERROR
                    self._stop.set()
        except KeyboardInterrupt:  # pragma: no cover - belt and braces
            self._stop.set()
        return self._shutdown()

    # ------------------------------------------------------------------ #
    # setup / teardown
    # ------------------------------------------------------------------ #
    def _setup(self) -> None:
        if self.client is None:
            try:
                creds = Credentials.load(self.workspace)
            except FileNotFoundError:
                # UnknownAgent, not a generic error, so `run` exits 3
                # ("auth/credential failure") exactly as SPEC 11 prescribes.
                raise errors.UnknownAgent(
                    "no credentials in {0}/.parley".format(self.workspace),
                    hint="Run `parley join --hub URL --invite \"watchword\"` first.",
                )
            except (OSError, ValueError) as exc:
                raise errors.UnknownAgent(
                    "credentials in {0}/.parley are unreadable: {1}".format(self.workspace, exc),
                    hint="Delete .parley/credentials.json and re-join.",
                )
            self.client = ParleyClient(self.workspace, creds)

        policy = dict(DEFAULTS)
        policy.update(self.client.policy or {})
        self.policy = policy
        self.heartbeat_s = float(policy.get("heartbeat_s", 15) or 15)
        self.psr_max_age_s = float(policy.get("psr_max_age_s", 30) or 30)

        # An early probe turns "wrong credentials" into a clear exit code
        # instead of an endless, silent reconnect loop. Unreachability, by
        # contrast, is survivable (R7): the stream will keep trying.
        try:
            self.client.state()
            self._connected = True
        except (errors.UnknownAgent, errors.BadSignature, errors.Revoked) as exc:
            raise errors.ParleyError(
                "the hub rejected these credentials: {0}".format(exc),
                hint="Re-join with `parley join`; the agent may have been revoked.",
            ) from exc
        except errors.PendingApproval:
            log.warning("this agent is pending approval; reads and writes stay limited until the host approves")
        except errors.ParleyError as exc:
            log.warning("hub not reachable yet (%s); starting anyway and will keep retrying", exc)

        self._since = self._load_cursor()

        if self.want_sync:
            self.sync = WorkspaceSync(
                self.client,
                self.workspace,
                poll_ms=int(policy.get("poll_ms", 2000) or 2000),
                max_blob_bytes=int(policy.get("max_blob_bytes", 26214400) or 26214400),
            )
        if self.want_pigeonhole:
            self.pigeonhole = Pigeonhole(self.client, self.workspace)
            if self.psr_from:
                candidate = Path(self.psr_from)
                if not candidate.is_absolute() and len(candidate.parts) == 1:
                    candidate = self.state_dir / candidate
                self.pigeonhole.me_path = candidate.resolve() if candidate.exists() else candidate
                log.info("reading the standing report from %s", self.pigeonhole.me_path)

        self.client.announce()
        self._initial_psr()

    def _initial_psr(self) -> None:
        """Be conforming from the first second (SPEC 6.1).

        An agent with no PSR is rendered as non-conforming on the Deck. If the
        operator supplied ``me.json`` that wins; otherwise we publish an honest
        placeholder rather than nothing.
        """
        if self.pigeonhole is not None:
            psr = self.pigeonhole.refresh_psr_from_me_json()
            if psr:
                self.client.set_last_psr(psr)
                return
        try:
            self.client.status(
                "Running the Parley daemon",
                state="idle",
                detail="Workspace sync and the pigeonhole are live; no task claimed yet.",
            )
        except errors.ParleyError as exc:
            log.debug("initial PSR not accepted yet (%s)", exc)

    def _shutdown(self) -> int:
        self._stop.set()
        # Order matters: say goodbye while the transport is still usable, then
        # close it (which unblocks the stream thread's socket read), then join.
        # Closing first would make `agent.bye` fail and leave every peer waiting
        # for a timeout that need never have happened.
        if self.client is not None:
            reason = "shutdown" if self._exit_code == EXIT_OK else "error"
            self.client.bye(reason)
            try:
                self.client.transport.close()
            except Exception:  # noqa: BLE001
                pass
        for name, thread in self._threads.items():
            thread.join(timeout=5.0)
            if thread.is_alive():
                # Every worker is a daemon thread, so one stuck in a blocking
                # socket read cannot hold the process open. Say so plainly
                # rather than warning about a situation that is already handled.
                log.info(
                    "thread %s is still blocked on I/O; it is a daemon thread and "
                    "will not delay exit", name,
                )
        if self.sync is not None:
            try:
                self.sync.save_index(force=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not persist the sync index on exit (%s)", exc)
        self._save_cursor(force=True)
        with self._failure_lock:
            failed = self._failure
        if failed is not None:
            log.error("daemon stopping because the %s thread failed: %s", failed[0], failed[1])
            return EXIT_ERROR
        log.info("parley daemon stopped cleanly")
        return self._exit_code

    # ------------------------------------------------------------------ #
    # signals
    # ------------------------------------------------------------------ #
    def _install_signals(self) -> None:
        """Install handlers for whichever signals this OS actually has.

        Windows has no ``SIGTERM`` worth the name and no ``SIGHUP``; it does
        have ``SIGBREAK``. ``signal.signal`` also refuses to run off the main
        thread, which is a legitimate way to embed the Runtime, so every failure
        here is non-fatal.
        """
        for name in ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError, RuntimeError) as exc:
                log.debug("cannot install a handler for %s (%s)", name, exc)

    def _on_signal(self, signum: int, _frame: Any) -> None:
        self._signal_count += 1
        if self._signal_count == 1:
            log.info("signal %d received; shutting down (press again to force)", signum)
            self._stop.set()
            return
        log.warning("signal %d received again; forcing exit", signum)
        # os._exit skips atexit and thread joins on purpose: we are here
        # precisely because something refused to unwind.
        os._exit(130)

    # ------------------------------------------------------------------ #
    # threads
    # ------------------------------------------------------------------ #
    def _start_threads(self) -> None:
        self._spawn("stream", self._stream_loop)
        if self.sync is not None:
            self._spawn("sync", self._sync_loop)
        self._spawn("house", self._house_loop)

    def _spawn(self, name: str, fn: Callable[[], None]) -> None:
        thread = threading.Thread(target=self._guard, args=(name, fn), name="parley-" + name)
        thread.daemon = True
        self._threads[name] = thread
        thread.start()

    def _guard(self, name: str, fn: Callable[[], None]) -> None:
        """Run a worker; never let it die silently (see the module docstring)."""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - this is the catch-all by design
            log.error("thread %s crashed: %s", name, exc)
            log.error("%s", traceback.format_exc())
            with self._failure_lock:
                if self._failure is None:
                    self._failure = (name, exc)
            self._stop.set()
        else:
            if not self._stop.is_set():
                log.warning("thread %s exited unexpectedly without an error", name)
                with self._failure_lock:
                    if self._failure is None:
                        self._failure = (name, RuntimeError("worker returned early"))
                self._stop.set()

    # -- stream -------------------------------------------------------------
    def _stream_loop(self) -> None:
        assert self.client is not None
        transport = self.client.transport
        transport.on_reconnect = self._on_reconnect
        with self._since_lock:
            since = self._since
        for event in transport.stream(since):
            if self._stop.is_set():
                break
            self._connected = True
            self._dispatch(event)
        log.debug("stream loop finished")

    def _on_reconnect(self) -> None:
        """Re-announce after a reconnect (SPEC 4.1 ``agent.hello``)."""
        if self._stop.is_set() or self.client is None:
            return
        log.info("reconnected to the hub; re-announcing")
        try:
            self.client.announce()
            psr = self.client.last_psr
            if psr:
                self.client.repeat_psr()
        except errors.ParleyError as exc:
            log.debug("re-announce failed (%s)", exc)

    def _dispatch(self, event: dict) -> None:
        """Route one event. A failure here must never break the stream."""
        etype = event.get("type")
        try:
            if self.sync is not None and isinstance(etype, str) and etype.startswith("file."):
                self.sync.apply_event(event)
        except Exception as exc:  # noqa: BLE001
            log.error("sync could not apply %s: %s", etype, exc)
            log.debug("%s", traceback.format_exc())
        try:
            if self.pigeonhole is not None:
                self.pigeonhole.on_event(event)
        except Exception as exc:  # noqa: BLE001
            log.error("pigeonhole could not mirror %s: %s", etype, exc)
            log.debug("%s", traceback.format_exc())

        if etype == "agent.revoked":
            body = event.get("body")
            if isinstance(body, dict) and body.get("agent_id") == self.client.agent_id:
                log.error("this agent was revoked by the host; stopping")
                self._exit_code = EXIT_AUTH
                self._stop.set()
        elif etype == "hub.policy":
            body = event.get("body")
            if isinstance(body, dict):
                self._apply_policy(body)

        seq = event.get("seq")
        if isinstance(seq, int):
            with self._since_lock:
                if seq > self._since:
                    self._since = seq
            self._events_since_save += 1
            self._save_cursor()

    def _apply_policy(self, body: Dict[str, Any]) -> None:
        """Honour a mid-session policy change without a restart."""
        if isinstance(body.get("heartbeat_s"), (int, float)):
            self.heartbeat_s = float(body["heartbeat_s"])
        if isinstance(body.get("psr_max_age_s"), (int, float)):
            self.psr_max_age_s = float(body["psr_max_age_s"])
        if self.sync is not None and isinstance(body.get("max_blob_bytes"), int):
            self.sync.max_blob_bytes = int(body["max_blob_bytes"])
        if self.sync is not None and isinstance(body.get("poll_ms"), int):
            self.sync.poll_ms = max(200, int(body["poll_ms"]))
        log.info("hub policy updated: heartbeat=%ss psr_max_age=%ss", self.heartbeat_s, self.psr_max_age_s)

    # -- sync ---------------------------------------------------------------
    def _sync_loop(self) -> None:
        assert self.sync is not None
        self.sync.run(self._stop)

    # -- housekeeping -------------------------------------------------------
    def _house_loop(self) -> None:
        assert self.client is not None
        last_heartbeat = 0.0
        last_psr = time.monotonic()
        while not self._stop.wait(TICK_S):
            now = time.monotonic()
            if self.pigeonhole is not None:
                self._safe("outbox", self._drain_outbox)
                self._safe("me.json", self._refresh_me)
                self._safe("state mirror", self.pigeonhole.refresh_state)
            if (now - last_heartbeat) >= self.heartbeat_s:
                last_heartbeat = now
                self._safe("heartbeat", self._heartbeat)
            if (now - last_psr) >= self.psr_max_age_s:
                last_psr = now
                self._safe("psr", self._repeat_psr)

    def _safe(self, what: str, fn: Callable[[], Any]) -> None:
        """Run a housekeeping step; log and continue on an expected failure.

        Only :class:`ParleyError` and ``OSError`` are swallowed. Anything else is
        a bug in our own code and is allowed to reach :meth:`_guard`, where it
        stops the daemon loudly instead of leaving it quietly broken.
        """
        try:
            fn()
        except errors.ParleyError as exc:
            log.debug("%s step failed (%s)", what, exc)
        except OSError as exc:
            log.warning("%s step failed (%s)", what, exc)

    def _drain_outbox(self) -> None:
        assert self.pigeonhole is not None
        for event in self.pigeonhole.drain_outbox():
            # A PSR posted through the outbox still counts for freshness.
            if event.get("type") == "status.update":
                body = event.get("body")
                if isinstance(body, dict):
                    self.client.set_last_psr(body)

    def _refresh_me(self) -> None:
        assert self.pigeonhole is not None
        psr = self.pigeonhole.refresh_psr_from_me_json()
        if psr:
            self.client.set_last_psr(psr)

    def _heartbeat(self) -> None:
        assert self.client is not None
        files = None
        total = None
        if self.sync is not None:
            index = self.sync.index
            files = len(index)
            total = sum(int(rec.get("size") or 0) for rec in index.values())
        self.client.heartbeat(workspace_files=files, workspace_bytes=total)

    def _repeat_psr(self) -> None:
        assert self.client is not None
        if self.client.repeat_psr() is None:
            # No PSR yet at all - say something true rather than stay silent and
            # be rendered as non-conforming on the Deck.
            self.client.status("Running the Parley daemon", state="idle")

    # ------------------------------------------------------------------ #
    # stream cursor
    # ------------------------------------------------------------------ #
    def _load_cursor(self) -> int:
        """Resume the stream where we left off, so a restart is not a replay.

        A lost or corrupt cursor is safe, just wasteful: we resume from 0, the
        Hub replays the log, and every consumer in this package is idempotent
        (``file.put`` compares hashes, the pigeonhole appends what it is given).
        """
        try:
            doc = loads(self._cursor_path.read_bytes())
        except FileNotFoundError:
            return 0
        except Exception:  # noqa: BLE001 - any unreadable cursor is a cold start
            log.info("stream cursor unreadable; replaying from the start of the log")
            return 0
        if isinstance(doc, dict) and isinstance(doc.get("since"), int) and doc["since"] >= 0:
            return doc["since"]
        return 0

    def _save_cursor(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force:
            if self._events_since_save < CURSOR_SAVE_EVERY and (now - self._last_cursor_save) < CURSOR_SAVE_INTERVAL_S:
                return
        self._last_cursor_save = now
        self._events_since_save = 0
        with self._since_lock:
            since = self._since
        try:
            atomic_write(self._cursor_path, dumps({"since": since}).encode("utf-8"))
        except OSError as exc:
            log.debug("could not persist the stream cursor (%s)", exc)


def _exit_code_for(exc: errors.ParleyError) -> int:
    code = getattr(exc, "code", "")
    if code in ("bad_signature", "unknown_agent", "revoked", "pending_approval"):
        return EXIT_AUTH
    if code in ("fingerprint_mismatch",):
        return 5
    if code == "transport":
        return EXIT_UNREACHABLE
    return EXIT_ERROR
