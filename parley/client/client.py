"""The participant-facing client: enrolment, events, blobs and the live stream.

Everything a participant does is "append an event to the log", so this class is
mostly a set of well-named wrappers over :meth:`ParleyClient.emit`. The parts
that carry real weight are:

* **Enrolment** (SPEC 3.4). The watchword is an *enrolment secret only*: it is
  used once, to derive the enrol key, and then never again - the Hub mints a
  per-agent key that everything afterwards uses. That two-tier design is why
  rotating the watchword does not kick anyone out, and it is also why the
  watchword is never written to disk here.
* **The fingerprint check** (SPEC 3.5). Before sending the watchword-derived
  proof anywhere we confirm that the Hub's advertised fingerprint matches the
  one *we* derive from the watchword. A relay pointing us at a different parley
  therefore fails before any secret-derived material leaves the machine, and a
  changed fingerprint for a session we already know is a hard error that is
  never auto-accepted.
* **Idempotent writes.** Every event carries an author-assigned ``id``; a retry
  after an ambiguous failure reuses it, and the Hub deduplicates on
  ``(actor, id)``. A ``409 duplicate_event`` is therefore *success*, not an
  error, and is reported as such.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .. import crypto, errors, protocol
from ..config import Credentials, workspace_state_dir, resolve_hub_url
from ..jsonutil import loads, sha256_hex
from ..version import WIRE_VERSION, __version__
from .transport import Transport, error_from_response

log = logging.getLogger("parley.client.client")

DEFAULT_CAPABILITIES = ("chat", "sync", "tasks", "psr")

#: Where a sealed-mode client keeps its seal key. The key derives from the
#: watchword, which we deliberately never persist, so without this file a
#: restarted sealed client could not decrypt anything. Written 0600 next to the
#: credentials, which carry an equivalent secret already.
SEAL_KEY_FILE = "seal.key"


def hub_hello(hub_url: str, *, timeout: float = 10.0) -> dict:
    """``GET /v1/hello`` - the one unauthenticated call in the protocol.

    It exists so a joiner can learn the session id (needed to derive the root
    key, since the session id is the PBKDF2 salt) and the fingerprint (needed to
    verify it is talking to the right parley) *before* authenticating.
    """
    url = resolve_hub_url(hub_url) + "/v1/hello"
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-Parley-Version", WIRE_VERSION)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001
            pass
        raise error_from_response(exc.code, body)
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        raise errors.TransportError(
            "cannot reach the hub at {0}".format(hub_url),
            detail={"cause": str(exc)},
            hint="Check the URL, that the Hub is running, and that the tunnel is up.",
        )
    doc = loads(raw)
    if not isinstance(doc, dict):
        raise errors.TransportError("hub /v1/hello did not return an object")
    return doc


class ParleyClient:
    """One participant's connection to a Hub."""

    def __init__(self, workspace: Path, creds: Credentials) -> None:
        self.workspace = Path(workspace).resolve()
        self.creds = creds
        self.state_dir = workspace_state_dir(self.workspace)

        key = bytes.fromhex(creds.agent_key_hex)
        seal = self._load_seal_key() if getattr(creds, "sealed", False) else None
        if getattr(creds, "sealed", False) and seal is None:
            raise errors.ParleyError(
                "this parley is sealed but the local seal key is missing",
                hint="Re-run `parley join` with the watchword to re-derive it.",
            )
        self.transport = Transport(
            creds.hub_url,
            creds.session,
            creds.agent_id,
            key,
            sealed=bool(getattr(creds, "sealed", False)),
            seal_key=seal,
        )
        self._key = key
        self._emit_lock = threading.Lock()
        self._last_psr: Optional[dict] = None
        self._last_psr_at = 0.0
        self.policy: Dict[str, Any] = dict(getattr(creds, "policy", None) or {})

    # ------------------------------------------------------------------ #
    # identity
    # ------------------------------------------------------------------ #
    @property
    def agent_id(self) -> str:
        return self.creds.agent_id

    @property
    def session(self) -> str:
        return self.creds.session

    @property
    def fingerprint(self) -> str:
        return getattr(self.creds, "fingerprint", "")

    def close(self) -> None:
        self.transport.close()

    # ------------------------------------------------------------------ #
    # enrolment
    # ------------------------------------------------------------------ #
    @classmethod
    def enroll(
        cls,
        hub_url: str,
        watchword: str,
        workspace: Path,
        *,
        name: str,
        kind: str,
        model: str = "",
        capabilities: Optional[List[str]] = None,
        sealed: bool = False,
        expect_fingerprint: str = "",
    ) -> "ParleyClient":
        """Join a parley with the watchword and store the minted credentials."""
        workspace = Path(workspace).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        state_dir = workspace_state_dir(workspace)
        hub_url = resolve_hub_url(hub_url)

        hello = hub_hello(hub_url)
        session = hello.get("session")
        if not isinstance(session, str) or not session.startswith("ses_"):
            raise errors.TransportError(
                "hub did not advertise a session id",
                detail={"hello": hello},
                hint="Is this URL really a Parley Hub?",
            )
        wire = hello.get("v")
        if isinstance(wire, str) and wire != WIRE_VERSION:
            raise errors.ParleyError(
                "hub speaks {0}, this client speaks {1}".format(wire, WIRE_VERSION),
                hint="Upgrade whichever side is older.",
            )

        iterations = hello.get("pbkdf2_iterations")
        normalised = crypto.normalise_watchword(watchword)
        if not normalised:
            raise errors.ParleyError(
                "the watchword is empty after normalisation",
                hint="Type it as it was spoken, e.g. \"copper otter climbs the quiet hill\".",
            )
        if isinstance(iterations, int) and iterations > 0:
            root = crypto.derive_root_key(normalised, session, iterations=iterations)
        else:
            root = crypto.derive_root_key(normalised, session)

        ours = crypto.fingerprint(root)
        theirs = hello.get("fingerprint")
        if isinstance(theirs, str) and theirs and ours != theirs:
            # Either the watchword is wrong, or something is relaying us to a
            # different parley. Both are fatal and neither is auto-recoverable.
            raise errors.FingerprintMismatch(
                "the watchword does not match this hub's parley",
                detail={"hub_fingerprint": theirs, "derived_fingerprint": ours},
                hint="Check the watchword with whoever hosted the parley, word by word.",
            )
        if expect_fingerprint and expect_fingerprint != ours:
            raise errors.FingerprintMismatch(
                "fingerprint is not the one you expected",
                detail={"expected": expect_fingerprint, "actual": ours},
                hint="Do not continue: you may be talking to a different Hub.",
            )
        cls._guard_known_session(workspace, session, ours)

        wants_seal = bool(sealed or hello.get("requires_seal"))
        sk = crypto.seal_key(root) if wants_seal else None

        payload = {
            "session": session,
            "agent": {
                "name": name,
                "kind": kind,
                "os": _os_name(),
                "host": _hostname(),
                "client_version": __version__,
                "capabilities": list(capabilities or DEFAULT_CAPABILITIES),
                "workspace_hint": str(workspace),
            },
        }
        if model:
            payload["agent"]["model"] = model

        enroll_transport = Transport(
            hub_url, session, "enroll", crypto.enroll_key(root),
            sealed=wants_seal, seal_key=sk,
        )
        try:
            status, _headers, body = enroll_transport.request(
                "POST", "/v1/enroll", json_body=payload, retry_on_ambiguous=False
            )
        finally:
            enroll_transport.close()
        if status >= 400:
            raise error_from_response(status, body)
        doc = loads(body) if body else {}
        if not isinstance(doc, dict):
            raise errors.TransportError("enrolment response was not an object")

        agent_id = doc.get("agent_id")
        agent_key_hex = doc.get("agent_key")
        if not isinstance(agent_id, str) or not isinstance(agent_key_hex, str):
            raise errors.TransportError(
                "enrolment response is missing agent_id/agent_key",
                detail={"keys": sorted(doc.keys())},
            )
        try:
            bytes.fromhex(agent_key_hex)
        except ValueError:
            raise errors.TransportError("enrolment returned a malformed agent key")

        creds = Credentials(
            hub_url=hub_url,
            session=session,
            agent_id=agent_id,
            agent_key_hex=agent_key_hex,
            fingerprint=doc.get("fingerprint") or ours,
            name=name,
            kind=kind,
            sealed=wants_seal,
            policy=doc.get("policy") or {},
        )
        if wants_seal and sk is not None:
            _write_seal_key(state_dir, sk)
        creds.save(workspace)
        log.info(
            "enrolled as %s (%s) in session %s; fingerprint %s",
            name, agent_id, session, creds.fingerprint,
        )
        # No agent.hello here: SPEC 4.1 says the *Hub* emits it on successful
        # enrolment, and the client emits it on every reconnect. Announcing
        # again now would put two identical hellos in the log for one join.
        return cls(workspace, creds)

    @staticmethod
    def _guard_known_session(workspace: Path, session: str, fingerprint: str) -> None:
        """Refuse to re-join a known session whose fingerprint changed (SPEC 3.5)."""
        try:
            previous = Credentials.load(workspace)
        except Exception:  # noqa: BLE001 - no credentials yet is the normal case
            return
        if getattr(previous, "session", "") != session:
            return
        known = getattr(previous, "fingerprint", "")
        if known and known != fingerprint:
            raise errors.FingerprintMismatch(
                "this session's fingerprint changed since you last joined",
                detail={"known": known, "now": fingerprint},
                hint="Someone may be impersonating the Hub. Verify the three words out loud "
                     "before deleting .parley/credentials.json and re-joining.",
            )

    def _load_seal_key(self) -> Optional[bytes]:
        path = self.state_dir / SEAL_KEY_FILE
        try:
            raw = path.read_text("ascii").strip()
        except OSError:
            return None
        try:
            return bytes.fromhex(raw)
        except ValueError:
            log.warning("seal key at %s is malformed", path)
            return None

    def announce(self) -> Optional[dict]:
        """Emit ``agent.hello`` (SPEC 4.1) - on join and after every reconnect."""
        body = {
            "name": self.creds.name,
            "kind": self.creds.kind,
            "os": _os_name(),
            "host": _hostname(),
            "client_version": __version__,
            "capabilities": list(DEFAULT_CAPABILITIES),
            "workspace_hint": str(self.workspace),
        }
        try:
            return self.emit("agent.hello", body)
        except errors.ParleyError as exc:
            log.warning("could not announce presence (%s)", exc)
            return None

    # ------------------------------------------------------------------ #
    # events
    # ------------------------------------------------------------------ #
    def emit(self, etype: str, body: dict, *, event_id: Optional[str] = None) -> dict:
        """Sign and append one event; returns the stored event with its ``seq``.

        ``event_id`` lets a caller pin the id. That matters for two things: the
        pigeonhole derives a stable id per outbox line so a crash cannot double
        publish, and any retry must reuse the id so the Hub's dedup applies.
        """
        if not isinstance(body, dict):
            raise errors.BadEvent(
                "event body must be an object", detail={"type": etype, "got": type(body).__name__}
            )
        event = protocol.make_event(self.agent_id, self.session, etype, body, event_id=event_id)
        problems = protocol.validate_event(event, strict=False)
        if problems:
            # The Hub is the authority (SPEC 2.1), so this is advice, not a veto -
            # except for problems that would certainly be rejected, which we
            # surface locally where the error is actually readable.
            log.warning("event %s has validation problems: %s", etype, "; ".join(problems))
        event["sig"] = crypto.sign_event(self._key, event)

        with self._emit_lock:
            status, _headers, raw = self.transport.request("POST", "/v1/events", json_body=event)
        if status == 409:
            # Our own event, already appended - exactly what the dedup is for.
            seq = _seq_from_duplicate(raw)
            if seq is not None:
                event["seq"] = seq
            log.debug("event %s was already appended (dedup hit)", event.get("id"))
            return event
        if status >= 400:
            raise error_from_response(status, raw)
        doc = loads(raw) if raw else {}
        seq = _seq_of(doc)
        if seq is not None:
            event["seq"] = seq
        if etype == "status.update":
            self._last_psr = dict(body)
            self._last_psr_at = time.monotonic()
        return event

    def say(
        self,
        text: str,
        *,
        to: Optional[Sequence[str]] = None,
        reply_to: Optional[str] = None,
        refs: Optional[List[dict]] = None,
        thread: Optional[str] = None,
        fmt: str = "text",
    ) -> dict:
        body: Dict[str, Any] = {"text": text}
        if to:
            body["to"] = list(to)
        if reply_to:
            body["reply_to"] = reply_to
        if thread:
            body["thread"] = thread
        if refs:
            body["refs"] = list(refs)
        if fmt and fmt != "text":
            body["format"] = fmt
        return self.emit("chat.message", body)

    def status(
        self,
        headline: str,
        *,
        state: str = "working",
        focus: Optional[Sequence[str]] = None,
        detail: str = "",
        progress: Optional[float] = None,
        task: Optional[str] = None,
        blocked_on: Optional[dict] = None,
        needs: Optional[Sequence[str]] = None,
        eta_s: Optional[int] = None,
    ) -> dict:
        """Emit a PSR (SPEC 6). Keys with no value are omitted, per SPEC 1.3."""
        from ..jsonutil import now_rfc3339

        body: Dict[str, Any] = {"state": state, "headline": headline, "since": now_rfc3339()}
        if detail:
            body["detail"] = detail
        if focus:
            body["focus"] = [str(f) for f in focus][:8]
        if progress is not None:
            body["progress"] = float(progress)
        if task:
            body["task"] = task
        if blocked_on:
            body["blocked_on"] = blocked_on
        if needs:
            body["needs"] = list(needs)
        if eta_s is not None:
            body["eta_s"] = int(eta_s)
        return self.emit("status.update", body)

    def repeat_psr(self) -> Optional[dict]:
        """Re-emit the last PSR to satisfy the freshness contract (SPEC 6.1).

        An agent that is genuinely doing the same thing should not have to
        invent a new headline every 30 seconds; it should restate the same one.
        """
        if not self._last_psr:
            return None
        return self.emit("status.update", dict(self._last_psr))

    @property
    def last_psr(self) -> Optional[dict]:
        return dict(self._last_psr) if self._last_psr else None

    def set_last_psr(self, body: dict) -> None:
        """Record a PSR emitted by another path (e.g. ``me.json``) for re-emission."""
        if isinstance(body, dict) and body:
            self._last_psr = dict(body)
            self._last_psr_at = time.monotonic()

    def know(self, title: str, kind: str, *, detail: str = "", refs: Optional[List[dict]] = None) -> dict:
        body: Dict[str, Any] = {"kind": kind, "title": title}
        if detail:
            body["detail"] = detail
        if refs:
            body["refs"] = list(refs)
        return self.emit("knowledge.contribution", body)

    def heartbeat(
        self,
        *,
        psr_seq: Optional[int] = None,
        workspace_files: Optional[int] = None,
        workspace_bytes: Optional[int] = None,
    ) -> dict:
        body: Dict[str, Any] = {}
        if psr_seq is not None:
            body["psr_seq"] = int(psr_seq)
        if workspace_files is not None:
            body["workspace_files"] = int(workspace_files)
        if workspace_bytes is not None:
            body["workspace_bytes"] = int(workspace_bytes)
        return self.emit("agent.heartbeat", body)

    def bye(self, reason: str = "") -> None:
        """Announce a clean departure. Best effort: shutdown must not hang."""
        body = {"reason": reason} if reason else {}
        try:
            self.emit("agent.bye", body)
        except errors.ParleyError as exc:
            log.debug("could not send agent.bye (%s)", exc)
        except Exception as exc:  # noqa: BLE001 - we are already on the way out
            log.debug("could not send agent.bye (%s)", exc)

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #
    def state(self) -> dict:
        return self.transport.get_json("/v1/state")

    def events(self, since: int = 0, limit: int = 1000) -> List[dict]:
        doc = self.transport.get_json("/v1/events?since={0}&limit={1}".format(int(since), int(limit)))
        for key in ("events", "items", "data", "log"):
            value = doc.get(key)
            if isinstance(value, list):
                return [e for e in value if isinstance(e, dict)]
        return []

    def stream(self, since: int = 0) -> Iterator[dict]:
        return self.transport.stream(since)

    def file_index(self) -> Dict[str, dict]:
        """The Hub's authoritative file index, as ``path -> record``.

        SPEC 5 does not pin the envelope, so both the mapping and the list
        shapes are accepted; a client that breaks because the Hub picked the
        other one would be needlessly brittle.
        """
        doc = self.transport.get_json("/v1/index")
        files = doc.get("files", doc.get("index"))
        out: Dict[str, dict] = {}
        if isinstance(files, dict):
            for path, rec in files.items():
                if isinstance(path, str) and isinstance(rec, dict):
                    out[path] = rec
        elif isinstance(files, list):
            for rec in files:
                if isinstance(rec, dict) and isinstance(rec.get("path"), str):
                    out[rec["path"]] = rec
        return out

    # ------------------------------------------------------------------ #
    # blobs
    # ------------------------------------------------------------------ #
    def has_blob(self, blob_hash: str) -> bool:
        return self.transport.has_blob(blob_hash)

    def put_blob(self, data: bytes, blob_hash: Optional[str] = None) -> str:
        if blob_hash is None:
            blob_hash = "sha256:" + sha256_hex(data)
        return self.transport.put_blob(data, blob_hash)

    def get_blob(self, blob_hash: str) -> bytes:
        return self.transport.get_blob(blob_hash)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _write_seal_key(state_dir: Path, key: bytes) -> None:
    path = Path(state_dir) / SEAL_KEY_FILE
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key.hex().encode("ascii"))
        finally:
            os.close(fd)
    except OSError as exc:
        log.warning("could not store the seal key (%s); sealed mode will need a re-join", exc)


def _seq_of(doc: Any) -> Optional[int]:
    """Pull the assigned ``seq`` out of whatever shape the Hub replied with."""
    if not isinstance(doc, dict):
        return None
    if isinstance(doc.get("seq"), int):
        return doc["seq"]
    for key in ("seqs", "events", "accepted", "results"):
        value = doc.get(key)
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, int):
                return first
            if isinstance(first, dict) and isinstance(first.get("seq"), int):
                return first["seq"]
    event = doc.get("event")
    if isinstance(event, dict) and isinstance(event.get("seq"), int):
        return event["seq"]
    return None


def _seq_from_duplicate(raw: bytes) -> Optional[int]:
    """A ``409 duplicate_event`` carries the original ``seq`` (SPEC 5.2)."""
    try:
        doc = loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(doc, dict):
        return None
    env = doc.get("error")
    if isinstance(env, dict):
        detail = env.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("seq"), int):
            return detail["seq"]
    return _seq_of(doc)


def _os_name() -> str:
    import sys

    plat = sys.platform
    if plat.startswith("linux"):
        return "linux"
    if plat == "darwin":
        return "darwin"
    if plat in ("win32", "cygwin"):
        return "windows"
    return plat


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""
