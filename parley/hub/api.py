"""Authentication (SPEC §3) and the HTTP router (SPEC §5).

``handle`` is deliberately "pure-ish": it takes the request and returns
``(status, headers, body)``.  Everything that needs the raw socket -- only
``GET /v1/stream`` -- is handled in ``server.py``.  Keeping the router free of
socket code is what makes it directly testable without binding a port.
"""

from __future__ import annotations

import binascii
import gzip
import hmac
import io
import logging
import mimetypes
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from .. import crypto, ids
from ..errors import (
    BadEvent,
    BadJson,
    BadPath,
    BadRequest,
    BadSignature,
    EnrollClosed,
    HostTokenRequired,
    NoSuchAgent,
    NoSuchBlob,
    NoSuchSession,
    ParleyError,
    PendingApproval,
    RateLimited,
    ReadOnlyToken,
    ReplayedNonce,
    Revoked,
    ShuttingDown,
    StaleTimestamp,
    TooLarge,
    UnknownAgent,
)
from ..jsonutil import dumps, loads, now_rfc3339, sha256_hex
from ..version import WIRE_VERSION
from .store import normalise_blob_hash, wire_blob_hash

log = logging.getLogger("parley.hub.api")

JSON_CT = "application/json; charset=utf-8"
AUTH_SCHEME = "Parley-HMAC-SHA256"
#: Alternative host-token carrier; the Deck sends this alongside the header.
HOST_AUTH_SCHEME = "Parley-Host"
MAX_BATCH = 64
MAX_EVENTS_LIMIT = 2000
MAX_WAIT_S = 30.0
DEFAULT_VIEWER_TTL_S = 12 * 3600
#: SPEC §8.  Note that this blocks inline <script>/<style>: the Deck must ship
#: its CSS and JS as separate same-origin files.  Overridable via policy so a
#: deployment behind a proxy can adjust it without patching the Hub.
DEFAULT_DECK_CSP = "default-src 'self'; connect-src 'self'; img-src 'self' data:"

#: Extra MIME types the stdlib table gets wrong or misses on some platforms.
_MIME_OVERRIDES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ico": "image/x-icon",
    ".map": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
}

_PLACEHOLDER_DECK = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Parley Hub</title></head>
<body>
<h1>Parley Hub is running</h1>
<p>The Deck has not been installed into this build. The Hub itself is healthy and
the API is serving normally.</p>
<ul>
  <li><code>GET /v1/hello</code> &mdash; unauthenticated discovery</li>
  <li><code>GET /v1/state?vt=&lt;viewer token&gt;</code> &mdash; the snapshot the Deck paints from</li>
  <li><code>GET /v1/stream?vt=&lt;viewer token&gt;</code> &mdash; the live SSE feed</li>
</ul>
<p>Drop the Deck's <code>index.html</code> and its assets into
<code>parley/hub/deck/</code> and reload.</p>
</body></html>
"""


# --------------------------------------------------------------------- errors


class NotFound(ParleyError):
    """No such route. Distinct from the §12 404 codes, which are about objects."""

    code = "not_found"
    http_status = 404


class MethodNotAllowed(ParleyError):
    code = "method_not_allowed"
    http_status = 405


# -------------------------------------------------------------------- helpers


def lower_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        items = headers.items()
    except AttributeError:  # pragma: no cover - defensive
        items = []
    for k, v in items:
        out[str(k).lower()] = v if isinstance(v, str) else str(v)
    return out


def split_path(raw: str) -> Tuple[str, Dict[str, str]]:
    """Split a request target into its path and a flat first-value query dict."""
    parts = urlsplit(raw)
    query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
    return parts.path or "/", query


def _int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _types_param(value: str) -> "Optional[List[str]]":
    if not value:
        return None
    out = [p.strip() for p in value.split(",") if p.strip()]
    return out or None



#: Query parameters that carry a credential and must never reach a log line.
_SECRET_QUERY_KEYS = ("vt", "ht", "token", "invite")


def redact_path(raw: str) -> str:
    """A request target safe to log.

    Viewer and host tokens travel in the query string (a browser's EventSource
    cannot set headers), so the raw path is credential-bearing.  SPEC §3.7 is
    explicit that a token must never appear in a log line.
    """
    if "?" not in raw:
        return raw
    head, _, tail = raw.partition("?")
    parts = []
    for chunk in tail.split("&"):
        key = chunk.split("=", 1)[0]
        parts.append(key + "=<redacted>" if key in _SECRET_QUERY_KEYS else chunk)
    return head + "?" + "&".join(parts)



#: Exact and prefix routes this Hub serves (SPEC §5).  Checked before
#: authentication so a typo in a URL answers "no such endpoint" rather than
#: "your credentials are bad" -- the route table is public, so nothing leaks.
_EXACT_ROUTES = frozenset({
    "/v1/hello", "/v1/enroll", "/v1/events", "/v1/stream", "/v1/state",
    "/v1/index", "/v1/ledger", "/v1/blobs", "/v1/me",
})
_PREFIX_ROUTES = ("/v1/blobs/", "/v1/admin/")


def _known_route(path: str) -> bool:
    return path in _EXACT_ROUTES or any(path.startswith(p) for p in _PREFIX_ROUTES)


def base_headers(hub) -> Dict[str, str]:
    """``X-Parley-Time``/``X-Parley-Seq`` go on every response (SPEC §5)."""
    return {
        "X-Parley-Time": now_rfc3339(),
        "X-Parley-Seq": str(hub.store.head_seq()),
        "X-Parley-Version": WIRE_VERSION,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }


def json_response(hub, status: int, obj: Any, extra: "Optional[Dict[str, str]]" = None):
    body = dumps(obj).encode("utf-8")
    headers = base_headers(hub)
    headers["Content-Type"] = JSON_CT
    headers["Cache-Control"] = "no-store"
    if extra:
        headers.update(extra)
    return status, headers, body


def error_response(hub, err: ParleyError):
    extra: Dict[str, str] = {}
    retry = getattr(err, "retry_after", None)
    if retry:
        extra["Retry-After"] = str(int(retry))
    return json_response(hub, int(getattr(err, "http_status", 500) or 500), err.to_dict(), extra)


def _body_json(body: bytes) -> dict:
    if not body:
        return {}
    try:
        obj = loads(body)
    except ParleyError:
        raise
    except Exception as exc:
        raise BadJson("request body is not valid JSON", hint=str(exc)[:200])
    if not isinstance(obj, dict):
        raise BadRequest(
            "request body must be a JSON object",
            hint="Send {...}, not a list or a bare value.",
        )
    return obj


# ------------------------------------------------------------- authentication


def authenticate(
    store,
    config,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
) -> dict:
    """Verify one request's credentials.

    Returns ``{"kind": "agent"|"enroll"|"viewer"|"host", "agent": dict|None}``.
    Raises a :class:`ParleyError` otherwise.  Never includes key material in the
    returned dict beyond what the caller already has to hold, and never puts a
    secret into an exception message.
    """
    hdr = lower_headers(headers)
    _, query = split_path(path)
    policy = dict(getattr(config, "policy", {}) or {})
    skew_s = float(policy.get("skew_s", 300) or 300)
    nonce_ttl = float(policy.get("nonce_ttl_s", 600) or 600)

    # 1. Host token -- header, `Authorization: Parley-Host <tok>`, or `?ht=`
    #    (the Deck is a browser and cannot set headers on a plain navigation).
    host_token = hdr.get("x-parley-host-token") or query.get("ht") or ""
    if not host_token:
        authz_probe = hdr.get("authorization", "")
        if authz_probe.startswith(HOST_AUTH_SCHEME + " "):
            host_token = authz_probe[len(HOST_AUTH_SCHEME) + 1:].strip()
    if host_token:
        expected = str(getattr(config, "host_token", "") or "")
        if expected and hmac.compare_digest(host_token, expected):
            return {"kind": "host", "agent": None}
        raise HostTokenRequired(
            "host token rejected",
            hint="Run `parley invite --reveal` on the hosting machine to see the "
            "current host token; it is printed once at `parley init`.",
        )

    # 2. Viewer token (read-only, SPEC §3.7).
    vt = query.get("vt") or hdr.get("x-parley-viewer-token") or ""
    if vt:
        if store.check_viewer_token(vt):
            return {"kind": "viewer", "agent": None}
        raise ReadOnlyToken(
            "viewer token is unknown, revoked or expired",
            hint="Ask the host for a fresh Deck link; viewer tokens expire (12 h by default).",
        )

    # 3. Full §3.3 HMAC.
    authz = hdr.get("authorization", "")
    if not authz:
        raise UnknownAgent(
            "no credentials on this request",
            hint="Sign the request per SPEC §3.3, or pass ?vt=<viewer token> for read-only access.",
        )
    scheme, _, sig_hex = authz.partition(" ")
    if scheme != AUTH_SCHEME or not sig_hex.strip():
        raise BadSignature(
            "unsupported Authorization scheme",
            hint="Use `Authorization: " + AUTH_SCHEME + " <hex signature>`.",
        )
    sig_hex = sig_hex.strip()

    version = hdr.get("x-parley-version", "")
    if version and version != WIRE_VERSION:
        raise BadRequest(
            "wire version mismatch",
            detail={"expected": WIRE_VERSION, "got": version},
            hint="Upgrade the client or the Hub so both speak " + WIRE_VERSION + ".",
        )

    session = hdr.get("x-parley-session", "")
    if not session or session != getattr(config, "session", ""):
        raise NoSuchSession(
            "this Hub is not hosting that session",
            detail={"session": session},
            hint="Check the Hub URL; a restarted Hub with a fresh state dir has a new session id.",
        )

    agent_id = hdr.get("x-parley-agent", "")
    ts_raw = hdr.get("x-parley-timestamp", "")
    nonce = hdr.get("x-parley-nonce", "")
    if not agent_id or not ts_raw or not nonce:
        raise BadRequest(
            "missing authentication headers",
            hint="X-Parley-Agent, X-Parley-Timestamp and X-Parley-Nonce are all required.",
        )
    if len(nonce) < 8 or len(nonce) > 64:
        raise BadRequest("malformed nonce", hint="Use 16 hex characters, fresh per request.")

    try:
        ts_int = int(ts_raw)
    except ValueError:
        # Reported as a 401 like any other timestamp problem: from the caller's
        # point of view the credential is what failed, not the request shape.
        raise StaleTimestamp(
            "X-Parley-Timestamp must be integer Unix seconds",
            hint="Send int(time.time()), not an RFC 3339 string.",
        )
    now = time.time()
    skew = abs(now - ts_int)
    if skew > skew_s:
        raise StaleTimestamp(
            "request timestamp is outside the accepted window",
            detail={"skew_s": round(skew, 1), "max_skew_s": skew_s},
            hint="Your clock is off by about %d s. Sync it (NTP), or read the Hub's "
            "X-Parley-Time header and apply the offset." % int(skew),
        )

    # Resolve the key.
    agent_rec: "Optional[dict]" = None
    if agent_id == "enroll":
        try:
            root_key = bytes.fromhex(getattr(config, "root_key_hex", "") or "")
        except ValueError:
            root_key = b""
        if not root_key:
            raise EnrollClosed(
                "this Hub has no enrolment key configured",
                hint="Re-run `parley init`; the Hub state directory looks incomplete.",
            )
        key = crypto.enroll_key(root_key)
        kind = "enroll"
    else:
        agent_rec = store.get_agent(agent_id)
        if agent_rec is None:
            raise UnknownAgent(
                "no such agent in this parley",
                detail={"agent": agent_id},
                hint="Enrol again with `parley join`; the Hub may have been reset.",
            )
        status = str(agent_rec.get("status", "active"))
        if status == "revoked":
            raise Revoked(
                "this agent has been revoked by the host",
                hint="Ask the host to re-invite you, then `parley join` with the new watchword.",
            )
        key = store.agent_key(agent_id) or b""
        if not key:
            raise UnknownAgent(
                "agent record has no usable key",
                hint="Enrol again with `parley join`.",
            )
        kind = "agent"

    sts = crypto.string_to_sign(method.upper(), path, body, str(ts_raw), nonce, session, agent_id)
    if not crypto.verify(key, sts, sig_hex):
        raise BadSignature(
            "signature does not verify",
            hint="string_to_sign is the 8 fields of SPEC §3.3 joined by '\\n', with the path "
            "exactly as sent (query included) and sha256 of the raw body.",
        )

    # Burn the nonce only after the signature verifies, so an unauthenticated
    # attacker cannot exhaust a victim's nonce space by guessing values.
    if store.seen_nonce(agent_id, nonce, float(ts_int), nonce_ttl):
        raise ReplayedNonce(
            "this nonce was already used",
            hint="Generate a fresh nonce for every request (secrets.token_hex(8)).",
        )

    return {"kind": kind, "agent": agent_rec}


# ------------------------------------------------------- sealed mode (SPEC §3.6)


def _seal_aad_candidates(method: str, raw_path: str, hdr: Dict[str, str],
                         session: str, outer: bytes) -> List[bytes]:
    """AADs we will try when decrypting a sealed request body.

    SPEC §3.6 says the AAD is "the string_to_sign of §3.3", but §3.3's string
    embeds ``sha256(body)`` -- and the Hub cannot hash the plaintext before it has
    decrypted it.  Read literally the requirement is circular, so the Hub accepts
    the three non-circular readings and lets the Poly1305 tag pick the right one.
    A wrong AAD simply fails authentication, so trying several is not a weakening.

    The canonical form (first, and the one the Hub uses for responses' sibling
    computation) replaces the body hash with the hash of an empty body: it is
    computable identically on both sides and still binds the ciphertext to
    method, path, timestamp, nonce and identity, which is §3.6's stated purpose.
    """
    ts = hdr.get("x-parley-timestamp", "")
    nonce = hdr.get("x-parley-nonce", "")
    agent = hdr.get("x-parley-agent", "")
    return [
        crypto.string_to_sign(method, raw_path, b"", ts, nonce, session, agent),
        crypto.string_to_sign(method, raw_path, outer, ts, nonce, session, agent),
        b"",
    ]


def _unseal_request_body(hub, method: str, raw_path: str, hdr: Dict[str, str],
                         body: bytes) -> Tuple[bytes, bytes]:
    """-> (plaintext, the seal key that worked)."""
    session = str(getattr(hub.config, "session", ""))
    candidates = _seal_aad_candidates(method, raw_path, hdr, session, body)
    keys = hub.seal_keys()
    if not keys:
        raise BadRequest(
            "this Hub cannot decrypt sealed bodies",
            hint="The Hub state directory has no root key; re-run `parley init`.",
        )
    for key in keys:
        for aad in candidates:
            try:
                return crypto.unseal(key, body, aad), key
            except Exception:  # noqa: BLE001 - a tag failure is the probe result
                continue
    raise BadRequest(
        "could not decrypt the sealed request body",
        hint="Check that the client and the Hub derived seal_key from the same "
        "watchword. If the host rotated the watchword, re-run `parley join`.",
    )


def handle(hub, method: str, path: str, headers: Mapping[str, str], body: bytes):
    """Route one request.  Returns ``(status, headers, body_bytes)``."""
    seal: Dict[str, Any] = {}
    try:
        status, out_headers, out_body = _route(hub, method.upper(), path, headers, body or b"", seal)
        return _maybe_seal(seal, status, out_headers, out_body)
    except ParleyError as err:
        safe = redact_path(path)
        if err.http_status >= 500:
            log.error("%s %s -> %s: %s", method, safe, err.code, err)
        else:
            log.info("%s %s -> %d %s", method, safe, err.http_status, err.code)
        return _maybe_seal(seal, *error_response(hub, err))
    except Exception:
        log.exception("unhandled error serving %s %s", method, redact_path(path))
        return _maybe_seal(
            seal,
            *error_response(
                hub,
                ParleyError(
                    "internal hub error",
                    hint="This is a bug. The Hub logged a traceback; please report it.",
                ),
            ),
        )


def _maybe_seal(seal: Dict[str, Any], status: int, headers: Dict[str, str], body: bytes):
    """Encrypt a response body when the request arrived sealed.

    The response AAD is empty: SPEC §3.6 specifies an AAD for the *request* and
    is silent about the response, and an empty AAD is the one value both ends can
    agree on without another round of circular derivation.  Confidentiality and
    integrity still come from the AEAD itself.
    """
    if not seal.get("on") or not body:
        return status, headers, body
    try:
        sealed_body = crypto.seal(seal["key"], body, b"")
    except Exception:
        log.exception("could not seal the response body; refusing to send it in the clear")
        return 500, headers, b""
    out = dict(headers)
    out["X-Parley-Seal"] = "v1"
    out["Content-Type"] = "application/octet-stream"
    out.pop("Content-Length", None)
    return status, out, sealed_body


def _route(hub, method: str, raw_path: str, headers: Mapping[str, str], body: bytes,
           seal: "Optional[Dict[str, Any]]" = None):
    seal = seal if seal is not None else {}
    path, query = split_path(raw_path)
    hdr = lower_headers(headers)
    peer = hdr.get("x-parley-peer-addr", "?")

    if not path.startswith("/v1/"):
        if method in ("GET", "HEAD"):
            return _serve_deck(hub, method, path)
        raise MethodNotAllowed("only GET/HEAD are served outside /v1", hint="Use /v1/* for the API.")

    if path == "/v1/hello":
        if method not in ("GET", "HEAD"):
            raise MethodNotAllowed("GET only", hint="GET /v1/hello")
        return _hello(hub)

    if not _known_route(path):
        raise NotFound(
            "no such endpoint",
            detail={"path": path},
            hint="See SPEC §5 for the endpoint table.",
        )

    if hub.shutting_down:
        raise ShuttingDown(
            "the Hub is shutting down",
            hint="Reconnect once it is back; your local log and workspace are intact.",
        )

    # Admin needs the host token specifically, so say that outright instead of
    # letting the generic "no credentials" 401 send the caller hunting.
    if path.startswith("/v1/admin/") and not _has_host_credential(hdr, query):
        raise HostTokenRequired(
            "admin actions need the host token",
            hint="Pass it as `X-Parley-Host-Token:` or `?ht=`; it is printed once at "
            "`parley init` and stored in the Hub state directory.",
        )

    auth = authenticate(hub.store, hub.config, method, raw_path, headers, body)
    kind = auth["kind"]
    agent = auth.get("agent")
    agent_id = str(agent.get("agent_id")) if agent else ""
    now = time.time()

    # Generic backstop limiter, keyed by whoever we can identify.
    rl_key = agent_id or ("vt:" + peer if kind == "viewer" else peer)
    wait = hub.limiter.check(rl_key, "request", now)
    if wait:
        raise _rate_limited(wait)

    if agent_id:
        hub.store.touch_agent(agent_id, now)
        hub.view.touch(agent_id, now)

    pending = bool(agent and agent.get("status") == "pending")

    # --- sealed mode (SPEC §3.6) ---------------------------------------------
    # Blob bodies carry their own per-frame AEAD and are handled in the blob
    # endpoints; everything else is one sealed envelope around the JSON body.
    seal_hdr = hdr.get("x-parley-seal", "")
    is_blob_path = path == "/v1/blobs" or path.startswith("/v1/blobs/")
    if seal_hdr and kind in ("agent", "enroll") and not is_blob_path and body:
        body, seal_key = _unseal_request_body(hub, method, raw_path, hdr, body)
        seal["on"] = True
        seal["key"] = seal_key
    elif (
        hub.policy.get("sealed")
        and kind in ("agent", "enroll")
        and body
        and not is_blob_path
    ):
        raise BadRequest(
            "this Hub runs in sealed mode; request bodies must be sealed",
            hint="Set X-Parley-Seal: v1 and encrypt the body with "
            "HKDF(root_key, 'parley/v1/seal') per SPEC §3.6.",
        )

    if path == "/v1/enroll":
        if method != "POST":
            raise MethodNotAllowed("POST only", hint="POST /v1/enroll")
        if kind != "enroll":
            raise BadSignature(
                "enrolment must be signed with the enrol key",
                hint="Set X-Parley-Agent: enroll and sign with HKDF(root_key, 'parley/v1/enroll').",
            )
        return _enroll(hub, body, peer)

    if kind == "enroll":
        raise UnknownAgent(
            "enrol credentials are only valid for POST /v1/enroll",
            hint="Use the agent_id and agent_key returned by enrolment for everything else.",
        )

    if path == "/v1/me":
        if method not in ("GET", "HEAD"):
            raise MethodNotAllowed("GET only", hint="GET /v1/me")
        return _me(hub, auth)

    # SPEC §3.4: a pending agent may read nothing but its own status.
    if pending:
        raise PendingApproval(
            "this agent is waiting for the host to approve it",
            detail={"agent": agent_id},
            hint="Ask the host to run `parley approve " + agent_id + "`. "
            "GET /v1/me shows your status meanwhile.",
        )

    if path == "/v1/events":
        if method == "POST":
            _require_writer(kind)
            return _post_events(hub, agent, body, query)
        if method in ("GET", "HEAD"):
            return _get_events(hub, query)
        raise MethodNotAllowed("GET or POST", hint="GET to read, POST to append.")

    if path == "/v1/state":
        if method not in ("GET", "HEAD"):
            raise MethodNotAllowed("GET only", hint="GET /v1/state")
        return json_response(hub, 200, hub.view.snapshot(for_viewer=(kind == "viewer")))

    if path == "/v1/index":
        if method not in ("GET", "HEAD"):
            raise MethodNotAllowed("GET only", hint="GET /v1/index")
        return _get_index(hub, query)

    if path == "/v1/ledger":
        if method not in ("GET", "HEAD"):
            raise MethodNotAllowed("GET only", hint="GET /v1/ledger")
        return json_response(hub, 200, hub.view.ledger())

    if path == "/v1/blobs":
        if method != "POST":
            raise MethodNotAllowed("POST only", hint="POST /v1/blobs with the raw bytes.")
        _require_writer(kind)
        return _post_blob(hub, agent_id, hdr, body)

    if path.startswith("/v1/blobs/"):
        if method not in ("GET", "HEAD"):
            raise MethodNotAllowed("GET or HEAD", hint="HEAD to test existence, GET to download.")
        return _get_blob(hub, kind, agent_id, method, path, hdr)

    if path.startswith("/v1/admin/"):
        if kind != "host":
            raise HostTokenRequired(
                "admin actions need the host token",
                hint="Pass it as `X-Parley-Host-Token:` or `?ht=`; it is printed once at "
                "`parley init` and stored in the Hub state directory.",
            )
        if method != "POST":
            raise MethodNotAllowed("POST only", hint="All /v1/admin/* actions are POST.")
        return _admin(hub, path[len("/v1/admin/"):], body)

    raise NotFound(
        "no such endpoint",
        detail={"path": path},
        hint="See SPEC §5 for the endpoint table.",
    )



def _has_host_credential(hdr: Dict[str, str], query: Dict[str, str]) -> bool:
    if hdr.get("x-parley-host-token") or query.get("ht"):
        return True
    return hdr.get("authorization", "").startswith(HOST_AUTH_SCHEME + " ")


def _require_writer(kind: str) -> None:
    if kind == "viewer":
        raise ReadOnlyToken(
            "viewer tokens are read-only",
            hint="Enrol as an agent (`parley join`) to write.",
        )
    if kind == "host":
        raise ReadOnlyToken(
            "the host token administers the parley; it is not an agent identity",
            hint="Enrol as an agent to append events, or use /v1/admin/* for host actions.",
        )


def _rate_limited(wait: float) -> RateLimited:
    err = RateLimited(
        "rate limit exceeded",
        detail={"retry_after_s": int(wait)},
        hint="Back off for %d s. Per-agent limits are 60 events/min (burst 120) and "
        "120 blob ops/min." % int(wait),
    )
    setattr(err, "retry_after", int(wait))
    return err


# ----------------------------------------------------------------- endpoints


def _hello(hub):
    cfg = hub.config
    return json_response(
        hub,
        200,
        {
            "v": WIRE_VERSION,
            "session": getattr(cfg, "session", ""),
            "fingerprint": getattr(cfg, "fingerprint", ""),
            "name": getattr(cfg, "name", ""),
            "agents_online": hub.view.active_agent_count(),
            "requires_seal": bool(hub.policy.get("sealed", False)),
            "server_time": now_rfc3339(),
        },
    )


def _me(hub, auth):
    agent = auth.get("agent")
    if not agent:
        return json_response(hub, 200, {"kind": auth["kind"], "agent": None})
    return json_response(
        hub,
        200,
        {
            "kind": auth["kind"],
            "agent": {
                "agent_id": agent.get("agent_id"),
                "name": agent.get("name", ""),
                "status": agent.get("status", "active"),
                "created": agent.get("created", ""),
                "last_seen": agent.get("last_seen", 0.0),
            },
            "session": getattr(hub.config, "session", ""),
            "fingerprint": getattr(hub.config, "fingerprint", ""),
            "head_seq": hub.store.head_seq(),
            "policy": dict(hub.policy),
        },
    )


def _enroll(hub, body: bytes, peer: str):
    now = time.time()
    wait = hub.limiter.check(peer or "?", "enroll", now)
    if wait:
        raise _rate_limited(wait)

    policy = hub.policy
    if not policy.get("enroll_open", True):
        raise EnrollClosed(
            "enrolment is closed on this Hub",
            hint="Ask the host to re-open it or to rotate the watchword.",
        )

    ttl = float(policy.get("enroll_ttl_s", 0) or 0)
    if ttl > 0:
        opened = hub.enroll_opened_at()
        if opened and (now - opened) > ttl:
            raise EnrollClosed(
                "the watchword has expired",
                hint="Ask the host to run `parley invite --rotate` for a fresh one.",
            )

    max_uses = int(policy.get("enroll_max_uses", 0) or 0)
    if max_uses > 0 and hub.enroll_uses() >= max_uses:
        raise EnrollClosed(
            "the watchword has reached its use limit",
            hint="Ask the host to run `parley invite --rotate` for a fresh one.",
        )

    max_agents = int(policy.get("max_agents", 16) or 16)
    if hub.store.count_agents() >= max_agents:
        raise EnrollClosed(
            "this parley is full",
            detail={"max_agents": max_agents},
            hint="Ask the host to revoke an agent that has left, or raise max_agents.",
        )

    payload = _body_json(body)
    session = str(payload.get("session") or "")
    if session and session != getattr(hub.config, "session", ""):
        raise NoSuchSession(
            "that session is not hosted here",
            detail={"session": session},
            hint="Check the Hub URL you joined.",
        )
    info = payload.get("agent")
    if not isinstance(info, dict):
        raise BadRequest(
            "enrolment body needs an `agent` object",
            hint='Send {"session": "...", "agent": {"name": "...", "kind": "...", "os": "..."}}.',
        )

    name = str(info.get("name") or "")[:64].strip() or "agent"
    agent_id = ids.new_agent_id()
    agent_key = secrets.token_bytes(32)
    status = "pending" if policy.get("require_approval", False) else "active"
    caps = info.get("capabilities")
    rec = {
        "agent_id": agent_id,
        "name": name,
        "kind": str(info.get("kind") or "unknown")[:48],
        "model": str(info.get("model") or "")[:64],
        # `agent.hello` requires non-empty os/client_version (SPEC §4.1), and the
        # Hub authors that event on the joiner's behalf.  "unknown" is honest and
        # keeps a minimal enrolment -- a shell script with curl -- conforming.
        "os": str(info.get("os") or "unknown")[:32],
        "host": str(info.get("host") or "")[:64],
        "client_version": str(info.get("client_version") or "unknown")[:32],
        "capabilities": [str(c)[:32] for c in caps[:32]] if isinstance(caps, list) else [],
        "workspace_hint": str(info.get("workspace_hint") or "")[:512],
        "key_hex": agent_key.hex(),
        "status": status,
        "created": now_rfc3339(),
        "last_seen": now,
    }
    hub.store.put_agent(rec)
    hub.note_enrolment()

    hello_body = {
        "name": rec["name"],
        "kind": rec["kind"],
        "os": rec["os"],
        "client_version": rec["client_version"],
        "capabilities": rec["capabilities"],
    }
    for optional in ("model", "host", "workspace_hint"):
        if rec[optional]:
            hello_body[optional] = rec[optional]
    # Authored as the new agent (the Hub holds its key, so the signature is
    # genuine) so the roster builds from the log alone, per SPEC §4.1.
    hub.hub_event("agent.hello", hello_body, actor=agent_id, actor_key=agent_key)
    if status == "pending":
        hub.hub_event(
            "hub.notice",
            {
                "text": "%s (%s) is waiting for approval" % (rec["name"], agent_id),
                "level": "warn",
                "agent_id": agent_id,
            },
        )

    log.info("enrolled agent %s (%s) status=%s from %s", agent_id, rec["name"], status, peer)
    return json_response(
        hub,
        201,
        {
            "agent_id": agent_id,
            "agent_key": agent_key.hex(),
            "session": getattr(hub.config, "session", ""),
            "fingerprint": getattr(hub.config, "fingerprint", ""),
            "hub_time": now_rfc3339(),
            "seq": hub.store.head_seq(),
            "status": status,
            "policy": hub.wire_policy(),
        },
    )


def _post_events(hub, agent: dict, body: bytes, query: Dict[str, str]):
    agent_id = str(agent.get("agent_id"))
    payload = _body_json(body)
    if "events" in payload:
        raw = payload.get("events")
        if not isinstance(raw, list):
            raise BadRequest("`events` must be a list", hint='Send {"events": [ {...}, ... ]}.')
    else:
        raw = [payload]
    if not raw:
        return json_response(hub, 200, {"results": [], "head_seq": hub.store.head_seq()})
    if len(raw) > MAX_BATCH:
        raise TooLarge(
            "batch too large",
            detail={"count": len(raw), "max": MAX_BATCH},
            hint="Send at most %d events per POST." % MAX_BATCH,
        )
    for item in raw:
        if not isinstance(item, dict):
            raise BadEvent("every element of `events` must be an object", hint="See SPEC §2.")

    wait = hub.limiter.check(agent_id, "events", time.time(), float(len(raw)))
    if wait:
        raise _rate_limited(wait)

    results, stored = hub.append(raw, agent=agent)
    resp: Dict[str, Any] = {
        "results": results,
        "head_seq": hub.store.head_seq(),
        "seq": results[-1]["seq"] if results else hub.store.head_seq(),
    }
    if query.get("echo") in ("1", "true", "yes"):
        resp["events"] = stored
    return json_response(hub, 201, resp)


def _get_events(hub, query: Dict[str, str]):
    since = _int(query.get("since"), 0)
    limit = max(1, min(MAX_EVENTS_LIMIT, _int(query.get("limit"), 1000)))
    wait = max(0.0, min(MAX_WAIT_S, _float(query.get("wait"), 0.0)))
    types = _types_param(query.get("types", ""))
    events = hub.read_events(since=since, limit=limit, types=types, wait=wait)
    return json_response(
        hub,
        200,
        {"events": events, "head_seq": hub.store.head_seq(), "since": since},
    )


def _get_index(hub, query: Dict[str, str]):
    since = _int(query.get("since"), 0)
    files = hub.store.list_files_since(since) if since > 0 else list(hub.store.list_files().values())
    stats = hub.store.file_stats()
    return json_response(
        hub,
        200,
        {
            "files": files,
            "count": stats["count"],
            "bytes": stats["bytes"],
            "head_seq": hub.store.head_seq(),
            "since": since,
        },
    )


def _post_blob(hub, agent_id: str, hdr: Dict[str, str], body: bytes):
    now = time.time()
    wait = hub.limiter.check(agent_id or "?", "blobs", now)
    if wait:
        raise _rate_limited(wait)

    declared = hdr.get("x-parley-blob-sha256", "")
    if not declared:
        raise BadRequest(
            "X-Parley-Blob-SHA256 is required",
            hint="Send the sha256 of the bytes you are uploading, as 'sha256:<64 hex>' or bare hex.",
        )
    data = body
    if hdr.get("x-parley-seal"):
        # SPEC §3.6: blobs travel as independent 256 KiB sealed frames, not as a
        # single sealed envelope -- that is what keeps a 25 MiB upload streamable.
        data = _unseal_frames(hub, body, declared)
    elif "gzip" in hdr.get("content-encoding", "").lower():
        try:
            data = gzip.decompress(body)
        except (OSError, EOFError, binascii.Error) as exc:
            raise BadRequest(
                "Content-Encoding says gzip but the body does not decompress",
                hint=str(exc)[:160],
            )
    max_bytes = int(hub.policy.get("max_blob_bytes", 26214400) or 26214400)
    if len(data) > max_bytes:
        raise TooLarge(
            "blob exceeds max_blob_bytes",
            detail={"size": len(data), "max_blob_bytes": max_bytes},
            hint="Files larger than %d bytes are not synced; the Hub emits a hub.notice "
            "naming them instead." % max_bytes,
        )
    size = hub.store.put_blob(declared, data)
    # Measure the blob's line count now, while its bytes are already resident:
    # ledger.compute() is pure and may not read blobs, so the index entry written
    # by a later file.put has to carry the answer (SPEC §9).
    hub.note_blob_lines(wire_blob_hash(declared), data)
    return json_response(
        hub,
        201,
        {"hash": wire_blob_hash(declared), "size": size},
    )



def _unseal_frames(hub, body: bytes, declared: str) -> bytes:
    """Decrypt a frame-sealed blob upload, trying each live seal key."""
    wire = wire_blob_hash(declared)
    last = None
    for key in hub.seal_keys():
        try:
            return crypto.unseal_frames(key, body, wire)
        except Exception as exc:  # noqa: BLE001 - tag failure is the probe result
            last = exc
    raise BadRequest(
        "could not decrypt the sealed blob",
        detail={"cause": str(last)[:160]} if last else None,
        hint="Client and Hub must derive seal_key from the same watchword.",
    )


def _get_blob(hub, kind: str, agent_id: str, method: str, path: str, hdr: Dict[str, str]):
    if kind != "agent":
        # SPEC §3.7: a viewer token grants no blob content.
        raise ReadOnlyToken(
            "blob content is not available to viewer or host tokens",
            hint="Enrol as an agent to download file content.",
        )
    raw = unquote(path[len("/v1/blobs/"):]).strip("/")
    if not raw:
        raise NoSuchBlob("no blob hash in the path", hint="GET /v1/blobs/sha256:<64 hex>")
    blob_hash = normalise_blob_hash(raw)

    wait = hub.limiter.check(agent_id or "?", "blobs", time.time())
    if wait:
        raise _rate_limited(wait)

    size = hub.store.blob_size(blob_hash)
    if size is None:
        raise NoSuchBlob(
            "no such blob",
            detail={"hash": wire_blob_hash(blob_hash)},
            hint="Upload it first with POST /v1/blobs.",
        )
    headers = base_headers(hub)
    headers["Content-Type"] = "application/octet-stream"
    headers["Cache-Control"] = "public, max-age=31536000, immutable"
    headers["ETag"] = '"' + blob_hash + '"'
    headers["X-Parley-Blob-SHA256"] = wire_blob_hash(blob_hash)
    if method == "HEAD":
        headers["Content-Length"] = str(size)
        return 200, headers, b""

    data = hub.store.read_blob(blob_hash)
    if hdr.get("x-parley-seal"):
        for key in hub.seal_keys():
            try:
                headers["X-Parley-Seal"] = "v1"
                return 200, headers, crypto.seal_frames(key, data, wire_blob_hash(blob_hash))
            except Exception:  # noqa: BLE001
                log.debug("seal_frames failed for one key", exc_info=True)
        raise BadRequest(
            "this Hub cannot seal blob frames",
            hint="Fetch the blob unsealed, or check the Hub's root key.",
        )
    if "gzip" in hdr.get("accept-encoding", "").lower() and len(data) > 1024:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as gz:
            gz.write(data)
        packed = buf.getvalue()
        if len(packed) < len(data):
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
            return 200, headers, packed
    return 200, headers, data


# -------------------------------------------------------------------- admin


def _admin(hub, action: str, body: bytes):
    payload = _body_json(body) if body else {}
    if action == "approve":
        agent_id = str(payload.get("agent_id") or "")
        rec = hub.store.get_agent(agent_id)
        if not rec:
            raise NoSuchAgent(
                "no such agent",
                detail={"agent": agent_id},
                hint="List the roster with GET /v1/state.",
            )
        hub.store.set_agent_status(agent_id, "active")
        hub.hub_event(
            "hub.notice",
            {
                "text": "%s (%s) approved by the host" % (rec.get("name", ""), agent_id),
                "level": "info",
                "agent_id": agent_id,
            },
        )
        log.info("approved agent %s", agent_id)
        return json_response(hub, 200, {"ok": True, "agent_id": agent_id, "status": "active"})

    if action == "revoke":
        agent_id = str(payload.get("agent_id") or "")
        rec = hub.store.get_agent(agent_id)
        if not rec:
            raise NoSuchAgent("no such agent", detail={"agent": agent_id}, hint="Check the roster.")
        hub.store.set_agent_status(agent_id, "revoked")
        hub.limiter.forget(agent_id)
        hub.hub_event("agent.revoked", {"agent_id": agent_id, "by": "host"})
        hub.hub_event("agent.offline", {"agent_id": agent_id, "reason": "revoked"})
        log.info("revoked agent %s", agent_id)
        return json_response(hub, 200, {"ok": True, "agent_id": agent_id, "status": "revoked"})

    if action == "rotate-watchword":
        words = _int(payload.get("words"), 5)
        watchword, fingerprint = hub.rotate_watchword(words=max(3, min(12, words)))
        return json_response(
            hub,
            200,
            {"ok": True, "watchword": watchword, "fingerprint": fingerprint},
        )

    if action == "viewer-token":
        ttl = max(60.0, min(90 * 86400.0, _float(payload.get("ttl_s"), DEFAULT_VIEWER_TTL_S)))
        label = str(payload.get("label") or "")[:64]
        token = hub.mint_viewer_token(ttl=ttl, label=label)
        return json_response(
            hub,
            201,
            {
                "viewer_token": token,
                "expires_in_s": int(ttl),
                "deck_url": hub.url + "/?vt=" + token,
            },
        )

    if action == "revoke-viewer-token":
        token = str(payload.get("viewer_token") or "")
        if token:
            hub.store.revoke_viewer_token(token)
        else:
            hub.store.revoke_all_viewer_tokens()
        return json_response(hub, 200, {"ok": True})

    if action in ("reveal-invite", "reveal"):
        # The watchword itself is never recoverable -- the Hub only ever stored a
        # hash of it (SPEC §3.7 / §7).  Rotation is the way to get a usable one.
        return json_response(
            hub,
            200,
            {
                "ok": True,
                "fingerprint": getattr(hub.config, "fingerprint", ""),
                "watchword": None,
                "hint": "The Hub stores only a hash of the watchword, never the words. "
                "Use /v1/admin/rotate-watchword to mint a fresh invite.",
            },
        )

    if action == "shutdown":
        hub.request_shutdown()
        return json_response(hub, 200, {"ok": True, "shutting_down": True})

    raise NotFound(
        "no such admin action",
        detail={"action": action},
        hint="approve | revoke | rotate-watchword | viewer-token | revoke-viewer-token | "
        "reveal | shutdown",
    )


# ---------------------------------------------------------------- deck assets


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _serve_deck(hub, method: str, path: str):
    root = Path(hub.deck_dir).resolve()
    rel = unquote(path)
    if rel.startswith("/deck/"):
        rel = rel[len("/deck/"):]
    elif rel == "/deck":
        rel = ""
    else:
        rel = rel.lstrip("/")
    if rel in ("", "/") or rel.endswith("/"):
        rel = rel + "index.html"

    if "\x00" in rel or "\\" in rel:
        raise BadPath(
            "illegal characters in path",
            hint="Deck asset paths are POSIX-style with no backslashes.",
        )
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise BadPath("path traversal rejected", hint="Deck assets are served from one directory.")

    headers = base_headers(hub)
    headers["Content-Security-Policy"] = str(hub.policy.get("deck_csp") or DEFAULT_DECK_CSP)
    headers["X-Frame-Options"] = "SAMEORIGIN"

    if not root.is_dir() and parts == ["index.html"]:
        return _placeholder(hub, method, headers)

    target = root.joinpath(*parts) if parts else root / "index.html"
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target
    if not _is_within(resolved, root):
        raise BadPath("path traversal rejected", hint="Deck assets are served from one directory.")

    if not resolved.is_file():
        if parts == ["index.html"]:
            return _placeholder(hub, method, headers)
        raise NotFound(
            "no such Deck asset",
            detail={"path": "/".join(parts)},
            hint="The Deck is served from parley/hub/deck/.",
        )

    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise NotFound("Deck asset is unreadable", hint=str(exc)[:160])

    ext = resolved.suffix.lower()
    ctype = _MIME_OVERRIDES.get(ext) or mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    headers["Content-Type"] = ctype
    headers["Cache-Control"] = "no-cache" if ext in (".html", ".htm") else "public, max-age=300"
    headers["ETag"] = '"' + sha256_hex(data)[:32] + '"'
    if method == "HEAD":
        headers["Content-Length"] = str(len(data))
        return 200, headers, b""
    return 200, headers, data


def _placeholder(hub, method: str, headers: Dict[str, str]):
    headers = dict(headers)
    headers["Content-Type"] = "text/html; charset=utf-8"
    headers["Cache-Control"] = "no-store"
    if method == "HEAD":
        headers["Content-Length"] = str(len(_PLACEHOLDER_DECK))
        return 200, headers, b""
    return 200, headers, _PLACEHOLDER_DECK
