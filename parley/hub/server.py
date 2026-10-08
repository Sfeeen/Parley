"""The Hub: the one ordering authority for a parley.

Everything that mutates the log funnels through :meth:`Hub._append_locked`, which
runs under a single re-entrant mutex.  That is what makes ``seq`` strictly +1
from 1 under concurrent POSTs, and what makes conflict arbitration deterministic:
two agents putting the same path in the same millisecond are serialised, so the
second one always *sees* the first one's result and displaces it properly rather
than both writing over each other.

LOCK ORDER (outermost first).  Never acquire in the reverse direction::

    Hub._append_lock          (RLock; the whole append pipeline)
      StateView._ledger_lock  (ledger recompute)
        StateView._lock       (the materialised view)
          Store._lock         (SQLite + the seq counter)
    Hub._subs_lock            (SSE subscriber registry)   -- leaf
    Limiter._lock                                         -- leaf
    Hub._cv                                               -- leaf

The subscriber registry, the limiter and the long-poll condition are leaves: no
code holding them ever calls back into the pipeline.  SSE writer threads hold
*none* of these locks while writing to a socket, which is the whole point -- a
dead client can stall only its own thread.
"""

from __future__ import annotations

import io
import logging
import os
import queue
import re
import select
import shutil
import socket
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from .. import crypto, ids, protocol
from ..config import DEFAULTS, HubConfig, workspace_state_dir
from ..errors import BadEvent, BadPath, BadRequest, ParleyError, TooLarge
from ..exchange import HUB_AUTHORED_TYPES, Capability
from ..jsonutil import dumps, now_rfc3339, parse_rfc3339, sha256_hex
from .. import ledger as ledger_mod
from ..version import WIRE_VERSION, __version__
from . import api
from .discovery import DISCOVERY_PORT, DiscoveryResponder, local_ip_towards
from .ratelimit import Limiter
from .state import StateView
from .store import Store, normalise_blob_hash, wire_blob_hash

log = logging.getLogger("parley.hub.server")

#: SPEC §5.2 -- a resend inside this window returns the original seq.
DEDUP_WINDOW_S = 24 * 3600
#: SPEC §5.1 -- comment frame cadence, also our liveness probe for dead peers.
SSE_PING_S = 15.0
#: How long a single socket write may block before we call the peer dead.
SSE_WRITE_TIMEOUT_S = 30.0
#: Events buffered per SSE subscriber before it is declared too slow.
SSE_QUEUE_MAX = 2048
#: Concurrent SSE streams.  Each one owns a thread, so this is a thread budget.
MAX_SUBSCRIBERS = 128
#: Events replayed per DB page on SSE connect.
REPLAY_PAGE = 500
#: How often an idle SSE writer wakes to probe whether its peer is still there.
SSE_POLL_S = 1.0
#: Blob line-count results held in memory (content-addressed, so never stale).
LINE_STATS_CACHE = 8192
#: SPEC §15.6 -- 20 `request.create` per agent per minute, on top of the §12.1
#: limits.  Burst equals the rate: the spec grants no larger one, and asking 20
#: peers for something in the same second is not a shape the Exchange needs.
REQUEST_CREATE_PER_MIN = 20.0
#: Most capabilities one agent may announce at once.  A catalogue is read by
#: another model to choose from; past this it is a denial-of-attention attack as
#: much as a memory one.
MAX_ANNOUNCED_CAPABILITIES = 64

_AGT_PREFIX = re.compile(r"^agt_")


# --------------------------------------------------------------------- helpers


def _short_agent(agent_id: str) -> str:
    cleaned = _AGT_PREFIX.sub("", str(agent_id or ""))
    cleaned = re.sub(r"[^0-9a-zA-Z]", "", cleaned)
    return cleaned[:8] or "unknown"


def _short_hash(blob_hash: str) -> str:
    try:
        return normalise_blob_hash(blob_hash)[:8]
    except ParleyError:
        return "00000000"



def safe_wire_path(raw: str) -> str:
    """Validate a wire path per SPEC §7.1, then normalise it.

    SPEC §7.1 makes path rejection a Hub MUST -- "the Hub MUST reject any other
    path with 422 bad_path" -- so the Hub checks the bytes it was *sent* before
    handing them to the shared normaliser.  That ordering matters: a normaliser
    that silently repairs `a/./b` or `a\\b` would otherwise let a non-conforming
    client stay non-conforming forever, and the Hub is the only place that can
    tell it so.  The normalised result is re-checked afterwards as well, so a
    change in the normaliser can never widen what the Hub accepts.
    """
    if not isinstance(raw, str) or not raw:
        raise BadPath(
            "path is empty",
            hint="Send a workspace-relative POSIX path, e.g. 'parley/hub/server.py'.",
        )
    _assert_wire_path_shape(raw)
    path = protocol.normalise_path(raw)
    _assert_wire_path_shape(path)
    return path


def _assert_wire_path_shape(path: str) -> None:
    if "\\" in path or "\x00" in path:
        raise BadPath(
            "wire paths are POSIX-separated",
            detail={"path": path},
            hint="Convert Windows separators to '/' before sending a path.",
        )
    if path.startswith("/") or path.endswith("/"):
        raise BadPath(
            "wire paths are workspace-relative with no trailing slash",
            detail={"path": path},
            hint="Drop the leading and trailing '/'.",
        )
    if len(path) > 1 and path[1] == ":":
        raise BadPath(
            "wire paths carry no drive letter",
            detail={"path": path},
            hint="Send the path relative to the workspace root.",
        )
    for segment in path.split("/"):
        if segment in ("", ".", ".."):
            raise BadPath(
                "wire paths have no empty, '.' or '..' segment",
                detail={"path": path},
                hint="Collapse the path before sending it; the Hub will not "
                "resolve it for you.",
            )
    if len(path.encode("utf-8")) > 1024:
        raise BadPath(
            "wire path is longer than 1024 bytes",
            detail={"bytes": len(path.encode("utf-8"))},
            hint="Shorten the path or the filename.",
        )


def conflict_sidecar_path(path: str, agent_id: str, blob_hash: str) -> str:
    """``<path>.parley-conflict-<short_agent>-<short_hash>`` (SPEC §7.6).

    Deterministic by construction: the same displaced (agent, hash) pair always
    produces the same sidecar name, so two Hubs replaying the same log agree and
    a repeated conflict does not pile up near-identical files.
    """
    suffix = ".parley-conflict-%s-%s" % (_short_agent(agent_id), _short_hash(blob_hash))
    raw = path + suffix
    if len(raw.encode("utf-8")) > 1024:
        budget = 1024 - len(suffix.encode("utf-8"))
        trimmed = path.encode("utf-8")[: max(1, budget)].decode("utf-8", "ignore")
        raw = trimmed + suffix
    return safe_wire_path(raw)


def _clean(rec: dict) -> dict:
    """Drop None values -- SPEC §1.3 forbids them in canonical objects."""
    return {k: v for k, v in rec.items() if v is not None}


class _Subscriber:
    """One live SSE stream.

    The queue is bounded on purpose.  An SSE client that cannot keep up is a real
    and common failure (a laptop lid closing mid-stream); growing a buffer for it
    would trade a stalled client for a dead Hub.  Instead we mark it overflowed,
    the writer thread tells it exactly where to resume, and it reconnects with
    ``?since=`` and catches up from the database.
    """

    __slots__ = ("id", "types", "q", "overflow", "peer", "created", "label")

    def __init__(self, sub_id: int, types: "Optional[List[str]]", peer: str, label: str) -> None:
        self.id = sub_id
        self.types = types
        self.q: "queue.Queue" = queue.Queue(maxsize=SSE_QUEUE_MAX)
        self.overflow = False
        self.peer = peer
        self.label = label
        self.created = time.time()

    def matches(self, event: dict) -> bool:
        if not self.types:
            return True
        etype = str(event.get("type", ""))
        for prefix in self.types:
            if etype == prefix or etype.startswith(prefix + "."):
                return True
        return False


class _PlanContext:
    """Scratch space for one batch append.

    ``overlay`` shadows the on-disk file index so that event *n+1* of a batch sees
    the effect of event *n* during conflict arbitration, before anything has been
    committed.  Index writes are deferred because the real ``seq`` is only known
    once ``Store.append_many`` has run.
    """

    def __init__(self, hub: "Hub", now: float) -> None:
        self.hub = hub
        self.now = now
        self.plan: List[dict] = []
        self.overlay: Dict[str, Optional[dict]] = {}
        self.index_ops: List[Tuple[int, str, Optional[dict]]] = []
        self.by_id: Dict[str, int] = {}

    def add(self, event: dict) -> int:
        self.plan.append(event)
        return len(self.plan) - 1

    def current(self, path: str) -> "Optional[dict]":
        if path in self.overlay:
            return self.overlay[path]
        return self.hub.store.get_file(path)

    def put_index(self, pos: int, path: str, rec: dict) -> None:
        self.overlay[path] = rec
        self.index_ops.append((pos, path, rec))

    def del_index(self, pos: int, path: str) -> None:
        self.overlay[path] = None
        self.index_ops.append((pos, path, None))

    def flush(self, stored: List[dict]) -> None:
        for pos, path, rec in self.index_ops:
            if rec is None:
                self.hub.store.delete_file(path)
            else:
                full = dict(rec)
                full["seq"] = stored[pos]["seq"]
                full["ts"] = stored[pos]["ts"]
                self.hub.store.put_file(path, _clean(full))


# ------------------------------------------------------------------- the Hub


class Hub:
    """One parley's server process."""

    def __init__(
        self,
        state_dir: Path,
        config: HubConfig,
        *,
        workspace: "Optional[Path]" = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.workspace = Path(workspace) if workspace else None
        self.store = Store(self.state_dir)
        self.limiter = Limiter(
            {"request_create": (REQUEST_CREATE_PER_MIN, REQUEST_CREATE_PER_MIN)}
        )
        self.started_at = now_rfc3339()
        self.deck_dir = Path(__file__).resolve().parent / "deck"

        self.policy: Dict[str, Any] = dict(DEFAULTS)
        self.policy.update(dict(getattr(config, "policy", {}) or {}))

        try:
            self._root_key = bytes.fromhex(getattr(config, "root_key_hex", "") or "")
        except ValueError:
            self._root_key = b""
        #: Root keys retired by `rotate_watchword`, newest first.  Kept only so
        #: sealed-mode agents that enrolled under the old watchword can finish.
        self._previous_root_keys: List[bytes] = []

        self.view = StateView(
            self.store,
            self.policy,
            identity={
                "session": getattr(config, "session", ""),
                "name": getattr(config, "name", ""),
                "fingerprint": getattr(config, "fingerprint", ""),
                "hub_started": self.started_at,
            },
            weights=self._load_weights(),
        )

        self._append_lock = threading.RLock()
        self._subs_lock = threading.Lock()
        self._cv = threading.Condition()
        self._subs: Dict[int, _Subscriber] = {}
        self._sub_seq = 0

        self._httpd: "Optional[ThreadingHTTPServer]" = None
        self._serve_thread: "Optional[threading.Thread]" = None
        self._reaper_thread: "Optional[threading.Thread]" = None
        self._stopping = threading.Event()
        self.shutting_down = False
        self._bound_port = int(getattr(config, "port", 7777) or 7777)
        self._discovery: "Optional[DiscoveryResponder]" = None
        self._announced = False
        #: blob hash -> {"lines": n} | {"binary": True}.  The Ledger may not read
        #: blobs (compute() is pure), so the Hub measures each blob once at index
        #: time and records the result on the file entry (SPEC §9, parley.ledger).
        #: Content-addressed, so a hash's answer can never go stale.
        self._line_stats: "OrderedDict[str, dict]" = OrderedDict()

        self.view.rebuild()
        if not self.store.get_meta("enroll_opened_at"):
            self.store.set_meta("enroll_opened_at", repr(time.time()))

    # ------------------------------------------------------------- lifecycle

    def _load_weights(self) -> dict:
        if self.workspace is not None:
            try:
                return ledger_mod.load_weights(self.workspace)
            except Exception:
                log.warning("could not read .parley/ledger.json; using default weights",
                            exc_info=True)
        return dict(ledger_mod.DEFAULT_WEIGHTS)

    def start(self) -> None:
        """Bind and serve on a background thread.  Non-blocking."""
        if self._httpd is not None:
            return
        self._bind()
        assert self._httpd is not None
        self._serve_thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.5},
            name="parley-hub-http", daemon=True,
        )
        self._serve_thread.start()
        self._start_background()

    def serve_forever(self) -> None:
        """Bind and serve on this thread.  Blocking."""
        if self._httpd is None:
            self._bind()
        assert self._httpd is not None
        self._start_background()
        try:
            self._httpd.serve_forever(poll_interval=0.5)
        finally:
            self.stop()

    def _bind(self) -> None:
        bind = str(getattr(self.config, "bind", "0.0.0.0") or "0.0.0.0")
        port = int(getattr(self.config, "port", 7777) or 0)
        handler = _make_handler(self)
        server_cls = _HubHTTPServer
        if ":" in bind:
            server_cls = _HubHTTPServer6
        try:
            self._httpd = server_cls((bind, port), handler)
        except OSError as exc:
            raise ParleyError(
                "cannot bind %s:%d (%s)" % (bind, port, exc),
                hint="Another process is probably on that port. Pick another with "
                "`parley init --port N`, or stop the other Hub.",
            )
        self._httpd.hub = self  # type: ignore[attr-defined]
        self._bound_port = int(self._httpd.server_address[1])
        log.info(
            "Parley Hub '%s' listening on %s:%d  session=%s  fingerprint=%s",
            getattr(self.config, "name", ""),
            bind,
            self._bound_port,
            getattr(self.config, "session", ""),
            getattr(self.config, "fingerprint", ""),
        )

    def _start_background(self) -> None:
        if self._reaper_thread is None:
            self._reaper_thread = threading.Thread(
                target=self._reaper, name="parley-hub-reaper", daemon=True
            )
            self._reaper_thread.start()
        if not self.policy.get("public", False) and self._discovery is None:
            responder = DiscoveryResponder(self._describe_for_discovery, port=DISCOVERY_PORT)
            if responder.start():
                self._discovery = responder
        self._announce()

    def _announce(self) -> None:
        if self._announced:
            return
        self._announced = True
        self.hub_event(
            "hub.started",
            {
                "name": getattr(self.config, "name", ""),
                "url": self.url,
                "version": __version__,
                "wire": WIRE_VERSION,
                "fingerprint": getattr(self.config, "fingerprint", ""),
                "crypto_backend": getattr(crypto, "BACKEND", "unknown"),
            },
        )
        self.hub_event("hub.policy", {"policy": self.wire_policy()})

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        self.shutting_down = True
        self._stopping.set()
        if self._discovery is not None:
            self._discovery.stop()
            self._discovery = None
        # Wake every SSE writer so it can close its stream cleanly instead of
        # sitting on a 15 s ping timeout while the process tries to exit.
        with self._subs_lock:
            subs = list(self._subs.values())
        for sub in subs:
            try:
                sub.q.put_nowait(None)
            except queue.Full:
                sub.overflow = True
        with self._cv:
            self._cv.notify_all()
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                log.debug("httpd shutdown raised", exc_info=True)
            try:
                httpd.server_close()
            except Exception:
                pass
        thread, self._serve_thread = self._serve_thread, None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self.store.close()
        log.info("Parley Hub stopped")

    def request_shutdown(self) -> None:
        """Mark the Hub as shutting down and stop it from another thread."""
        self.shutting_down = True
        threading.Thread(target=self.stop, name="parley-hub-stop", daemon=True).start()

    # ------------------------------------------------------------------- urls

    @property
    def port(self) -> int:
        return self._bound_port

    @property
    def url(self) -> str:
        bind = str(getattr(self.config, "bind", "0.0.0.0") or "0.0.0.0")
        if bind in ("", "0.0.0.0", "::", "[::]"):
            host = local_ip_towards("")
        else:
            host = bind
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        return "http://%s:%d" % (host, self._bound_port)

    def deck_url(self, *, with_viewer_token: bool = True) -> str:
        if not with_viewer_token:
            return self.url + "/"
        token = self.mint_viewer_token(
            ttl=float(self.policy.get("viewer_token_ttl_s", api.DEFAULT_VIEWER_TTL_S)),
            label="deck",
        )
        return self.url + "/?vt=" + token

    def _describe_for_discovery(self, peer: str) -> dict:
        host = local_ip_towards(peer)
        bind = str(getattr(self.config, "bind", "0.0.0.0") or "0.0.0.0")
        if bind not in ("", "0.0.0.0", "::", "[::]"):
            host = bind
        return {
            "session": getattr(self.config, "session", ""),
            "name": getattr(self.config, "name", ""),
            "url": "http://%s:%d" % (host, self._bound_port),
            "fingerprint": getattr(self.config, "fingerprint", ""),
        }

    # ---------------------------------------------------------------- policy

    def wire_policy(self) -> dict:
        """The subset of policy clients are told about (SPEC §3.4)."""
        return {
            "heartbeat_s": int(self.policy.get("heartbeat_s", 15)),
            "psr_max_age_s": int(self.policy.get("psr_max_age_s", 30)),
            "max_blob_bytes": int(self.policy.get("max_blob_bytes", 26214400)),
            "sealed": bool(self.policy.get("sealed", False)),
            "poll_ms": int(self.policy.get("poll_ms", 2000)),
            "max_agents": int(self.policy.get("max_agents", 16)),
            "require_approval": bool(self.policy.get("require_approval", False)),
        }

    def root_keys(self) -> List[bytes]:
        """The current root key first, then recently rotated ones.

        Sealed mode (§3.6) derives its key from the root key, which §3.8 rotation
        changes -- so a rotation would otherwise cut off every sealed agent that
        is already enrolled and does not know the new watchword.  Keeping the
        last few lets them finish their session.
        """
        keys = [k for k in [self._root_key] + self._previous_root_keys if k]
        return keys

    def seal_keys(self) -> List[bytes]:
        out: List[bytes] = []
        for rk in self.root_keys():
            try:
                out.append(crypto.seal_key(rk))
            except Exception:  # pragma: no cover - defensive
                log.debug("could not derive a seal key", exc_info=True)
        return out

    def note_blob_lines(self, blob_hash: str, data: bytes) -> dict:
        """Measure a blob's line count once, while its bytes are already in hand."""
        try:
            counted = ledger_mod.count_lines(data)
        except Exception:  # noqa: BLE001 - never let the Ledger break an upload
            log.debug("count_lines failed for %s", blob_hash[:19], exc_info=True)
            return {}
        rec = {"binary": True} if counted is None else {"lines": int(counted)}
        with self._append_lock:
            self._line_stats[blob_hash] = rec
            self._line_stats.move_to_end(blob_hash)
            while len(self._line_stats) > LINE_STATS_CACHE:
                self._line_stats.popitem(last=False)
        return dict(rec)

    def _line_stats_for(self, blob_hash: str) -> dict:
        """``{"lines": n}`` or ``{"binary": True}``; ``{}`` when the blob is absent."""
        cached = self._line_stats.get(blob_hash)
        if cached is not None:
            self._line_stats.move_to_end(blob_hash)
            return dict(cached)
        if not self.store.has_blob(blob_hash):
            return {}
        try:
            data = self.store.read_blob(blob_hash)
        except ParleyError:
            return {}
        return self.note_blob_lines(blob_hash, data)


    def enroll_uses(self) -> int:
        try:
            return int(self.store.get_meta("enroll_uses", "0") or 0)
        except ValueError:
            return 0

    def enroll_opened_at(self) -> float:
        try:
            return float(self.store.get_meta("enroll_opened_at", "0") or 0.0)
        except ValueError:
            return 0.0

    def note_enrolment(self) -> None:
        self.store.bump_meta_counter("enroll_uses", 1)

    def mint_viewer_token(self, *, ttl: float = api.DEFAULT_VIEWER_TTL_S, label: str = "") -> str:
        token = ids.new_viewer_token()
        self.store.put_viewer_token(token, time.time() + float(ttl), label)
        return token

    def rotate_watchword(self, *, words: int = 5) -> Tuple[str, str]:
        """New watchword + new enrolment key.  Existing agent keys are untouched.

        The verbal fingerprint derives from the root key, so it necessarily
        changes.  We announce the change in the log (signed, ordered) so clients
        can tell a legitimate rotation from the relay attack §3.5 guards against.
        """
        new_watchword = crypto.generate_watchword(words)
        normalised = crypto.normalise_watchword(new_watchword)
        iterations = int(getattr(self.config, "pbkdf2_iterations", 200_000) or 200_000)
        session = getattr(self.config, "session", "")
        root_key = crypto.derive_root_key(normalised, session, iterations=iterations)
        previous_fp = getattr(self.config, "fingerprint", "")
        new_fp = crypto.fingerprint(root_key)

        if self._root_key and self._root_key != root_key:
            self._previous_root_keys.insert(0, self._root_key)
            del self._previous_root_keys[3:]
        self._root_key = root_key
        self.config.root_key_hex = root_key.hex()
        self.config.watchword_hash = sha256_hex(normalised.encode("utf-8"))
        self.config.fingerprint = new_fp
        try:
            self.config.save(self.state_dir)
        except Exception:
            log.exception("could not persist the rotated watchword to %s", self.state_dir)
        self.view.identity["fingerprint"] = new_fp
        self.store.set_meta("enroll_opened_at", repr(time.time()))
        self.store.set_meta("enroll_uses", "0")

        self.hub_event(
            "hub.notice",
            {
                "text": "The watchword was rotated. The verbal fingerprint is now "
                + new_fp
                + " (was " + previous_fp + "). Existing agents keep working.",
                "level": "warn",
                "fingerprint": new_fp,
                "previous_fingerprint": previous_fp,
            },
        )
        log.info("watchword rotated; fingerprint %s -> %s", previous_fp, new_fp)
        return new_watchword, new_fp

    # ------------------------------------------------------------ append path

    def append(self, raw_events: List[dict], *, agent: dict) -> Tuple[List[dict], List[dict]]:
        """Append events authored by an authenticated agent."""
        agent_id = str(agent.get("agent_id") or "")
        key = self.store.agent_key(agent_id) or b""
        with self._append_lock:
            return self._append_locked(raw_events, actor=agent_id, key=key, hub_authored=False)

    def hub_event(
        self,
        etype: str,
        body: dict,
        *,
        actor: "Optional[str]" = None,
        actor_key: "Optional[bytes]" = None,
    ) -> "Optional[dict]":
        """Append a Hub-authored event.  ``actor`` defaults to the literal "hub"."""
        who = actor or "hub"
        key = actor_key if actor_key is not None else self._root_key
        ev = protocol.make_event(who, getattr(self.config, "session", ""), etype, dict(body))
        with self._append_lock:
            _results, stored = self._append_locked(
                [ev], actor=who, key=key, hub_authored=True
            )
        return stored[0] if stored else None

    def submit(self, event: dict, *, actor_key: "Optional[bytes]" = None) -> dict:
        """In-process append, bypassing HTTP.  Used by the host's own CLI."""
        ev = dict(event)
        actor = str(ev.get("actor") or "hub")
        key = actor_key
        if key is None:
            key = self._root_key if actor == "hub" else (self.store.agent_key(actor) or b"")
        with self._append_lock:
            results, stored = self._append_locked(
                [ev], actor=actor, key=key, hub_authored=True
            )
        if stored:
            return stored[0]
        seq = results[0].get("seq") if results else None
        return self.store.get_event(int(seq)) or {} if seq else {}

    def _append_locked(
        self,
        raw_events: Iterable[dict],
        *,
        actor: str,
        key: bytes,
        hub_authored: bool,
    ) -> Tuple[List[dict], List[dict]]:
        now = time.time()
        ctx = _PlanContext(self, now)
        meta: List[Dict[str, Any]] = []

        for raw in raw_events:
            if not isinstance(raw, dict):
                raise BadEvent("each event must be a JSON object", hint="See SPEC §2.")
            entry = self._ingest(raw, actor, key, ctx, hub_authored, now)
            meta.append(entry)

        if not ctx.plan:
            return [self._result(m, []) for m in meta], []

        stored = self.store.append_many(ctx.plan)
        ctx.flush(stored)
        for ev in stored:
            self.view.apply(ev)
        self._fanout(stored)
        self._notify_waiters()

        results = [self._result(m, stored) for m in meta]
        self._resolve_due_decisions(now)
        self._drain_taken()
        return results, stored

    @staticmethod
    def _result(entry: Dict[str, Any], stored: List[dict]) -> dict:
        out: Dict[str, Any] = {"id": entry["id"], "duplicate": bool(entry["duplicate"])}
        if entry.get("seq") is not None:
            out["seq"] = int(entry["seq"])
        elif entry.get("pos") is not None and stored:
            ev = stored[int(entry["pos"])]
            out["seq"] = int(ev["seq"])
            body = ev.get("body") or {}
            if isinstance(body, dict) and body.get("_rejected"):
                out["rejected"] = True
                out["reason"] = body.get("_reason", "rejected")
        return out

    # ------------------------------------------------------ per-event ingest

    def _ingest(
        self,
        raw: dict,
        actor: str,
        key: bytes,
        ctx: _PlanContext,
        hub_authored: bool,
        now: float,
    ) -> Dict[str, Any]:
        session = getattr(self.config, "session", "")
        ev_actor = str(raw.get("actor") or actor)
        if not hub_authored and ev_actor != actor:
            raise BadEvent(
                "event actor does not match the authenticated agent",
                detail={"actor": ev_actor, "authenticated": actor},
                hint="Set `actor` to your own agent id, or omit it and the Hub will fill it in.",
            )
        ev_session = raw.get("session")
        if ev_session and ev_session != session:
            raise BadEvent(
                "event session does not match this parley",
                detail={"session": str(ev_session)},
                hint="Use the session id the Hub returned at enrolment.",
            )

        event_id = raw.get("id")
        if not isinstance(event_id, str) or not event_id:
            event_id = ids.new_event_id()

        # --- dedup (SPEC §5.2) ------------------------------------------------
        dedup_key = ev_actor + "\x00" + event_id
        if dedup_key in ctx.by_id:
            return {"id": event_id, "pos": ctx.by_id[dedup_key], "seq": None, "duplicate": True}
        previous = self.store.find_by_author_id_within(ev_actor, event_id, DEDUP_WINDOW_S)
        if previous is not None:
            log.debug("dedup hit actor=%s id=%s -> seq=%s", ev_actor, event_id, previous.get("seq"))
            return {
                "id": event_id,
                "pos": None,
                "seq": int(previous.get("seq") or 0),
                "duplicate": True,
            }

        # --- signature (verified against the bytes the author actually signed) --
        supplied_sig = raw.get("sig")
        if isinstance(supplied_sig, str) and supplied_sig and key:
            probe = {k: v for k, v in raw.items() if k != "seq"}
            if not crypto.verify_event(key, probe):
                raise BadEvent(
                    "event signature does not verify",
                    detail={"id": event_id},
                    hint="Sign canonical JSON of the event with `seq` and `sig` removed "
                    "(SPEC §2), keyed by your agent key.",
                )

        ev: Dict[str, Any] = dict(raw)
        ev.pop("seq", None)
        ev.pop("sig", None)
        ev["v"] = WIRE_VERSION
        ev["session"] = session
        ev["actor"] = ev_actor
        ev["id"] = event_id

        etype = ev.get("type")
        if not isinstance(etype, str) or not etype:
            raise BadEvent("event has no type", hint="See SPEC §4 for the type table.")
        body = ev.get("body")
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise BadEvent(
                "event body must be an object",
                detail={"type": etype},
                hint="Every PARLEY/1 body is a JSON object, even when empty.",
            )
        body = dict(body)
        ev["body"] = body

        # --- timestamp / clock skew (SPEC §2) --------------------------------
        skew_s = float(self.policy.get("skew_s", 300) or 300)
        ts = ev.get("ts")
        if not isinstance(ts, str) or not ts:
            ev["ts"] = now_rfc3339()
        else:
            try:
                authored = parse_rfc3339(ts)
            except (ValueError, TypeError):
                body["_clock_skew_corrected"] = True
                body["_original_ts"] = ts
                ev["ts"] = now_rfc3339()
            else:
                if abs(authored - now) > skew_s:
                    body["_clock_skew_corrected"] = True
                    # Keeping the original makes the correction auditable (R6) and
                    # lets a verifier reconstruct what the author signed.
                    body["_original_ts"] = ts
                    ev["ts"] = now_rfc3339()

        # --- paths --------------------------------------------------------------
        # Canonicalise wire paths before the generic schema check, so a bad path
        # answers `422 bad_path` as SPEC §7.1 requires rather than being folded
        # into a generic `bad_event` problem list.
        self._normalise_event_paths(etype, body)

        # --- size + schema ----------------------------------------------------
        body_bytes = len(dumps(body).encode("utf-8"))
        max_body = int(getattr(protocol, "MAX_BODY_BYTES", 256 * 1024))
        if body_bytes > max_body:
            raise BadEvent(
                "event body is too large",
                detail={"bytes": body_bytes, "max": max_body},
                hint="Put large content in a blob (POST /v1/blobs) and reference its hash.",
            )
        probe = dict(ev)
        probe["seq"] = self.store.head_seq() + 1
        problems = protocol.validate_event(probe, strict=True)
        if problems:
            raise BadEvent(
                "event failed validation",
                detail={"type": etype, "problems": list(problems)[:8]},
                hint="SPEC §2 and §4 define the shape; unknown types must live under `x.`.",
            )

        # --- the Exchange (SPEC §15) -----------------------------------------
        self._check_exchange_event(etype, body, hub_authored)

        # --- type-specific planning (conflicts, locks) ------------------------
        pos = self._plan(ev, ev_actor, ctx)

        # --- sign (the Hub holds the key either way, so the log stays verifiable)
        if key:
            ctx.plan[pos]["sig"] = crypto.sign_event(key, ctx.plan[pos])

        ctx.by_id[dedup_key] = pos
        return {"id": event_id, "pos": pos, "seq": None, "duplicate": False}

    @staticmethod
    def _check_exchange_event(etype: str, body: dict, hub_authored: bool) -> None:
        """Validate the Exchange events the Hub must not take on trust (SPEC §15).

        Two things are checked, and both of them are things the Hub is the only
        party able to check.

        ``capability.announce`` is validated entry by entry with
        :meth:`parley.exchange.Capability.validate`.  The registry would quietly
        drop a malformed capability and carry on -- correct for a *reader* of the
        log, wrong for the ingest point, because the announcing agent would then
        believe it had lent something it had not.  A provider that publishes a
        broken catalogue finds out here, with the problems named, instead of when
        somebody tries to call it.

        ``request.taken`` and ``request.expired`` are Hub-authored.  An agent that
        could forge ``request.expired`` could charge a rival the SPEC §9
        abandonment penalty -- the Ledger's only negative term -- for work the
        rival never dropped.
        """
        if not hub_authored and etype in HUB_AUTHORED_TYPES:
            raise BadEvent(
                "%s is authored by the Hub, not by an agent" % etype,
                detail={"type": etype},
                hint="The Hub emits request.taken and request.expired itself from the "
                "request state machine (SPEC §15.3). A provider ends a request with "
                "request.result or request.decline.",
            )
        if etype != "capability.announce":
            return
        caps = body.get("capabilities")
        if not isinstance(caps, list):
            raise BadEvent(
                "capability.announce needs a `capabilities` list",
                hint='Send {"capabilities": [ {...}, ... ]}. An empty list is how an '
                "agent withdraws its whole catalogue (announce is total, SPEC §15.1).",
            )
        if len(caps) > MAX_ANNOUNCED_CAPABILITIES:
            raise TooLarge(
                "too many capabilities in one announcement",
                detail={"count": len(caps), "max": MAX_ANNOUNCED_CAPABILITIES},
                hint="Announce the %d another agent would plausibly choose between."
                % MAX_ANNOUNCED_CAPABILITIES,
            )
        problems: List[str] = []
        seen: Dict[str, int] = {}
        for index, raw in enumerate(caps):
            if not isinstance(raw, dict):
                problems.append("capabilities[%d] is not an object" % index)
                continue
            cap = Capability.from_dict(raw)
            for problem in cap.validate():
                problems.append("capabilities[%d]: %s" % (index, problem))
            if cap.name:
                if cap.name in seen:
                    problems.append(
                        "capabilities[%d]: %r was already announced at index %d; "
                        "`name` is unique per agent (SPEC §15.1)"
                        % (index, cap.name, seen[cap.name])
                    )
                seen[cap.name] = index
        if problems:
            raise BadEvent(
                "capability.announce is not well-formed",
                detail={"problems": problems[:8], "count": len(problems)},
                hint="SPEC §15.1 has the field table. `description` is the one that "
                "decides whether anybody uses the capability, and `safety` is the one "
                "it is worst to get wrong -- an unrecognised value is treated as "
                "dangerous. Nothing in this announcement was registered.",
            )

    @staticmethod
    def _normalise_event_paths(etype: str, body: dict) -> None:
        """Normalise every wire path in a body in place; raises BadPath (SPEC §7.1)."""
        if etype in ("file.put", "file.delete"):
            if body.get("path") is not None:
                body["path"] = safe_wire_path(str(body["path"]))
        elif etype == "file.move":
            for key in ("from", "to"):
                if body.get(key) is not None:
                    body[key] = safe_wire_path(str(body[key]))
        elif etype in ("lock.acquire", "lock.release"):
            paths = body.get("paths")
            if isinstance(paths, list):
                body["paths"] = [safe_wire_path(str(p)) for p in paths[:256]]
        elif etype == "status.update":
            focus = body.get("focus")
            if isinstance(focus, list):
                body["focus"] = [safe_wire_path(str(p)) for p in focus[:8]]

    # ------------------------------------------------- conflict arbitration

    def _plan(self, ev: dict, actor: str, ctx: _PlanContext) -> int:
        etype = str(ev.get("type"))
        if etype == "file.put":
            return self._plan_put(ev, actor, ctx)
        if etype == "file.delete":
            return self._plan_delete(ev, actor, ctx)
        if etype == "file.move":
            return self._plan_move(ev, actor, ctx)
        if etype == "lock.acquire":
            return self._plan_lock(ev, actor, ctx)
        return ctx.add(ev)

    def _make_hub_event(self, etype: str, body: dict) -> dict:
        """Build a Hub-authored event (sidecars, conflicts, denials, notices).

        These bypass `_ingest` -- they are generated inside the planner, after
        validation has already run on the agent's event -- so they are checked
        here instead.  A failure is a Hub bug, not a client error: we log it
        loudly but still append, because losing a conflict record would violate
        R5 far more seriously than a schema nit.
        """
        ev = protocol.make_event("hub", getattr(self.config, "session", ""), etype, dict(body))
        ev["v"] = WIRE_VERSION
        probe = dict(ev)
        probe["seq"] = self.store.head_seq() + 1
        problems = protocol.validate_event(probe, strict=True)
        if problems:
            log.error("Hub-authored %s failed validation (appending anyway): %s",
                      etype, "; ".join(problems[:4]))
        if self._root_key:
            ev["sig"] = crypto.sign_event(self._root_key, ev)
        return ev

    def _flag_lock_violation(self, body: dict, paths: List[str], actor: str, now: float) -> None:
        # SPEC §4.5: a lock is a social signal. The write is always accepted (R5)
        # but it is flagged so the Deck can show it and the holder can react.
        if self.view.locks_held_by_others(paths, actor, now):
            body["lock_violation"] = True

    def _notice_missing_blob(
        self, ctx: _PlanContext, missing: bool, path: str, blob_hash: str, actor: str
    ) -> None:
        if not missing:
            return
        log.warning("file.put for %s references blob %s which the Hub does not hold",
                    path, blob_hash[:19])
        ctx.add(
            self._make_hub_event(
                "hub.notice",
                {
                    "text": "%s is indexed at %s but its blob has not been uploaded; "
                    "peers cannot fetch it until %s runs POST /v1/blobs."
                    % (path, blob_hash[:19] + "...", actor),
                    "level": "warn",
                    "path": path,
                    "hash": blob_hash,
                },
            )
        )

    def _plan_put(self, ev: dict, actor: str, ctx: _PlanContext) -> int:
        body = ev["body"]
        path = safe_wire_path(str(body.get("path") or ""))
        body["path"] = path
        raw_hash = body.get("hash")
        if not raw_hash:
            raise BadEvent(
                "file.put needs a `hash`",
                detail={"path": path},
                hint="Upload the bytes with POST /v1/blobs first, then cite the returned hash.",
            )
        new_hash = wire_blob_hash(str(raw_hash))
        body["hash"] = new_hash
        size = int(body.get("size") or 0)
        base = body.get("base")
        base_hash = wire_blob_hash(str(base)) if base else None
        if base_hash:
            body["base"] = base_hash
        self._flag_lock_violation(body, [path], actor, ctx.now)

        current = ctx.current(path)
        if current is None:
            clean = base_hash is None
        else:
            cur_hash = str(current.get("hash") or "")
            clean = (base_hash is not None and base_hash == cur_hash) or cur_hash == new_hash

        new_rec = _clean(
            {
                "path": path,
                "hash": new_hash,
                "size": size,
                "mode": body.get("mode"),
                "author": actor,
            }
        )
        new_rec.update(self._line_stats_for(new_hash))

        # SPEC §7.4 says upload the blob first.  We accept the put either way --
        # rejecting would lose the event, and the blob may still arrive -- but we
        # say so out loud, because a silently dangling index entry is exactly the
        # kind of thing R6 forbids.
        missing_blob = not self.store.has_blob(new_hash)

        if clean or current is None:
            pos = ctx.add(ev)
            ctx.put_index(pos, path, new_rec)
            self._notice_missing_blob(ctx, missing_blob, path, new_hash, actor)
            if not clean:
                # `base` named a version this Hub no longer has: the file was
                # deleted here.  Resurrecting it loses nothing (deletion loses
                # ties, SPEC §7.7), so this is a notice, not a conflict -- a
                # file.conflict with no displaced side would be meaningless.
                ctx.add(
                    self._make_hub_event(
                        "hub.notice",
                        {
                            "text": "%s re-created %s from a base this Hub no longer holds; "
                            "accepted (deletion loses ties)." % (actor, path),
                            "level": "info",
                            "path": path,
                        },
                    )
                )
            return pos

        # --- divergence (SPEC §7.6): never lose a byte ------------------------
        cur_hash = str(current.get("hash") or "")
        cur_author = str(current.get("author") or "")
        sidecar = conflict_sidecar_path(path, cur_author, cur_hash)
        side_ev = self._make_hub_event(
            "file.put",
            _clean(
                {
                    "path": sidecar,
                    "hash": cur_hash,
                    "size": int(current.get("size") or 0),
                    "mode": current.get("mode"),
                    "conflict_of": path,
                    "preserved_from": cur_author,
                }
            ),
        )
        # Emitted BEFORE the overwrite so the displaced content is never
        # unreferenced at any point in the log.
        side_pos = ctx.add(side_ev)
        ctx.put_index(
            side_pos,
            sidecar,
            _clean(
                {
                    "path": sidecar,
                    "hash": cur_hash,
                    "size": int(current.get("size") or 0),
                    "mode": current.get("mode"),
                    "author": cur_author,
                    "conflict_of": path,
                    "lines": current.get("lines"),
                    "binary": current.get("binary"),
                }
            ),
        )
        pos = ctx.add(ev)
        ctx.put_index(pos, path, new_rec)
        self._notice_missing_blob(ctx, missing_blob, path, new_hash, actor)
        ctx.add(
            self._make_hub_event(
                "file.conflict",
                {
                    "path": path,
                    # `ours` is the version this Hub already held and has now
                    # displaced; `theirs` is the arriving version that wins the
                    # path.  `kept_as` is always where `ours` ended up -- which is
                    # what makes §7.6 and §7.7 read consistently.
                    "ours": {"hash": cur_hash, "agent": cur_author},
                    "theirs": {"hash": new_hash, "agent": actor},
                    "kept_as": sidecar,
                },
            )
        )
        log.info("conflict at %s: %s displaced to %s", path, cur_hash[:19], sidecar)
        return pos

    def _plan_delete(self, ev: dict, actor: str, ctx: _PlanContext) -> int:
        body = ev["body"]
        path = safe_wire_path(str(body.get("path") or ""))
        body["path"] = path
        base = body.get("base")
        base_hash = wire_blob_hash(str(base)) if base else None
        if base_hash:
            body["base"] = base_hash
        self._flag_lock_violation(body, [path], actor, ctx.now)

        current = ctx.current(path)
        if current is None:
            # Already gone.  Idempotent, nothing to arbitrate.
            pos = ctx.add(ev)
            ctx.del_index(pos, path)
            return pos

        cur_hash = str(current.get("hash") or "")
        if base_hash is not None and base_hash == cur_hash:
            pos = ctx.add(ev)
            ctx.del_index(pos, path)
            return pos

        # SPEC §7.7: deletion loses ties.  The file stays; the delete is recorded
        # but flagged so a replaying client does not act on it.
        body["_rejected"] = True
        body["_reason"] = "delete_lost_tie"
        theirs: Dict[str, Any] = {"agent": actor, "deleted": True}
        if base_hash:
            theirs["hash"] = base_hash
        pos = ctx.add(ev)
        ctx.add(
            self._make_hub_event(
                "file.conflict",
                {
                    "path": path,
                    "ours": {"hash": cur_hash, "agent": str(current.get("author") or "")},
                    "theirs": theirs,
                    "kept_as": path,
                },
            )
        )
        log.info("delete of %s by %s rejected: base mismatch, file kept", path, actor)
        return pos

    def _plan_move(self, ev: dict, actor: str, ctx: _PlanContext) -> int:
        body = ev["body"]
        src = safe_wire_path(str(body.get("from") or ""))
        dst = safe_wire_path(str(body.get("to") or ""))
        body["from"] = src
        body["to"] = dst
        cur_src = ctx.current(src)
        raw_hash = body.get("hash") or (cur_src or {}).get("hash")
        if not raw_hash:
            raise BadEvent(
                "file.move needs a `hash` when the source is unknown to the Hub",
                detail={"from": src, "to": dst},
                hint="Send the sha256 of the content being moved.",
            )
        moved_hash = wire_blob_hash(str(raw_hash))
        body["hash"] = moved_hash
        size = int(body.get("size") or (cur_src or {}).get("size") or 0)
        self._flag_lock_violation(body, [src, dst], actor, ctx.now)

        cur_dst = ctx.current(dst)
        if cur_dst is not None and str(cur_dst.get("hash") or "") != moved_hash:
            dst_hash = str(cur_dst.get("hash") or "")
            dst_author = str(cur_dst.get("author") or "")
            sidecar = conflict_sidecar_path(dst, dst_author, dst_hash)
            side_pos = ctx.add(
                self._make_hub_event(
                    "file.put",
                    _clean(
                        {
                            "path": sidecar,
                            "hash": dst_hash,
                            "size": int(cur_dst.get("size") or 0),
                            "mode": cur_dst.get("mode"),
                            "conflict_of": dst,
                            "preserved_from": dst_author,
                        }
                    ),
                )
            )
            ctx.put_index(
                side_pos,
                sidecar,
                _clean(
                    {
                        "path": sidecar,
                        "hash": dst_hash,
                        "size": int(cur_dst.get("size") or 0),
                        "author": dst_author,
                        "conflict_of": dst,
                        "lines": cur_dst.get("lines"),
                        "binary": cur_dst.get("binary"),
                    }
                ),
            )
            pos = ctx.add(ev)
            moved_rec = _clean({"path": dst, "hash": moved_hash, "size": size, "author": actor})
            moved_rec.update(self._line_stats_for(moved_hash))
            ctx.put_index(pos, dst, moved_rec)
            if cur_src is not None:
                ctx.del_index(pos, src)
            ctx.add(
                self._make_hub_event(
                    "file.conflict",
                    {
                        "path": dst,
                        "ours": {"hash": dst_hash, "agent": dst_author},
                        "theirs": {"hash": moved_hash, "agent": actor},
                        "kept_as": sidecar,
                    },
                )
            )
            return pos

        pos = ctx.add(ev)
        moved_rec = _clean({"path": dst, "hash": moved_hash, "size": size, "author": actor})
        moved_rec.update(self._line_stats_for(moved_hash))
        ctx.put_index(pos, dst, moved_rec)
        if cur_src is not None:
            ctx.del_index(pos, src)
        return pos

    def _plan_lock(self, ev: dict, actor: str, ctx: _PlanContext) -> int:
        body = ev["body"]
        raw_paths = body.get("paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            raise BadEvent(
                "lock.acquire needs a non-empty `paths` list",
                hint='Send {"paths": ["parley/hub/server.py"], "intent": "rewriting SSE"}.',
            )
        paths = [safe_wire_path(str(p)) for p in raw_paths[:256]]
        body["paths"] = paths
        held = self.view.locks_held_by_others(paths, actor, ctx.now)
        body["_granted"] = [p for p in paths if p not in held]
        pos = ctx.add(ev)
        if held:
            by_holder: Dict[str, List[str]] = {}
            for p, who in held.items():
                by_holder.setdefault(who, []).append(p)
            for who in sorted(by_holder):
                ctx.add(
                    self._make_hub_event(
                        "lock.denied", {"paths": sorted(by_holder[who]), "held_by": who}
                    )
                )
        return pos

    def _resolve_due_decisions(self, now: float) -> None:
        """Hub-author `decision.resolve` once quorum is met (SPEC §4.8)."""
        try:
            due = self.view.due_decisions(now)
        except Exception:
            log.exception("decision resolution check failed")
            return
        for did, option, tally in due:
            try:
                self.hub_event("decision.resolve", {"id": did, "option": option, "tally": tally})
            except ParleyError:
                log.exception("could not resolve decision %s", did)

    # -------------------------------------------------------------- exchange

    def _drain_taken(self) -> List[dict]:
        """Hub-author `request.taken` for the losers of a `to: "any"` race (§15.3).

        Driven straight off the append path rather than off the reaper: the race
        is decided by an ``request.accept`` that has just been appended, and an
        agent still deliberating on work somebody else already took is doing
        wasted work for however long it takes to tell it.  ``_append_lock`` is
        re-entrant, so the nested append this makes is the same serialised path
        as any other -- and the ``request.taken`` it appends drains nothing
        further, so the recursion is one level deep.
        """
        out: List[dict] = []
        try:
            owed = self.view.due_taken()
        except Exception:
            log.exception("could not read the pending request.taken events")
            return out
        for partial in owed:
            try:
                stored = self.hub_event("request.taken", dict(partial.get("body") or {}))
            except ParleyError:
                log.exception("could not emit request.taken for %s",
                              (partial.get("body") or {}).get("id"))
                continue
            if stored:
                out.append(stored)
        return out

    def sweep_exchange(self, now: "Optional[float]" = None) -> List[dict]:
        """Hub-author every `request.expired` that is due (SPEC §15.3).

        Called from the reaper tick, and directly by the tests and by `parley
        doctor`, so expiry never needs a thread of its own.  SPEC §15.3 calls
        dropping an accepted request the one unforgivable Exchange behaviour and
        §9 makes this event the *sole* trigger for the Ledger's abandonment
        penalty: an expiry that never fires is a provider never held to what it
        accepted, so this runs whether or not anybody is appending.
        """
        when = time.time() if now is None else float(now)
        out: List[dict] = []
        try:
            due = self.view.due_expiries(when)
        except Exception:
            log.exception("could not read the due request expiries")
            return out
        for partial in due:
            body = dict(partial.get("body") or {})
            try:
                stored = self.hub_event("request.expired", body)
            except ParleyError:
                log.exception("could not expire request %s", body.get("id"))
                continue
            if stored:
                log.info(
                    "request %s expired after %ss (%s)",
                    body.get("id"), body.get("timeout_s"),
                    "accepted but never answered" if body.get("abandoned")
                    else "nobody answered",
                )
                out.append(stored)
        out.extend(self._drain_taken())
        return out

    # --------------------------------------------------------------- reading

    def read_events(
        self,
        *,
        since: int = 0,
        limit: int = 1000,
        types: "Optional[List[str]]" = None,
        wait: float = 0.0,
    ) -> List[dict]:
        """Long-poll read (SPEC §5): returns as soon as anything matches."""
        events = self.store.read(since, limit, types)
        if events or wait <= 0:
            return events
        deadline = time.time() + min(wait, api.MAX_WAIT_S)
        while not self._stopping.is_set():
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            with self._cv:
                self._cv.wait(timeout=min(remaining, 1.0))
            events = self.store.read(since, limit, types)
            if events:
                return events
        return self.store.read(since, limit, types)

    def _notify_waiters(self) -> None:
        with self._cv:
            self._cv.notify_all()

    # ------------------------------------------------------- SSE subscribers

    def subscribe(
        self, *, types: "Optional[List[str]]" = None, peer: str = "", label: str = ""
    ) -> Tuple[_Subscriber, int]:
        """Register a live subscriber and capture the replay boundary atomically.

        Registering *before* reading history is what makes the SSE stream
        gap-free: anything appended from this instant lands in the queue, and the
        writer drops whatever it already replayed by comparing seq.
        """
        with self._append_lock:
            head = self.store.head_seq()
            with self._subs_lock:
                if len(self._subs) >= MAX_SUBSCRIBERS:
                    raise TooLarge(
                        "too many live streams on this Hub",
                        detail={"max": MAX_SUBSCRIBERS},
                        hint="Close an idle Deck tab, or fall back to GET /v1/events?wait=.",
                    )
                self._sub_seq += 1
                sub = _Subscriber(self._sub_seq, types, peer, label)
                self._subs[sub.id] = sub
            return sub, head

    def unsubscribe(self, sub: _Subscriber) -> None:
        with self._subs_lock:
            self._subs.pop(sub.id, None)

    def subscriber_count(self) -> int:
        with self._subs_lock:
            return len(self._subs)

    def _fanout(self, events: List[dict]) -> None:
        """Push appended events to every live subscriber.  Never blocks."""
        with self._subs_lock:
            subs = list(self._subs.values())
        for sub in subs:
            if sub.overflow:
                continue
            for ev in events:
                if not sub.matches(ev):
                    continue
                try:
                    sub.q.put_nowait(ev)
                except queue.Full:
                    sub.overflow = True
                    log.warning(
                        "SSE subscriber #%d (%s%s) fell %d events behind and was dropped; "
                        "it must reconnect with ?since=",
                        sub.id,
                        sub.peer,
                        (" " + sub.label) if sub.label else "",
                        SSE_QUEUE_MAX,
                    )
                    break

    # --------------------------------------------------------------- reaper

    def _reaper(self) -> None:
        tick = max(1.0, min(float(self.policy.get("heartbeat_s", 15) or 15), 5.0))
        while not self._stopping.wait(tick):
            now = time.time()
            try:
                for agent_id in self.view.due_offline(now):
                    log.info("agent %s missed 3 heartbeats; marking offline", agent_id)
                    self.hub_event(
                        "agent.offline", {"agent_id": agent_id, "reason": "timeout"}
                    )
                self._resolve_due_decisions(now)
                self.sweep_exchange(now)
            except Exception:
                log.exception("reaper tick failed")


# ------------------------------------------------------------- HTTP plumbing


class _HubHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128
    hub: "Optional[Hub]" = None

    def handle_error(self, request, client_address):  # noqa: D102 - stdlib override
        # A client vanishing mid-response is normal, not an incident.
        log.debug("connection error from %s", client_address, exc_info=True)


class _HubHTTPServer6(_HubHTTPServer):
    address_family = socket.AF_INET6


def _make_handler(hub: Hub):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Parley/" + __version__
        sys_version = ""
        #: Keep-alive sockets that go quiet must not pin a thread forever.
        timeout = 65

        # ------------------------------------------------------------ plumbing

        def log_message(self, fmt, *args):  # noqa: D102 - stdlib override
            # Redacted: the request line carries ?vt=/?ht= credentials.
            try:
                line = api.redact_path(fmt % args)
            except Exception:  # noqa: BLE001
                line = "<unloggable request line>"
            log.debug("%s - %s", self.address_string(), line)

        def _request_headers(self) -> Dict[str, str]:
            hdr = api.lower_headers(self.headers)
            try:
                hdr["x-parley-peer-addr"] = self.client_address[0]
            except (AttributeError, IndexError):
                hdr["x-parley-peer-addr"] = "?"
            return hdr

        def _read_body(self) -> bytes:
            limit = int(hub.policy.get("max_blob_bytes", 26214400) or 26214400) + (1 << 20)
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                return self._read_chunked(limit)
            raw_len = self.headers.get("Content-Length")
            if not raw_len:
                return b""
            try:
                length = int(raw_len)
            except ValueError:
                raise BadRequest("malformed Content-Length", hint="Send an integer byte count.")
            if length < 0:
                raise BadRequest("negative Content-Length", hint="Send an integer byte count.")
            if length > limit:
                raise TooLarge(
                    "request body exceeds the Hub's limit",
                    detail={"bytes": length, "max": limit},
                    hint="Split the upload, or raise max_blob_bytes on the Hub.",
                )
            return self.rfile.read(length) if length else b""

        def _read_chunked(self, limit: int) -> bytes:
            buf = io.BytesIO()
            total = 0
            while True:
                line = self.rfile.readline(64)
                if not line:
                    break
                size_part = line.split(b";", 1)[0].strip()
                try:
                    size = int(size_part, 16)
                except ValueError:
                    raise BadRequest("malformed chunked body", hint="Use a valid chunk size line.")
                if size == 0:
                    self.rfile.readline(8192)  # trailer terminator
                    break
                total += size
                if total > limit:
                    raise TooLarge(
                        "request body exceeds the Hub's limit",
                        detail={"max": limit},
                        hint="Split the upload, or raise max_blob_bytes on the Hub.",
                    )
                buf.write(self.rfile.read(size))
                self.rfile.read(2)  # trailing CRLF
            return buf.getvalue()

        def _send(self, status: int, headers: Dict[str, str], body: bytes) -> None:
            explicit_len = headers.get("Content-Length")
            try:
                self.send_response(status)
                for k, v in headers.items():
                    if k.lower() == "content-length":
                        continue
                    self.send_header(k, v)
                if self.command == "HEAD" and explicit_len is not None:
                    self.send_header("Content-Length", str(explicit_len))
                else:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD" and body:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                self.close_connection = True
                log.debug("client disconnected before the response was written")

        def _dispatch(self, method: str) -> None:
            try:
                body = self._read_body()
            except ParleyError as err:
                self.close_connection = True
                status, headers, data = api.error_response(hub, err)
                self._send(status, headers, data)
                return
            status, headers, data = api.handle(hub, method, self.path, self._request_headers(), body)
            self._send(status, headers, data)

        # ------------------------------------------------------------- verbs

        def do_GET(self):  # noqa: N802 - stdlib naming
            if urlsplit(self.path).path == "/v1/stream":
                self._serve_sse()
                return
            self._dispatch("GET")

        def do_HEAD(self):  # noqa: N802
            self._dispatch("HEAD")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self):  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self):  # noqa: N802
            self._dispatch("DELETE")

        def do_OPTIONS(self):  # noqa: N802
            headers = api.base_headers(hub)
            headers["Allow"] = "GET, HEAD, POST, OPTIONS"
            headers["Content-Type"] = "text/plain; charset=utf-8"
            self._send(204, headers, b"")

        # --------------------------------------------------------------- SSE

        def _peer_gone(self) -> bool:
            """Has the SSE client closed its half of the connection?

            An SSE client never sends anything after the request, so the socket
            becoming readable means either EOF or a protocol violation -- both of
            which mean we should stop.  ``MSG_PEEK`` leaves the byte in place so
            we are not corrupting anything if it is somehow real data.  Without
            this we would only notice at the next write, up to 15 s later, and a
            page of closed browser tabs would each hold a thread until then.
            """
            try:
                readable, _w, _x = select.select([self.connection], [], [], 0)
                if not readable:
                    return False
                peeked = self.connection.recv(1, socket.MSG_PEEK)
                return not peeked
            except (OSError, ValueError, AttributeError):
                return True

        def _chunk(self, data: bytes) -> None:
            self.wfile.write(b"%x\r\n" % len(data))
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        def _sse_event(self, event: dict) -> None:
            # Bare LF line endings: the EventSource grammar accepts CR, LF or
            # CRLF, but LF is what every proxy and polyfill is exercised against.
            payload = dumps(event).encode("utf-8")
            frame = b"id: %d\n" % int(event.get("seq") or 0)
            frame += b"event: parley\n"
            frame += b"data: " + payload + b"\n\n"
            self._chunk(frame)

        def _serve_sse(self) -> None:
            self.close_connection = True
            path, query = api.split_path(self.path)
            headers = self._request_headers()
            peer = headers.get("x-parley-peer-addr", "?")

            try:
                if hub.shutting_down:
                    from ..errors import ShuttingDown

                    raise ShuttingDown(
                        "the Hub is shutting down",
                        hint="Reconnect shortly; your local log is intact.",
                    )
                auth = api.authenticate(hub.store, hub.config, "GET", self.path, headers, b"")
                if auth["kind"] == "enroll":
                    from ..errors import UnknownAgent

                    raise UnknownAgent(
                        "enrol credentials cannot open a stream",
                        hint="Use the agent key returned by POST /v1/enroll.",
                    )
                agent = auth.get("agent")
                if agent and agent.get("status") == "pending":
                    from ..errors import PendingApproval

                    raise PendingApproval(
                        "this agent is waiting for the host to approve it",
                        hint="Poll GET /v1/me until status is active.",
                    )
                types = api._types_param(query.get("types", ""))
                sub, head = hub.subscribe(
                    types=types, peer=peer, label=str(agent.get("agent_id")) if agent else "viewer"
                )
            except ParleyError as err:
                status, hdrs, data = api.error_response(hub, err)
                self._send(status, hdrs, data)
                return

            # `since` precedence: explicit query, then the browser's automatic
            # Last-Event-ID on reconnect (SPEC §5.1), then "everything".
            if "since" in query:
                since = api._int(query.get("since"), 0)
            elif headers.get("last-event-id"):
                since = api._int(headers.get("last-event-id"), 0)
            else:
                since = 0

            agent_id = str(agent.get("agent_id")) if auth.get("agent") else ""
            try:
                self._run_sse(sub, head, since, agent_id, peer)
            finally:
                hub.unsubscribe(sub)

        def _run_sse(self, sub, head: int, since: int, agent_id: str, peer: str) -> None:
            try:
                self.connection.settimeout(SSE_WRITE_TIMEOUT_S)
            except OSError:
                pass

            out = api.base_headers(hub)
            out["Content-Type"] = "text/event-stream; charset=utf-8"
            out["Cache-Control"] = "no-store"
            out["X-Accel-Buffering"] = "no"  # tell nginx not to buffer us
            out["Connection"] = "close"
            out["Transfer-Encoding"] = "chunked"
            try:
                self.send_response(200)
                for k, v in out.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.flush()
            except (OSError, ValueError):
                return

            last_sent = head if since < 0 else max(0, since)
            try:
                # Retry hint for browsers; harmless for other clients.
                self._chunk(b"retry: 2000\n\n")

                if since >= 0:
                    cursor = since
                    while cursor < head:
                        batch = hub.store.read(cursor, REPLAY_PAGE, sub.types)
                        if not batch:
                            break
                        for ev in batch:
                            seq = int(ev.get("seq") or 0)
                            if seq > head:
                                break
                            self._sse_event(ev)
                            cursor = seq
                        if len(batch) < REPLAY_PAGE:
                            break
                    last_sent = max(last_sent, cursor, 0)
                    # Nothing to replay still deserves a frame, so a client can
                    # tell "connected and idle" from "connecting".
                last_sent = max(last_sent, 0)

                next_ping = time.time() + SSE_PING_S
                while not hub._stopping.is_set():
                    if sub.overflow:
                        self._chunk(
                            b"event: parley-overflow\ndata: "
                            + dumps(
                                {
                                    "reason": "subscriber_too_slow",
                                    "resume_from": last_sent,
                                    "hint": "Reconnect with ?since=%d" % last_sent,
                                }
                            ).encode("utf-8")
                            + b"\n\n"
                        )
                        log.warning(
                            "closing overflowed SSE stream #%d (%s); resume_from=%d",
                            sub.id, peer, last_sent,
                        )
                        break
                    # Capped at SSE_POLL_S so the EOF probe below runs about once
                    # a second; the ping itself still only goes out every 15 s.
                    timeout = max(0.05, min(SSE_POLL_S, next_ping - time.time()))
                    try:
                        ev = sub.q.get(timeout=timeout)
                    except queue.Empty:
                        if self._peer_gone():
                            log.debug("SSE stream #%d (%s): peer closed", sub.id, peer)
                            break
                        if time.time() >= next_ping:
                            # The ping also forces a write, which is the backstop
                            # detector for a peer that died without a FIN.
                            self._chunk(b": ping\n\n")
                            next_ping = time.time() + SSE_PING_S
                        continue
                    if ev is None:  # hub stopping
                        break
                    seq = int(ev.get("seq") or 0)
                    if seq <= last_sent:
                        continue  # already covered by the replay
                    self._sse_event(ev)
                    last_sent = seq
                    if time.time() >= next_ping:
                        next_ping = time.time() + SSE_PING_S
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    pass
            except (
                BrokenPipeError,
                ConnectionResetError,
                ConnectionAbortedError,
                socket.timeout,
                OSError,
                ValueError,
            ) as exc:
                log.debug("SSE stream #%d (%s) closed: %s", sub.id, peer, exc)
            finally:
                if agent_id:
                    hub.store.touch_agent(agent_id, time.time())

    return _Handler


# --------------------------------------------------------------- bootstrapping


class HubStateExists(ParleyError):
    """``create_parley`` was pointed at a directory that already holds a parley."""

    code = "hub_state_exists"
    http_status = 409


class NoHubState(ParleyError):
    """``resume_parley`` was pointed at something that is not a Hub state directory."""

    code = "no_hub_state"
    http_status = 404


def hub_state_dir(workspace: Path) -> Path:
    """Where a Hub hosted from ``workspace`` keeps its state.

    One place, so ``init``, ``resume`` and every tool that has to find an
    existing parley agree.  Nothing is created here -- see
    :func:`find_hub_state_dir` for the "does one already exist" question.
    """
    return Path(workspace).expanduser() / ".parley" / "hub"


def find_hub_state_dir(workspace: Path) -> "Optional[Path]":
    """The state directory of the parley hosted from ``workspace``, or ``None``.

    ``.parley`` itself is accepted as a fallback because very early versions of
    Parley -- and a hand-restored backup -- can put ``hub.json`` there.
    """
    workspace = Path(workspace).expanduser()
    for candidate in (hub_state_dir(workspace), workspace / ".parley"):
        if (candidate / "hub.json").is_file():
            return candidate
    return None


def discard_hub_state(state_dir: Path) -> None:
    """Remove a Hub state directory, log, blobs and all.

    Called for ``create_parley(force=True)``, and by the CLI to roll back a
    parley that was created but never managed to listen.  It is a deliberate
    amputation: a new parley has a new session id and a new root key, so the old
    log and the old agent keys could not be used by it anyway, and leaving them
    behind would mean a directory holding two sessions' rows.
    """
    shutil.rmtree(str(state_dir), ignore_errors=True)


def create_parley(
    workspace: Path,
    *,
    name: str,
    port: int = 7777,
    bind: str = "0.0.0.0",
    public: bool = False,
    sealed: bool = False,
    require_approval: bool = False,
    words: int = 5,
    force: bool = False,
) -> Tuple[Hub, str]:
    """Create a brand-new parley.

    Returns ``(hub, watchword)``.  The watchword is returned exactly once and is
    never written anywhere: the Hub persists only the derived root key and a
    sha256 of the normalised watchword (so ``parley invite`` can *check* a typed
    watchword without being able to reproduce it).

    Refuses when ``workspace`` already hosts a parley: this mints a new session
    id and a new root key, so running it over an existing state directory would
    silently lock out every enrolled agent (they would get
    ``fingerprint_mismatch``).  :func:`resume_parley` is what restarts an
    existing one.  ``force=True`` destroys the existing state and starts over --
    which is sometimes exactly what is wanted, but never by accident.
    """
    workspace = Path(workspace)
    existing = find_hub_state_dir(workspace)
    if existing is not None:
        if not force:
            raise HubStateExists(
                "a parley already exists in %s" % existing,
                detail={"state_dir": str(existing)},
                hint="Restart it with `parley resume` -- that keeps the session id, the "
                     "fingerprint, the log and every enrolled agent. `parley init --force` "
                     "would throw all of that away.",
            )
        discard_hub_state(existing)

    state_dir = workspace_state_dir(workspace) / "hub"
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass

    session = ids.new_session_id()
    watchword = crypto.generate_watchword(words)
    normalised = crypto.normalise_watchword(watchword)
    iterations = int(DEFAULTS.get("pbkdf2_iterations", 200_000) or 200_000)
    root_key = crypto.derive_root_key(normalised, session, iterations=iterations)

    policy: Dict[str, Any] = dict(DEFAULTS)
    policy.update(
        {
            "enroll_open": True,
            "enroll_ttl_s": 3600 if public else 0,
            "enroll_max_uses": 8 if public else 0,
            "require_approval": bool(require_approval or public),
            "max_agents": int(DEFAULTS.get("max_agents", 16) or 16),
            "sealed": bool(sealed),
            "public": bool(public),
        }
    )

    config = HubConfig(
        session=session,
        name=name,
        created=now_rfc3339(),
        bind=bind,
        port=port,
        watchword_hash=sha256_hex(normalised.encode("utf-8")),
        root_key_hex=root_key.hex(),
        fingerprint=crypto.fingerprint(root_key),
        host_token=ids.new_host_token(),
        policy=policy,
        pbkdf2_iterations=iterations,
    )
    config.save(state_dir)

    hub = Hub(state_dir, config, workspace=workspace)
    log.info(
        "created parley '%s' session=%s fingerprint=%s (watchword returned to the caller only)",
        name, session, config.fingerprint,
    )
    return hub, watchword


def load_parley(state_dir: Path, *, workspace: "Optional[Path]" = None) -> Hub:
    """Re-open an existing parley from its state directory."""
    state_dir = Path(state_dir)
    config = HubConfig.load(state_dir)
    return Hub(state_dir, config, workspace=workspace)


def _load_hub_config(state_dir: Path) -> HubConfig:
    """Load and sanity-check ``hub.json``, or raise :class:`NoHubState`.

    "The file parses" is not enough: a Hub with no session id or no root key
    cannot authenticate anybody, and failing here with a clear message beats
    starting and rejecting every request with ``bad_signature``.
    """
    try:
        config = HubConfig.load(state_dir)
    except Exception as exc:
        raise NoHubState(
            "%s/hub.json is not a readable Hub state file (%s)" % (state_dir, exc),
            detail={"state_dir": str(state_dir)},
            hint="Restore it from a backup (docs/DEPLOY.md 5.4), or start a new parley "
                 "with `parley init --force` -- which locks out every agent enrolled in "
                 "the old one.",
        )
    missing = [field for field in ("session", "root_key_hex")
               if not str(getattr(config, field, "") or "")]
    if missing:
        raise NoHubState(
            "%s/hub.json is missing %s" % (state_dir, " and ".join(missing)),
            detail={"state_dir": str(state_dir), "missing": missing},
            hint="Without those the Hub cannot be the same parley it was. Restore the file "
                 "from a backup, or start a new parley with `parley init --force`.",
        )
    return config


def resume_parley(
    workspace: Path,
    *,
    port: "Optional[int]" = None,
    bind: "Optional[str]" = None,
) -> Hub:
    """Restart the Hub on an existing state directory (SPEC 11).

    This is the invocation a service supervisor must use.  ``create_parley``
    mints a new session id and a new root key every time it runs, so a
    ``Restart=on-failure`` unit pointed at ``init`` silently starts a *different*
    parley and strands every enrolled client behind a ``fingerprint_mismatch``.
    Here nothing is minted: the session id, root key, fingerprint, host token,
    event log, blobs and roster are all read back off disk exactly as they were.

    ``port`` and ``bind`` override what is stored and are then persisted, because
    ``parley approve`` and ``parley invite`` reach the Hub at the address in
    ``hub.json`` -- leaving the old one there would break them after a move.
    """
    workspace = Path(workspace)
    state_dir = find_hub_state_dir(workspace)
    if state_dir is None:
        raise NoHubState(
            "no parley is hosted from %s" % workspace,
            detail={"workspace": str(workspace), "expected": str(hub_state_dir(workspace))},
            hint="`resume` restarts an existing parley; there is no state directory here to "
                 "restart. Start one with `parley init`, or point --workspace at the folder "
                 "the Hub was created in.",
        )

    config = _load_hub_config(state_dir)
    moved = False
    if bind is not None and str(bind) != str(config.bind):
        config.bind = str(bind)
        moved = True
    if port is not None and int(port) != int(config.port):
        config.port = int(port)
        moved = True
    if moved:
        config.save(state_dir)

    hub = Hub(state_dir, config, workspace=workspace)
    log.info(
        "resumed parley '%s' session=%s fingerprint=%s head=%d agents=%d",
        config.name, config.session, config.fingerprint,
        hub.store.head_seq(), len(hub.store.list_agents()),
    )
    return hub
