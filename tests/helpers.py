"""Shared fixtures, reference implementations and small utilities for the test suite.

Two rules govern this file:

1. **It imports nothing from ``parley`` at module scope.** Layers are built in parallel and
   a half-finished package must not stop ``tests/test_portability.py`` (which only reads
   source) from running. Everything that needs the package imports it inside a function.
2. **The reference crypto here is independent of the implementation under test.** It is a
   literal transcription of RFC 5869 and RFC 8439 used as an oracle: first the oracle is
   checked against the published vectors, then ``parley.crypto`` is checked against the
   oracle. Testing an implementation against itself proves nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import socket
import struct
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------- layout

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "parley"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def json_fixture(name: str):
    return json.loads(fixture(name).decode("utf-8"))


# --------------------------------------------------------------------------- imports


def try_import(name: str):
    """Import ``name`` or return ``None``. Never raises."""
    try:
        return importlib.import_module(name)
    except Exception:  # pragma: no cover - depends on which layers have landed
        return None


def require(*names: str):
    """Import every module in ``names`` or raise ``SkipTest`` naming what is missing.

    Used only by the two suites that are explicitly allowed to skip —
    ``test_conformance`` and ``test_integration``. Everywhere else a missing module is an
    import error on purpose: an unbuilt layer should be loud, not quietly green.
    """
    modules = []
    for name in names:
        try:
            modules.append(importlib.import_module(name))
        except Exception as exc:
            raise unittest.SkipTest(
                "{0} is not available yet ({1}: {2}); this suite needs it".format(
                    name, type(exc).__name__, exc
                )
            )
    return modules[0] if len(modules) == 1 else tuple(modules)


# --------------------------------------------------------------------------- waiting


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.02, message: str = ""):
    """Poll ``predicate`` until it returns something truthy, or fail with a useful message.

    Tests never sleep a fixed amount and hope. They poll with a deadline, and when the
    deadline passes they say what they were waiting for.
    """
    deadline = time.monotonic() + timeout
    result = None
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
        except Exception as exc:  # the thing being waited for may not exist yet
            result = None
            last_error = exc
        else:
            last_error = None
        if result:
            return result
        time.sleep(interval)
    detail = message or "condition never became true"
    if last_error is not None:
        detail += " (last attempt raised {0}: {1})".format(
            type(last_error).__name__, last_error
        )
    raise AssertionError("timed out after {0:.1f}s waiting: {1}".format(timeout, detail))


def free_port() -> int:
    """A port that was free a moment ago. Prefer binding to port 0 where the API allows."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


# --------------------------------------------------------------------------- events

SESSION = "ses_9f2c41ab77e0d315"
AGENT_A = "agt_0c5518aa91be7742"
AGENT_B = "agt_1b7fa2c0349e8d51"
AGENT_C = "agt_2d93ef114a0b6c87"

WIRE_VERSION = "PARLEY/1"


def ev(
    seq=None,
    actor: str = AGENT_A,
    etype: str = "chat.message",
    body=None,
    event_id: str = "",
    ts: str = "",
    session: str = SESSION,
    **extra
):
    """Build a plausible log event. Only the fields a test cares about need naming."""
    event = {
        "v": WIRE_VERSION,
        "id": event_id or "evt_{0:016x}".format(abs(hash((seq, actor, etype))) % (1 << 64)),
        "ts": ts or "2026-10-08T12:00:00.000Z",
        "session": session,
        "actor": actor,
        "type": etype,
        "body": {} if body is None else body,
    }
    if seq is not None:
        event["seq"] = seq
    event.update(extra)
    return event


def ts_at(offset_s: float, base: str = "2026-10-08T12:00:00.000Z") -> str:
    """``base`` shifted by ``offset_s`` seconds, in SPEC §1.2 wire form."""
    import datetime as _dt

    stamp = _dt.datetime(2026, 10, 8, 12, 0, 0, tzinfo=_dt.timezone.utc)
    if base != "2026-10-08T12:00:00.000Z":
        stamp = _dt.datetime.strptime(base, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=_dt.timezone.utc
        )
    stamp = stamp + _dt.timedelta(seconds=offset_s)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + "{0:03d}Z".format(stamp.microsecond // 1000)


# --------------------------------------------------------------- RFC 5869 reference


def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    """RFC 5869 §2.2. An empty salt means HashLen zero bytes."""
    if not salt:
        salt = b"\x00" * hashlib.sha256().digest_size
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 §2.3."""
    hash_len = hashlib.sha256().digest_size
    if length > 255 * hash_len:
        raise ValueError("length too large for HKDF-SHA256")
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def hkdf_reference(ikm: bytes, info: bytes = b"", length: int = 32, salt: bytes = b"") -> bytes:
    return hkdf_expand(hkdf_extract(salt, ikm), info, length)


# --------------------------------------------------------------- RFC 8439 reference


def _rotl32(value: int, count: int) -> int:
    return ((value << count) | (value >> (32 - count))) & 0xFFFFFFFF


def _quarter_round(state, a, b, c, d) -> None:
    state[a] = (state[a] + state[b]) & 0xFFFFFFFF
    state[d] = _rotl32(state[d] ^ state[a], 16)
    state[c] = (state[c] + state[d]) & 0xFFFFFFFF
    state[b] = _rotl32(state[b] ^ state[c], 12)
    state[a] = (state[a] + state[b]) & 0xFFFFFFFF
    state[d] = _rotl32(state[d] ^ state[a], 8)
    state[c] = (state[c] + state[d]) & 0xFFFFFFFF
    state[b] = _rotl32(state[b] ^ state[c], 7)


def chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    """RFC 8439 §2.3."""
    if len(key) != 32 or len(nonce) != 12:
        raise ValueError("ChaCha20 wants a 32-byte key and a 12-byte nonce")
    constants = struct.unpack("<4I", b"expand 32-byte k")
    state = list(constants) + list(struct.unpack("<8I", key))
    state += [counter & 0xFFFFFFFF] + list(struct.unpack("<3I", nonce))
    work = list(state)
    for _ in range(10):
        _quarter_round(work, 0, 4, 8, 12)
        _quarter_round(work, 1, 5, 9, 13)
        _quarter_round(work, 2, 6, 10, 14)
        _quarter_round(work, 3, 7, 11, 15)
        _quarter_round(work, 0, 5, 10, 15)
        _quarter_round(work, 1, 6, 11, 12)
        _quarter_round(work, 2, 7, 8, 13)
        _quarter_round(work, 3, 4, 9, 14)
    return struct.pack("<16I", *[(work[i] + state[i]) & 0xFFFFFFFF for i in range(16)])


def chacha20(key: bytes, counter: int, nonce: bytes, data: bytes) -> bytes:
    """RFC 8439 §2.4."""
    out = bytearray()
    for offset in range(0, len(data), 64):
        stream = chacha20_block(key, counter + offset // 64, nonce)
        chunk = data[offset : offset + 64]
        out += bytes(a ^ b for a, b in zip(chunk, stream))
    return bytes(out)


def poly1305(key: bytes, message: bytes) -> bytes:
    """RFC 8439 §2.5."""
    if len(key) != 32:
        raise ValueError("Poly1305 wants a 32-byte one-time key")
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:32], "little")
    prime = (1 << 130) - 5
    acc = 0
    for offset in range(0, len(message), 16):
        block = message[offset : offset + 16]
        acc = ((acc + int.from_bytes(block + b"\x01", "little")) * r) % prime
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _pad16(data: bytes) -> bytes:
    return b"\x00" * ((-len(data)) % 16)


def aead_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes):
    """RFC 8439 §2.8. Returns ``(ciphertext, tag)``."""
    one_time_key = chacha20_block(key, 0, nonce)[:32]
    ciphertext = chacha20(key, 1, nonce, plaintext)
    mac_data = aad + _pad16(aad) + ciphertext + _pad16(ciphertext)
    mac_data += struct.pack("<Q", len(aad)) + struct.pack("<Q", len(ciphertext))
    return ciphertext, poly1305(one_time_key, mac_data)


def aead_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes, aad: bytes) -> bytes:
    one_time_key = chacha20_block(key, 0, nonce)[:32]
    mac_data = aad + _pad16(aad) + ciphertext + _pad16(ciphertext)
    mac_data += struct.pack("<Q", len(aad)) + struct.pack("<Q", len(ciphertext))
    if not hmac.compare_digest(poly1305(one_time_key, mac_data), tag):
        raise ValueError("Poly1305 tag mismatch")
    return chacha20(key, 1, nonce, ciphertext)


# --------------------------------------------------------------------- secret sniffer


class SecretScanner:
    """Collects every byte string a test saw come out of the Hub and hunts for secrets.

    SPEC §3.7 and §12 both say the watchword, keys and tokens must never appear in a log
    line, an event body or an error message. That is only checkable in aggregate, so error
    bodies get recorded here as they are produced and one test at the end searches the lot.
    """

    def __init__(self) -> None:
        self.secrets = []  # list of (label, text)
        self.samples = []  # list of (where, text)

    def add_secret(self, value: str, label: str) -> None:
        if value and len(str(value)) >= 6:
            self.secrets.append((label, str(value)))

    def record(self, where: str, payload) -> None:
        if payload is None:
            return
        if isinstance(payload, (bytes, bytearray)):
            text = bytes(payload).decode("utf-8", "replace")
        elif isinstance(payload, (dict, list)):
            text = json.dumps(payload, ensure_ascii=False)
        else:
            text = str(payload)
        self.samples.append((where, text))

    def violations(self):
        found = []
        for where, text in self.samples:
            lowered = text.lower()
            for label, secret in self.secrets:
                if secret.lower() in lowered:
                    found.append((where, label, text[:400]))
        return found

    def reset(self) -> None:
        self.samples = []


#: One scanner for the whole run, so every suite can feed it.
SECRETS = SecretScanner()


# ------------------------------------------------------------------- scripted servers


class ScriptedHTTPServer:
    """A loopback HTTP server that replays a canned response byte-for-byte.

    Used by the SSE tests to control exactly where the TCP chunk boundaries fall, which is
    the whole point of an adversarial framing test. ``chunks`` is the list of byte strings
    to write, flushing between each.
    """

    def __init__(self, chunks, status: int = 200, content_type: str = "text/event-stream"):
        self.chunks = list(chunks)
        self.status = status
        self.content_type = content_type
        self.requests = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
                with outer._lock:
                    outer.requests.append((self.command, self.path, dict(self.headers)))
                self.send_response(outer.status)
                self.send_header("Content-Type", outer.content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in outer.chunks:
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ValueError):
                        return

            do_POST = do_GET

            def log_message(self, *args):  # keep the test output clean
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="scripted-http", daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return "http://{0}:{1}".format(host, port)

    def __enter__(self) -> "ScriptedHTTPServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# ------------------------------------------------------------------ signed HTTP calls


def sign_headers(method: str, path: str, body: bytes, session: str, agent: str, key: bytes,
                 timestamp=None, nonce: str = ""):
    """Build the SPEC §3.3 header set. Imports ``parley.crypto`` lazily on purpose."""
    crypto = importlib.import_module("parley.crypto")
    stamp = str(int(time.time()) if timestamp is None else int(timestamp))
    nonce = nonce or crypto.new_nonce_hex()
    sts = crypto.string_to_sign(method, path, body or b"", stamp, nonce, session, agent)
    return {
        "X-Parley-Version": WIRE_VERSION,
        "X-Parley-Session": session,
        "X-Parley-Agent": agent,
        "X-Parley-Timestamp": stamp,
        "X-Parley-Nonce": nonce,
        "Authorization": "Parley-HMAC-SHA256 " + crypto.sign(key, sts),
        "Content-Type": "application/json; charset=utf-8",
    }


def http_call(url: str, method: str = "GET", body=None, headers=None, timeout: float = 10.0):
    """Plain urllib round trip. Returns ``(status, headers_dict, body_bytes)``.

    Used by ``test_conformance`` so the suite can be pointed at any Hub implementation,
    not only the in-process reference one.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, method=method)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode(), dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        try:
            # HTTPError is itself a response object; leaving it unclosed leaks the socket
            # and makes the suite emit ResourceWarnings on every error-path assertion.
            return exc.code, dict(exc.headers or {}), exc.read()
        finally:
            exc.close()


def decode_json(payload: bytes):
    try:
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


# ------------------------------------------------------------------------- misc


def thread_names():
    return sorted(t.name for t in threading.enumerate() if t is not threading.current_thread())


def filesystem_is_case_insensitive(directory: Path) -> bool:
    probe = Path(directory) / "ParleyCaseProbe.tmp"
    probe.write_bytes(b"x")
    try:
        return (Path(directory) / "parleycaseprobe.tmp").exists()
    finally:
        probe.unlink()


def supports_symlinks(directory: Path) -> bool:
    link = Path(directory) / "parley-symlink-probe"
    try:
        link.symlink_to(Path(directory))
    except (OSError, NotImplementedError, AttributeError):
        return False
    link.unlink()
    return True


IS_WINDOWS = os.name == "nt"
PY_VERSION = sys.version_info[:2]
