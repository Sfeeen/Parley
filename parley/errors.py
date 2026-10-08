"""The one exception hierarchy, shared by the Hub, the client and the CLI.

Every error that can cross the wire is a :class:`ParleyError` subclass carrying
the error ``code`` and HTTP status from SPEC 12, so a handler can turn any
exception into a response without a lookup table, and a client can turn a
response back into the right exception type with :func:`error_from_response`.

Why one class per code rather than one class with a code attribute: callers want
``except PendingApproval`` and ``except RateLimited`` to mean different things,
and a bare ``except ParleyError`` still catches the lot.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Type

__all__ = [
    "ParleyError", "BadRequest", "BadJson", "BadSignature", "UnknownAgent",
    "StaleTimestamp", "ReplayedNonce", "PendingApproval", "Revoked", "EnrollClosed",
    "ReadOnlyToken", "HostTokenRequired", "NoSuchSession", "NoSuchBlob", "NoSuchAgent",
    "SeqConflict", "DuplicateEvent", "TooLarge", "BadEvent", "BadPath", "UnknownType",
    "RateLimited", "ShuttingDown", "FingerprintMismatch", "TransportError",
    "CryptoFailure", "NonceReuse", "BadSeal",
    "ERRORS_BY_CODE", "error_from_response",
]


class ParleyError(Exception):
    """Base class. ``code`` and ``http_status`` are what SPEC 12 calls for."""

    code: str = "error"
    http_status: int = 500
    retryable: bool = False

    def __init__(self, message: str, *, detail: Optional[dict] = None, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail: Dict[str, Any] = dict(detail) if detail else {}
        self.hint = hint

    def to_dict(self) -> dict:
        """The exact JSON body SPEC 12 specifies.

        ``detail`` is always present (possibly empty) so consumers never have to
        branch on its absence.
        """
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "detail": self.detail,
                "retryable": self.retryable,
                "hint": self.hint,
            }
        }

    def __repr__(self) -> str:        # pragma: no cover - debugging aid only
        return "%s(%r)" % (type(self).__name__, self.message)


# -- 400 ---------------------------------------------------------------------
class BadRequest(ParleyError):
    code = "bad_request"
    http_status = 400


class BadJson(ParleyError):
    code = "bad_json"
    http_status = 400


# -- 401 ---------------------------------------------------------------------
class BadSignature(ParleyError):
    code = "bad_signature"
    http_status = 401


class UnknownAgent(ParleyError):
    code = "unknown_agent"
    http_status = 401


class StaleTimestamp(ParleyError):
    code = "stale_timestamp"
    http_status = 401


class ReplayedNonce(ParleyError):
    code = "replayed_nonce"
    http_status = 401


# -- 403 ---------------------------------------------------------------------
class PendingApproval(ParleyError):
    code = "pending_approval"
    http_status = 403


class Revoked(ParleyError):
    code = "revoked"
    http_status = 403


class EnrollClosed(ParleyError):
    code = "enroll_closed"
    http_status = 403


class ReadOnlyToken(ParleyError):
    code = "read_only_token"
    http_status = 403


class HostTokenRequired(ParleyError):
    code = "host_token_required"
    http_status = 403


# -- 404 ---------------------------------------------------------------------
class NoSuchSession(ParleyError):
    code = "no_such_session"
    http_status = 404


class NoSuchBlob(ParleyError):
    code = "no_such_blob"
    http_status = 404


class NoSuchAgent(ParleyError):
    code = "no_such_agent"
    http_status = 404


class NoHubState(ParleyError):
    """Asked to resume a parley whose state directory is absent or unreadable.

    Distinct from ``no_such_session``: that one means "this Hub is running, but
    not the parley you asked for".  This one means there is nothing here to run.
    """

    code = "no_hub_state"
    http_status = 404


# -- 409 ---------------------------------------------------------------------
class SeqConflict(ParleyError):
    code = "seq_conflict"
    http_status = 409


class HubStateExists(ParleyError):
    """Asked to create a parley where one already lives.

    Refusing is the whole point: ``init`` mints a new session id and root key, so
    silently overwriting would strand every enrolled agent behind a
    ``fingerprint_mismatch`` with no way back.  ``resume`` is almost always what
    the caller meant; ``init --force`` is the deliberate, destructive override.
    """

    code = "hub_state_exists"
    http_status = 409


class DuplicateEvent(ParleyError):
    code = "duplicate_event"
    http_status = 409


class FingerprintMismatch(ParleyError):
    """The Hub answering this session is not the one we enrolled with.

    Never auto-recoverable: SPEC 3.5 requires a hard stop so a human can compare
    the three spoken words.
    """

    code = "fingerprint_mismatch"
    http_status = 409


# -- 413 / 422 / 429 / 503 ---------------------------------------------------
class TooLarge(ParleyError):
    code = "too_large"
    http_status = 413


class BadEvent(ParleyError):
    code = "bad_event"
    http_status = 422


class BadPath(ParleyError):
    code = "bad_path"
    http_status = 422


class UnknownType(ParleyError):
    code = "unknown_type"
    http_status = 422


class RateLimited(ParleyError):
    code = "rate_limited"
    http_status = 429
    retryable = True


class ShuttingDown(ParleyError):
    code = "shutting_down"
    http_status = 503
    retryable = True


# -- local-only (never a response code of their own) -------------------------
class TransportError(ParleyError):
    """The request never got an HTTP answer at all: DNS, connect, reset, timeout."""

    code = "transport"
    http_status = 0
    retryable = True


class CryptoFailure(ParleyError):
    """A local cryptographic operation refused to proceed.

    Distinct from :class:`BadSignature`, which is a verdict about someone else's
    data; this one means *we* could not or would not produce/consume ciphertext.
    """

    code = "crypto_failure"
    http_status = 500


class NonceReuse(CryptoFailure):
    """A nonce repeated under one key. SPEC 3.6 requires an abort, not a warning.

    Reusing a ChaCha20-Poly1305 nonce leaks the XOR of two plaintexts and, worse,
    lets an attacker forge the Poly1305 one-time key. There is no safe recovery.
    """

    code = "nonce_reuse"


class BadSeal(CryptoFailure):
    """A sealed body failed authentication: wrong key, wrong AAD, or tampering."""

    code = "bad_seal"
    http_status = 400


#: Every code SPEC 12 defines, mapped to its class, so a client can rebuild the
#: right exception from a JSON error body.
ERRORS_BY_CODE: Dict[str, Type[ParleyError]] = {
    cls.code: cls
    for cls in (
        BadRequest, BadJson, BadSignature, UnknownAgent, StaleTimestamp, ReplayedNonce,
        PendingApproval, Revoked, EnrollClosed, ReadOnlyToken, HostTokenRequired,
        NoSuchSession, NoSuchBlob, NoSuchAgent, SeqConflict, DuplicateEvent,
        FingerprintMismatch, TooLarge, BadEvent, BadPath, UnknownType, RateLimited,
        ShuttingDown, TransportError, CryptoFailure, NonceReuse, BadSeal,
    )
}


def error_from_response(payload: Mapping[str, Any], http_status: int = 0) -> ParleyError:
    """Rebuild an exception from a SPEC 12 error body.

    Unknown codes become a plain :class:`ParleyError` carrying the server's text
    rather than being swallowed -- forward compatibility cuts both ways.
    """
    err = payload.get("error") if isinstance(payload, Mapping) else None
    if not isinstance(err, Mapping):
        return ParleyError("malformed error response", detail={"http_status": http_status})
    code = str(err.get("code") or "error")
    message = str(err.get("message") or code)
    detail = err.get("detail")
    cls = ERRORS_BY_CODE.get(code, ParleyError)
    exc = cls(message,
              detail=dict(detail) if isinstance(detail, Mapping) else None,
              hint=str(err.get("hint") or ""))
    if cls is ParleyError:
        exc.code = code
        exc.http_status = http_status or 500
        exc.retryable = bool(err.get("retryable"))
    return exc
