"""HTTP transport for a Parley client.

Why this module exists in the shape it does
-------------------------------------------
The Hub is reached over plain HTTP/1.1 (SPEC R4) and the connection *will* drop:
tunnels expire, laptops suspend, the Hub restarts, a corporate proxy decides a
long-lived response is idle. The protocol's answer to that is not "try harder"
but "make every operation safely repeatable":

* Every request is individually authenticated with a fresh timestamp and nonce
  (SPEC 3.3), so a retry is a *new* request as far as replay protection is
  concerned, but...
* ...the request *body* is reused byte-for-byte on a retry. A client event
  carries its own ``event.id``, so the Hub's ``(actor, id)`` dedup (SPEC 5.2)
  turns an ambiguous "did my POST land?" into a no-op that returns the original
  ``seq``. That is the entire reason the dedup exists, so we lean on it.
* ``stream()`` is a generator that never raises on a transient failure. It
  reconnects with full-jitter backoff, resumes from the last ``seq`` it actually
  handed to the caller, and fills any gap it notices with an explicit range
  fetch. The caller therefore sees a single, unbroken, strictly increasing,
  duplicate-free sequence of events for the lifetime of the parley.

Sealed mode and the stream
--------------------------
SPEC 3.6 defines sealing for request/response *bodies* and states that
``text/event-stream`` has **no** sealed framing in PARLEY/1: a sealed client MUST
NOT use ``/v1/stream``, and a Hub MUST answer 422 if it tries. A sealed client
therefore skips SSE entirely and long-polls ``GET /v1/events?wait=``, whose body
goes through the ordinary sealed path. Confidentiality is preserved; only
liveness latency changes (bounded by ``wait``).
"""

from __future__ import annotations

import gzip
import logging
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .. import crypto, errors
from ..jsonutil import dumps, loads, parse_rfc3339, sha256_hex
from ..version import WIRE_VERSION

log = logging.getLogger("parley.client.transport")

#: Read timeout floor for a streaming connection. The Hub sends a ``: ping``
#: comment every 15 s (SPEC 5.1); anything substantially longer than that with
#: no bytes at all means the path is dead even though the socket still looks
#: open, which is exactly the failure a TCP keepalive would not catch in time.
STREAM_TIMEOUT_S = 75.0

#: How long the long-poll fallback asks the Hub to hold a request open. The
#: spec caps ``wait`` at 30 s.
LONGPOLL_WAIT_S = 25

#: After this many consecutive SSE failures we stop trying SSE and fall back to
#: long-poll (SPEC 5.2 / 8.2 use the same "twice in a row" rule for the Deck).
SSE_FAILURES_BEFORE_FALLBACK = 2

#: ...but a proxy that broke SSE at 09:00 may be fine at 09:05, so periodically
#: give SSE another chance rather than degrading for the whole session.
SSE_RETRY_INTERVAL_S = 120.0

#: Upper bound on how many events a single gap-fill fetch asks for at a time.
GAP_FETCH_LIMIT = 500


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #

_ERROR_CLASSES: Optional[Dict[str, type]] = None


def _error_classes() -> Dict[str, type]:
    """Map SPEC 12 error codes to the exception classes in :mod:`parley.errors`.

    Built by introspection rather than by a hand-written table so that an error
    class added to ``parley.errors`` later is picked up here for free.
    """
    global _ERROR_CLASSES
    if _ERROR_CLASSES is None:
        found: Dict[str, type] = {}
        base = getattr(errors, "ParleyError")
        for name in dir(errors):
            obj = getattr(errors, name)
            if isinstance(obj, type) and issubclass(obj, base):
                code = getattr(obj, "code", None)
                if isinstance(code, str) and code:
                    found.setdefault(code, obj)
        _ERROR_CLASSES = found
    return _ERROR_CLASSES


def error_from_response(status: int, body: bytes) -> errors.ParleyError:
    """Turn a non-2xx Hub response into the right :class:`ParleyError` subclass.

    The Hub's error envelope (SPEC 12) carries the authoritative code; we only
    fall back to a status-derived guess when the body is unparseable, which is
    what a hostile proxy's own error page looks like.
    """
    code = ""
    message = ""
    detail: Dict[str, Any] = {}
    hint = ""
    retryable = status in (429, 502, 503, 504)
    try:
        payload = loads(body)
        if isinstance(payload, dict):
            env = payload.get("error")
            if isinstance(env, dict):
                code = str(env.get("code") or "")
                message = str(env.get("message") or "")
                raw_detail = env.get("detail")
                if isinstance(raw_detail, dict):
                    detail = raw_detail
                hint = str(env.get("hint") or "")
                if isinstance(env.get("retryable"), bool):
                    retryable = env["retryable"]
    except Exception:  # noqa: BLE001 - a non-JSON error body is expected in the wild
        pass

    cls = _error_classes().get(code)
    if cls is None:
        cls = getattr(errors, "TransportError")
    if not message:
        message = "HTTP {0} from hub".format(status)
    exc = cls(message, detail=detail or {"http_status": status}, hint=hint)
    # The Hub is allowed to mark an otherwise-fatal-looking code as retryable.
    try:
        exc.retryable = bool(retryable) or bool(getattr(cls, "retryable", False))
    except Exception:  # pragma: no cover - frozen/slotted subclass
        pass
    return exc


# --------------------------------------------------------------------------- #
# SSE framing
# --------------------------------------------------------------------------- #


class SSEParser:
    """Incremental parser for ``text/event-stream`` (WHATWG framing).

    This is a pure, side-effect-free state machine so it can be unit-tested
    without a server. It is deliberately strict about the two things that bite
    naive implementations:

    * **A chunk boundary can split a frame anywhere** - including between the
      ``\\r`` and the ``\\n`` of a CRLF terminator. Treating that ``\\r`` as a
      complete line end would silently produce one spurious blank line and
      dispatch a half-built event. We therefore hold a trailing ``\\r`` back
      until the next chunk (or EOF) tells us what follows it.
    * **A half-written frame at EOF must be discarded, not dispatched.** The
      bytes that were cut off are not lost: the stream resumes from the last
      ``seq`` the consumer actually received, so the Hub replays them.
    """

    __slots__ = ("_buf", "_data", "_event", "_last_id", "_retry", "_bom_checked",
                 "last_comment_at")

    def __init__(self) -> None:
        self._buf = b""
        self._data: List[bytes] = []
        self._event: Optional[str] = None
        self._last_id: Optional[str] = None
        self._retry: Optional[int] = None
        self._bom_checked = False
        self.last_comment_at: float = 0.0

    # -- public -------------------------------------------------------------
    @property
    def last_event_id(self) -> Optional[str]:
        """The most recent ``id:`` seen, which survives across frames per spec."""
        return self._last_id

    @property
    def retry_ms(self) -> Optional[int]:
        """Server-suggested reconnect delay from a ``retry:`` field, if any."""
        return self._retry

    def feed(self, chunk: bytes) -> List[Dict[str, Any]]:
        """Consume bytes; return every frame that completed inside them."""
        if not chunk:
            return []
        buf = self._buf + chunk
        if not self._bom_checked:
            # A UTF-8 BOM at the very start of the stream is stripped and never
            # treated as part of the first field name.
            if len(buf) >= 3:
                if buf[:3] == b"\xef\xbb\xbf":
                    buf = buf[3:]
                self._bom_checked = True
            elif not b"\xef\xbb\xbf".startswith(buf):
                self._bom_checked = True
            else:
                # Could still turn out to be a BOM; wait for more bytes.
                self._buf = buf
                return []

        out: List[Dict[str, Any]] = []
        pos = 0
        n = len(buf)
        while pos < n:
            j = pos
            while j < n and buf[j] != 13 and buf[j] != 10:
                j += 1
            if j == n:
                break  # no terminator yet; the rest is an incomplete line
            line = buf[pos:j]
            if buf[j] == 13:  # CR
                if j + 1 == n:
                    # Might be the first half of a CRLF split across chunks.
                    break
                pos = j + 2 if buf[j + 1] == 10 else j + 1
            else:  # LF
                pos = j + 1
            frame = self._line(line)
            if frame is not None:
                out.append(frame)
        self._buf = buf[pos:]
        return out

    def close(self) -> List[Dict[str, Any]]:
        """Signal EOF.

        A trailing ``\\r`` that we were holding back (it could have been the
        first half of a CRLF) is now known to be a complete terminator, so the
        line it ends is processed and may dispatch a frame. Anything after it is
        an unterminated line and is dropped deliberately - those bytes are not
        lost, because the consumer resumes from the last ``seq`` it received.
        """
        out: List[Dict[str, Any]] = []
        if self._buf.endswith(b"\r"):
            frame = self._line(self._buf[:-1])
            if frame is not None:
                out.append(frame)
            self._buf = b""
        dropped = bool(self._buf) or bool(self._data)
        self._buf = b""
        self._data = []
        self._event = None
        if dropped:
            log.debug("sse: discarded an incomplete frame at EOF (resume will replay it)")
        return out

    # -- internals ----------------------------------------------------------
    def _line(self, line: bytes) -> Optional[Dict[str, Any]]:
        if not line:
            return self._dispatch()
        if line[0:1] == b":":
            # Comment / keepalive. Carries no data but proves the path is alive.
            self.last_comment_at = time.monotonic()
            return None
        idx = line.find(b":")
        if idx < 0:
            field, value = line, b""
        else:
            field, value = line[:idx], line[idx + 1:]
            if value[0:1] == b" ":
                value = value[1:]
        name = field.decode("utf-8", "replace")
        if name == "data":
            self._data.append(value)
        elif name == "event":
            self._event = value.decode("utf-8", "replace")
        elif name == "id":
            if b"\x00" not in value:
                self._last_id = value.decode("utf-8", "replace")
        elif name == "retry":
            try:
                self._retry = int(value.decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                pass
        # Unknown fields are ignored, per spec.
        return None

    def _dispatch(self) -> Optional[Dict[str, Any]]:
        if not self._data:
            # A blank line with no data buffer resets the event name and is
            # otherwise a no-op (this is how a lone keepalive newline behaves).
            self._event = None
            return None
        payload = b"\n".join(self._data).decode("utf-8", "replace")
        frame = {
            "event": self._event or "message",
            "data": payload,
            "id": self._last_id,
            "retry": self._retry,
        }
        self._data = []
        self._event = None
        return frame


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


class Transport:
    """urllib-based Hub client with SPEC 3.3 signing, sealing and resilience.

    Thread-safety: a :class:`Transport` may be shared by several threads. The
    only mutable state is ``_skew`` (guarded by ``_lock``) and ``_closed``
    (a :class:`threading.Event`). Each call builds its own opener-free request,
    so concurrent requests do not interfere.
    """

    def __init__(
        self,
        hub_url: str,
        session: str,
        agent_id: str,
        key: bytes,
        *,
        sealed: bool = False,
        seal_key: Optional[bytes] = None,
        timeout: float = 30.0,
    ) -> None:
        self.hub_url = hub_url.rstrip("/")
        self.session = session
        self.agent_id = agent_id
        self._key = key
        self._sealed = bool(sealed)
        self._seal_key = seal_key
        if self._sealed and not self._seal_key:
            raise ValueError("sealed transport requires seal_key")
        self.timeout = float(timeout)

        self._lock = threading.Lock()
        self._skew = 0.0
        self._closed = threading.Event()
        # Set when a stream successfully reads at least one byte; used only for
        # diagnostics ("are we live?") by the runtime's health reporting.
        self.last_event_at: float = 0.0
        self.mode: str = "poll" if self._sealed else "sse"
        #: Called (from the stream thread) each time the live feed is
        #: re-established after a drop - never for the first connection. SPEC
        #: 4.1 requires the client to re-emit ``agent.hello`` on reconnect, and
        #: only the transport knows when a reconnect actually happened.
        self.on_reconnect: Optional[Any] = None
        self.connections: int = 0
        #: Live streaming responses, so close() can interrupt a blocked read.
        self._live: List[Any] = []

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        """Stop the stream and interrupt any read that is blocked on the socket.

        Setting the event alone is not enough: a healthy SSE connection can sit
        in ``read1()`` for the whole 75-second timeout waiting for the next
        ping, which would make ``parley run`` take over a minute to exit on
        Ctrl-C. Closing the underlying response makes that read raise
        immediately, and the stream loop then sees the flag and returns.
        """
        self._closed.set()
        with self._lock:
            live, self._live = self._live, []
        for resp in live:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def last_skew(self) -> float:
        """Seconds our clock is ahead of the Hub's, from ``X-Parley-Time``.

        Clients must not trust their own clock for ordering (SPEC 1.2); this is
        purely so ``parley doctor`` can report the skew to a human.
        """
        with self._lock:
            return self._skew

    # -- signing ------------------------------------------------------------
    def _stamp(self) -> Tuple[str, str]:
        """One request's ``(timestamp, nonce)``.

        Drawn once per attempt and then threaded through the seal AAD, the
        signature and the headers. Drawing them twice is exactly the bug that
        made sealed mode fail: the AAD then bound a timestamp and nonce the Hub
        never saw, and every sealed body failed its Poly1305 tag.
        """
        return str(int(time.time())), crypto.new_nonce_hex()

    def _auth_headers(self, method: str, path: str, outer_body: bytes,
                      stamp: Optional[Tuple[str, str]] = None) -> Dict[str, str]:
        ts, nonce = stamp if stamp is not None else self._stamp()
        sts = crypto.string_to_sign(
            method.upper(), path, outer_body, ts, nonce, self.session, self.agent_id
        )
        sig = crypto.sign(self._key, sts)
        headers = {
            "X-Parley-Version": WIRE_VERSION,
            "X-Parley-Session": self.session,
            "X-Parley-Agent": self.agent_id,
            "X-Parley-Timestamp": ts,
            "X-Parley-Nonce": nonce,
            "Authorization": "Parley-HMAC-SHA256 " + sig,
        }
        if self._sealed:
            headers["X-Parley-Seal"] = "v1"
        return headers

    def _seal_aad(self, method: str, path: str, stamp: Tuple[str, str], *,
                  response: bool) -> bytes:
        """The SPEC 3.6 binding for this request, or for its response.

        ``self.agent_id`` is the literal ``"enroll"`` while enrolling, which is
        what the Hub reads out of ``X-Parley-Agent`` -- so the two sides agree
        even though the body is sealed under ``seal_key`` and signed under
        ``enroll_key``.
        """
        ts, nonce = stamp
        return crypto.seal_aad(method, path, ts, nonce, self.session, self.agent_id,
                               response=response)

    def _seal_request(self, method: str, path: str, plain: bytes,
                      stamp: Tuple[str, str]) -> bytes:
        """Seal a request body under the §3.6 AAD for this exact request.

        The §3.3 signature is computed afterwards, over these sealed bytes
        (``_auth_headers`` is called with the result), so the Hub verifies the
        signature before it decrypts anything -- which is the order §3.6 makes
        normative. The AAD itself contains no body hash, so nothing is circular.
        """
        return crypto.seal(self._seal_key, plain,
                           self._seal_aad(method, path, stamp, response=False))

    def _unseal_response(self, body: bytes, method: str, path: str,
                         stamp: Tuple[str, str]) -> bytes:
        """Decrypt a sealed response body.

        Exactly one AAD: ``PARLEY/1-SEAL-RESPONSE`` over *this request's*
        timestamp and nonce, which is what ties the answer to the question.
        SPEC 3.6 forbids trying several and taking whichever authenticates, so a
        failure here is reported rather than probed around.
        """
        if not body:
            return body
        try:
            return crypto.unseal(self._seal_key, body,
                                 self._seal_aad(method, path, stamp, response=True))
        except Exception as exc:  # noqa: BLE001 - surfaced as a transport error
            raise errors.TransportError(
                "could not decrypt sealed response body",
                detail={"cause": str(exc)},
                hint="Hub and client disagree on the SPEC 3.6 response AAD "
                "(PARLEY/1-SEAL-RESPONSE, method, path, the request's timestamp "
                "and nonce, session, agent), or the watchword differs.",
            )

    # -- requests -----------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        *,
        headers: Optional[dict] = None,
        json_body: Any = None,
        max_attempts: int = 4,
        retry_on_ambiguous: bool = True,
        timeout: Optional[float] = None,
    ) -> Tuple[int, dict, bytes]:
        """Perform one signed request, retrying transient failures.

        Returns ``(status, response_headers, response_body)``. HTTP error
        statuses are *returned*, not raised - the caller decides whether a 404
        is a problem. Only a failure to get any response at all after the retry
        budget raises (:class:`TransportError`).

        ``body`` is sent unchanged on every attempt. That is what makes a retry
        idempotent: a client event body embeds its ``event.id``, so a redelivery
        after an ambiguous failure is deduplicated by the Hub (SPEC 5.2).

        ``retry_on_ambiguous=False`` restricts retries to failures that happened
        before the request could have been delivered (DNS, connection refused).
        Use it for genuinely non-idempotent calls such as ``POST /v1/enroll``.
        """
        if json_body is not None:
            if body is not None:
                raise ValueError("pass either body or json_body, not both")
            body = dumps(json_body).encode("utf-8")
        plain = body if body is not None else b""

        extra = dict(headers or {})
        url = self.hub_url + path
        attempt = 0
        last_error: Optional[Exception] = None

        while attempt < max_attempts:
            if self._closed.is_set() and attempt > 0:
                break
            attempt += 1
            try:
                # One timestamp and nonce per attempt, shared by the seal AAD,
                # the signature and the headers (SPEC 3.6: the AAD carries the
                # request's timestamp and nonce, and the response reuses them).
                stamp = self._stamp()
                if self._sealed and plain:
                    outer = self._seal_request(method, path, plain, stamp)
                else:
                    outer = plain
                req_headers = self._auth_headers(method, path, outer, stamp)
                req_headers.update(extra)
                if plain and "Content-Type" not in req_headers:
                    req_headers["Content-Type"] = "application/json; charset=utf-8"

                status, resp_headers, resp_body = self._raw(
                    method, url, outer, req_headers, timeout if timeout else self.timeout
                )
                if self._sealed and resp_headers.get("x-parley-seal") and resp_body:
                    resp_body = self._unseal_response(resp_body, method, path, stamp)

                if status == 429:
                    delay = self._retry_after(resp_headers)
                    if attempt < max_attempts:
                        log.warning("rate limited by hub on %s %s; waiting %.1fs", method, path, delay)
                        if self._closed.wait(delay):
                            break
                        continue
                if status in (502, 503, 504) and attempt < max_attempts:
                    self._backoff(attempt)
                    continue
                return status, resp_headers, resp_body

            except _Ambiguous as exc:
                last_error = exc.cause
                if not retry_on_ambiguous:
                    break
                if attempt >= max_attempts:
                    break
                log.debug("transport: %s %s failed (%s); retrying", method, path, exc.cause)
                self._backoff(attempt)
            except _NotDelivered as exc:
                last_error = exc.cause
                if attempt >= max_attempts:
                    break
                log.debug("transport: %s %s could not connect (%s); retrying", method, path, exc.cause)
                self._backoff(attempt)

        raise errors.TransportError(
            "cannot reach hub at {0}{1}".format(self.hub_url, path),
            detail={"cause": str(last_error) if last_error else "unknown", "attempts": attempt},
            hint="Check the Hub is running and the URL/tunnel is up (`parley doctor`).",
        )

    def _raw(
        self,
        method: str,
        url: str,
        body: bytes,
        headers: Dict[str, str],
        timeout: float,
    ) -> Tuple[int, dict, bytes]:
        req = urllib.request.Request(url, data=body if body else None, method=method.upper())
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.getcode()
                resp_headers = _header_dict(resp.headers)
                data = resp.read()
                if resp_headers.get("content-encoding") == "gzip":
                    data = gzip.decompress(data)
        except urllib.error.HTTPError as exc:
            # An HTTP error status is still a complete answer from the Hub.
            resp_headers = _header_dict(exc.headers) if exc.headers is not None else {}
            try:
                data = exc.read()
            except Exception:  # noqa: BLE001
                data = b""
            if resp_headers.get("content-encoding") == "gzip" and data:
                try:
                    data = gzip.decompress(data)
                except OSError:
                    pass
            self._note_time(resp_headers)
            return exc.code, resp_headers, data
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
                raise _NotDelivered(exc)
            raise _Ambiguous(exc)
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise _Ambiguous(exc)

        self._note_time(resp_headers)
        return status, resp_headers, data

    def _note_time(self, headers: Dict[str, str]) -> None:
        raw = headers.get("x-parley-time")
        if not raw:
            return
        try:
            hub_t = parse_rfc3339(raw)
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            self._skew = time.time() - hub_t

    @staticmethod
    def _retry_after(headers: Dict[str, str]) -> float:
        raw = headers.get("retry-after", "")
        try:
            return max(0.1, min(60.0, float(raw)))
        except (TypeError, ValueError):
            return 2.0

    def _note_connected(self) -> None:
        """Record a successful (re)connection of the live feed."""
        self.connections += 1
        if self.connections <= 1:
            return
        callback = self.on_reconnect
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:  # noqa: BLE001 - a bad callback must not kill the stream
            log.warning("on_reconnect callback failed: %s", exc)

    def _backoff(self, attempt: int) -> None:
        """Exponential backoff with *full* jitter (SPEC 5.2).

        Full jitter (uniform in ``[0, cap]``) rather than equal jitter matters
        when several agents are reconnecting to a Hub that just restarted: it
        spreads them out instead of synchronising them into a thundering herd.
        """
        delay = min(30.0, 0.5 * (2 ** max(0, attempt - 1))) * random.random()
        self._closed.wait(delay)

    # -- JSON helpers -------------------------------------------------------
    def get_json(self, path: str) -> dict:
        status, _headers, body = self.request("GET", path)
        if status >= 400:
            raise error_from_response(status, body)
        return self._json(body)

    def post_json(self, path: str, obj: Any) -> dict:
        status, _headers, body = self.request("POST", path, json_body=obj)
        if status >= 400:
            raise error_from_response(status, body)
        return self._json(body)

    @staticmethod
    def _json(body: bytes) -> dict:
        if not body:
            return {}
        parsed = loads(body)
        if not isinstance(parsed, dict):
            raise errors.TransportError(
                "hub returned a non-object JSON response",
                hint="This usually means a proxy replaced the response body.",
            )
        return parsed

    # -- stream -------------------------------------------------------------
    def stream(self, since: int, types: Optional[List[str]] = None) -> Iterator[dict]:
        """Yield every event after ``since``, forever, in strict ``seq`` order.

        Contract honoured here, because the rest of the client depends on it:

        * **Never raises on a transient failure.** Network errors reconnect with
          full-jitter backoff. Only a caller-side ``close()`` ends the generator.
        * **No gaps.** If a reconnect or a dropped frame leaves a hole, the hole
          is filled with ``GET /v1/events?since=...`` before the newer event is
          yielded, so the consumer's view of the log is contiguous.
        * **No duplicates.** Anything at or below the highest ``seq`` already
          yielded is dropped, which also makes the Hub's ``since`` replay on
          reconnect harmless.
        * **Degrades.** After two consecutive SSE failures it switches to
          long-poll, and periodically tries SSE again rather than staying
          degraded for the whole session.
        """
        last_seq = int(since)
        sse_failures = 0
        use_sse = not self._sealed
        sse_retry_at = 0.0
        attempt = 0
        # Long-poll makes a fresh HTTP request every round, so "a request
        # succeeded" is not by itself a reconnect. Only a success that follows a
        # failure (or a mode switch) counts.
        poll_broken = True

        while not self._closed.is_set():
            if not use_sse and not self._sealed and time.monotonic() >= sse_retry_at:
                log.info("stream: retrying SSE after long-poll fallback")
                use_sse = True
                sse_failures = 0

            self.mode = "sse" if use_sse else "poll"
            progressed = False
            try:
                if use_sse:
                    source = self._iter_sse(last_seq, types)
                else:
                    source = self._iter_poll(last_seq, types)
                    if poll_broken:
                        self._note_connected()
                        poll_broken = False
                for event in source:
                    if self._closed.is_set():
                        break
                    for out in self._in_order(event, last_seq, types):
                        seq = out.get("seq")
                        if isinstance(seq, int) and seq > last_seq:
                            last_seq = seq
                        progressed = True
                        attempt = 0
                        sse_failures = 0
                        self.last_event_at = time.monotonic()
                        yield out
                # The iterator ended without raising: the Hub closed the
                # response cleanly (restart, or a long-poll round finishing).
                # That is normal; loop round and reconnect.
                if not use_sse:
                    # Long-poll already blocked server-side for `wait` seconds,
                    # so there is nothing to back off from when it returns.
                    continue
            except GeneratorExit:
                raise
            except Exception as exc:  # noqa: BLE001 - resilience is the whole point
                if self._closed.is_set():
                    break
                if use_sse:
                    sse_failures += 1
                else:
                    poll_broken = True
                log.warning(
                    "stream: %s connection lost at seq %d (%s); reconnecting",
                    "sse" if use_sse else "long-poll",
                    last_seq,
                    exc,
                )

            if use_sse and sse_failures >= SSE_FAILURES_BEFORE_FALLBACK:
                log.warning(
                    "stream: SSE failed %d times in a row; falling back to long-poll",
                    sse_failures,
                )
                use_sse = False
                poll_broken = True
                sse_retry_at = time.monotonic() + SSE_RETRY_INTERVAL_S

            if not progressed:
                attempt += 1
                self._backoff(attempt)

    # -- stream internals ---------------------------------------------------
    def _in_order(
        self, event: dict, last_seq: int, types: Optional[List[str]]
    ) -> Iterable[dict]:
        """Yield ``event`` plus any events missing between ``last_seq`` and it."""
        seq = event.get("seq")
        if not isinstance(seq, int):
            # An event without a Hub-assigned seq cannot be ordered. Pass it
            # through (it is still information) but do not let it move the
            # resume point, or we would skip real events on reconnect.
            yield event
            return
        if seq <= last_seq:
            return
        if seq > last_seq + 1:
            log.info("stream: gap detected (have %d, got %d); backfilling", last_seq, seq)
            cursor = last_seq
            while cursor < seq - 1:
                batch = self._fetch_range(cursor, min(GAP_FETCH_LIMIT, seq - 1 - cursor), types)
                if not batch:
                    log.warning(
                        "stream: hub could not backfill %d..%d; continuing with a gap",
                        cursor + 1,
                        seq - 1,
                    )
                    break
                for item in batch:
                    item_seq = item.get("seq")
                    if isinstance(item_seq, int) and item_seq > cursor:
                        cursor = item_seq
                        if item_seq < seq:
                            yield item
                if cursor >= seq - 1:
                    break
        yield event

    def _fetch_range(self, since: int, limit: int, types: Optional[List[str]]) -> List[dict]:
        path = "/v1/events?since={0}&limit={1}".format(int(since), int(max(1, limit)))
        if types:
            path += "&types=" + urllib.parse.quote(",".join(types), safe=",")
        try:
            status, _h, body = self.request("GET", path, max_attempts=2)
            if status >= 400:
                log.debug("stream: backfill returned HTTP %d", status)
                return []
            payload = self._json(body)
        except Exception as exc:  # noqa: BLE001 - backfill is best-effort
            log.debug("stream: backfill failed (%s)", exc)
            return []
        return _events_of(payload)

    def _iter_sse(self, since: int, types: Optional[List[str]]) -> Iterator[dict]:
        if self._sealed:
            # SPEC 3.6: there is no sealed framing for text/event-stream, so a
            # sealed client must not open one -- the Hub answers 422 if it does.
            # `stream()` already chooses long-poll; this is the backstop that
            # keeps a future caller from reaching SSE by another route.
            raise errors.TransportError(
                "a sealed client must not use /v1/stream",
                hint="Sealed mode long-polls GET /v1/events?wait= instead (SPEC 3.6).",
            )
        path = "/v1/stream?since={0}".format(int(since))
        if types:
            path += "&types=" + urllib.parse.quote(",".join(types), safe=",")
        headers = self._auth_headers("GET", path, b"")
        headers["Accept"] = "text/event-stream"
        headers["Cache-Control"] = "no-store"
        req = urllib.request.Request(self.hub_url + path, method="GET")
        for k, v in headers.items():
            req.add_header(k, v)

        resp = urllib.request.urlopen(req, timeout=STREAM_TIMEOUT_S)
        with self._lock:
            self._live.append(resp)
        try:
            status = resp.getcode()
            resp_headers = _header_dict(resp.headers)
            self._note_time(resp_headers)
            if status != 200:
                raise errors.TransportError("stream returned HTTP {0}".format(status))
            ctype = resp_headers.get("content-type", "")
            if "text/event-stream" not in ctype:
                # A proxy that rewrote the response, or an auth failure page.
                raise errors.TransportError(
                    "stream content-type is {0!r}, not text/event-stream".format(ctype)
                )
            self._note_connected()
            parser = SSEParser()
            read1 = getattr(resp, "read1", None)
            while not self._closed.is_set():
                chunk = read1(16384) if read1 is not None else resp.read(1)
                if not chunk:
                    break
                for frame in parser.feed(chunk):
                    event = _frame_to_event(frame)
                    if event is not None:
                        yield event
            for frame in parser.close():
                event = _frame_to_event(frame)
                if event is not None:
                    yield event
        finally:
            with self._lock:
                try:
                    self._live.remove(resp)
                except ValueError:
                    pass
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    def _iter_poll(self, since: int, types: Optional[List[str]]) -> List[dict]:
        """One long-poll round. Returns a list (not a generator) deliberately:
        the caller needs the request to have *happened* before it decides the
        connection is healthy again."""
        path = "/v1/events?since={0}&limit={1}&wait={2}".format(
            int(since), GAP_FETCH_LIMIT, LONGPOLL_WAIT_S
        )
        if types:
            path += "&types=" + urllib.parse.quote(",".join(types), safe=",")
        status, _h, body = self.request(
            "GET",
            path,
            max_attempts=1,
            timeout=LONGPOLL_WAIT_S + self.timeout,
        )
        if status >= 400:
            raise error_from_response(status, body)
        return _events_of(self._json(body))

    # -- blobs --------------------------------------------------------------
    def has_blob(self, blob_hash: str) -> bool:
        status, _h, _b = self.request("HEAD", "/v1/blobs/" + blob_hash, max_attempts=2)
        return status == 200

    def put_blob(self, data: bytes, blob_hash: Optional[str] = None) -> str:
        """Upload a blob. Content-addressed, therefore idempotent by construction."""
        if blob_hash is None:
            blob_hash = "sha256:" + sha256_hex(data)
        payload = data
        headers = {
            "X-Parley-Blob-SHA256": blob_hash,
            "Content-Type": "application/octet-stream",
        }
        if self._sealed:
            payload = crypto.seal_frames(self._seal_key, data, blob_hash)
            headers["X-Parley-Seal"] = "v1"
        elif len(data) > 4096:
            payload = gzip.compress(data, 6)
            headers["Content-Encoding"] = "gzip"
        status, _h, body = self._blob_request("POST", "/v1/blobs", payload, headers)
        if status >= 400:
            raise error_from_response(status, body)
        return blob_hash

    def get_blob(self, blob_hash: str) -> bytes:
        headers = {"Accept-Encoding": "gzip"}
        status, _resp_headers, body = self._blob_request("GET", "/v1/blobs/" + blob_hash, None, headers)
        if status >= 400:
            raise error_from_response(status, body)
        # NB: `_raw` has already undone any Content-Encoding. Decompressing here
        # as well would corrupt every blob whose first bytes happen not to be a
        # gzip header - which is to say, all of them.
        if self._sealed:
            body = crypto.unseal_frames(self._seal_key, body, blob_hash)
        got = "sha256:" + sha256_hex(body)
        if got != blob_hash:
            raise errors.TransportError(
                "blob content does not match its hash",
                detail={"wanted": blob_hash, "got": got},
                hint="The Hub or an intermediary corrupted the transfer; retry.",
            )
        return body

    def _blob_request(
        self, method: str, path: str, payload: Optional[bytes], headers: Dict[str, str]
    ) -> Tuple[int, dict, bytes]:
        """Blob bodies are raw, never JSON, and are already sealed frame-wise.

        They therefore bypass :meth:`request`'s body sealing (which would double
        encrypt) while still getting signing, retries and backoff.
        """
        url = self.hub_url + path
        body = payload or b""
        attempt = 0
        last: Optional[Exception] = None
        while attempt < 4:
            attempt += 1
            try:
                req_headers = self._auth_headers(method, path, body)
                if self._sealed:
                    # SPEC 3.6 pins the header value to "v1" in both cases; what
                    # selects per-frame sealing over a single envelope is the
                    # /v1/blobs route, not a bespoke header value.
                    req_headers["X-Parley-Seal"] = "v1"
                req_headers.update(headers)
                status, resp_headers, data = self._raw(
                    method, url, body, req_headers, max(self.timeout, 120.0)
                )
                if status == 429 and attempt < 4:
                    if self._closed.wait(self._retry_after(resp_headers)):
                        break
                    continue
                if status in (502, 503, 504) and attempt < 4:
                    self._backoff(attempt)
                    continue
                return status, resp_headers, data
            except (_Ambiguous, _NotDelivered) as exc:
                last = exc.cause
                if attempt >= 4:
                    break
                self._backoff(attempt)
        raise errors.TransportError(
            "blob transfer failed against {0}".format(self.hub_url),
            detail={"cause": str(last) if last else "unknown", "path": path},
            hint="Check Hub reachability and max_blob_bytes.",
        )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


class _Ambiguous(Exception):
    """The request may or may not have reached the Hub."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


class _NotDelivered(Exception):
    """The request provably never reached the Hub (no connection established)."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


def _header_dict(headers: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        items = headers.items()
    except AttributeError:  # pragma: no cover
        return out
    for k, v in items:
        out[str(k).lower()] = str(v)
    return out


def _frame_to_event(frame: Dict[str, Any]) -> Optional[dict]:
    """Turn one SSE frame into an event dict, or ``None`` if it is not one."""
    name = frame.get("event") or "message"
    data = frame.get("data") or ""
    if name not in ("parley", "message", "event"):
        # Hub-private frame kinds (e.g. "ping" as a named event) are ignored
        # rather than treated as a protocol error - forward compatibility.
        log.debug("sse: ignoring frame of type %r", name)
        return None
    if not data.strip():
        return None
    try:
        parsed = loads(data)
    except Exception as exc:  # noqa: BLE001
        log.warning("sse: frame payload is not JSON (%s); dropping frame", exc)
        return None
    if not isinstance(parsed, dict):
        return None
    if "seq" not in parsed and "id" not in parsed and "type" not in parsed:
        return None
    # The SSE `id:` field is the seq (SPEC 5.1); trust the body first but fall
    # back to the frame id if a Hub ever omits it.
    if not isinstance(parsed.get("seq"), int) and frame.get("id"):
        try:
            parsed["seq"] = int(frame["id"])
        except (TypeError, ValueError):
            pass
    return parsed


def _events_of(payload: dict) -> List[dict]:
    """Extract the event list from a ``/v1/events`` response, liberally.

    SPEC 5 does not pin the exact envelope key, so accept the plausible shapes
    rather than breaking if the Hub implementation picked a different one.
    """
    for key in ("events", "items", "data", "log"):
        value = payload.get(key)
        if isinstance(value, list):
            return [e for e in value if isinstance(e, dict)]
    if isinstance(payload.get("event"), dict):
        return [payload["event"]]
    return []
