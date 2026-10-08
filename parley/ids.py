"""Identifier minting and checking (SPEC 1.1).

Ids are opaque: nothing in the system may parse meaning out of one beyond its
prefix. :func:`is_id` exists so that a Hub can reject a malformed id *before* it
reaches a database or a path, not so that callers can decode it.
"""
from __future__ import annotations

import re
import secrets

__all__ = ["new_session_id", "new_agent_id", "new_event_id", "new_task_id",
           "new_viewer_token", "new_host_token", "new_blob_id", "is_id",
           "is_blob_hash", "short", "ID_HEX_LENGTHS"]

#: How many hex characters follow each prefix. A length check is cheap and stops
#: a caller from smuggling a 10 KiB "id" into a log line or a filename.
ID_HEX_LENGTHS = {
    "ses": 16,
    "agt": 16,
    "evt": 16,
    "tsk": 8,
    "vwr": 32,
    "hst": 32,
}

_HEX_RE = re.compile(r"^[0-9a-f]+$")
_BLOB_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def new_session_id() -> str:
    return "ses_" + secrets.token_hex(8)


def new_agent_id() -> str:
    return "agt_" + secrets.token_hex(8)


def new_event_id() -> str:
    return "evt_" + secrets.token_hex(8)


def new_task_id() -> str:
    return "tsk_" + secrets.token_hex(4)


def new_viewer_token() -> str:
    """Read-only Deck credential (SPEC 3.7). 128 bits: it travels in a URL."""
    return "vwr_" + secrets.token_hex(16)


def new_host_token() -> str:
    """Admin credential, printed once at ``init`` and never again."""
    return "hst_" + secrets.token_hex(16)


def new_blob_id(sha256_hex_digest: str) -> str:
    """Wrap a bare digest in the ``sha256:`` form SPEC 1.1 uses on the wire."""
    digest = sha256_hex_digest.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("not a sha256 hex digest: %r" % sha256_hex_digest)
    return "sha256:" + digest


def is_id(value: str, prefix: str) -> bool:
    """True when ``value`` is a well-formed id of the given kind.

    The prefix may be written with or without its separator (``"agt"`` and
    ``"agt_"`` both work) because both spellings appear naturally at call sites.
    Blob ids use ``sha256:`` rather than an underscore, which is handled too.
    """
    if not isinstance(value, str) or not isinstance(prefix, str):
        return False
    name = prefix.rstrip("_:")
    if name == "sha256":
        return bool(_BLOB_RE.match(value))
    expected = ID_HEX_LENGTHS.get(name)
    head = name + "_"
    if not value.startswith(head):
        return False
    body = value[len(head):]
    if expected is not None and len(body) != expected:
        return False
    if expected is None and not 4 <= len(body) <= 64:
        return False
    return bool(_HEX_RE.match(body))


def is_blob_hash(value: str) -> bool:
    """True for the ``sha256:<64 hex>`` form used by every blob reference."""
    return isinstance(value, str) and bool(_BLOB_RE.match(value))


def short(value: str, chars: int = 4) -> str:
    """A stable abbreviation for human-facing output: ``agt_0c5518aa91be7742`` -> ``0c55``.

    Only ever for display -- two agents could in principle share a short form, so
    nothing may key off this.
    """
    if not isinstance(value, str):
        return ""
    body = value.split("_", 1)[-1].split(":", 1)[-1]
    return body[:chars]
