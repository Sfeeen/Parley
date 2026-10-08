"""Canonical JSON, RFC 3339 timestamps and crash-safe writes.

Everything here is used on both sides of the wire, so "close enough" is not good
enough: :func:`canonical` feeds every signature, and a one-byte difference in how
two implementations serialise the same object makes every signature mismatch.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Union

from .errors import BadJson

__all__ = ["canonical", "dumps", "loads", "sha256_hex", "now_rfc3339",
           "parse_rfc3339", "format_rfc3339", "atomic_write"]


def canonical(obj: Any) -> bytes:
    """SPEC 1.3 canonical form: the exact bytes every signature is taken over.

    ``sort_keys`` makes it order-independent, ``separators`` removes all optional
    whitespace, ``ensure_ascii=False`` keeps non-ASCII text as real UTF-8 rather
    than ``\\uXXXX`` escapes (so two implementations cannot disagree about escape
    casing), and ``allow_nan=False`` rejects the non-finite floats that have no
    JSON representation at all.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def dumps(obj: Any) -> str:
    """Compact JSON for storage and the wire.

    Unlike :func:`canonical` this keeps insertion order, which makes stored
    events and log lines read the way they were written. Never use it where a
    signature is involved.
    """
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def loads(data: Union[bytes, str]) -> Any:
    """Parse JSON, raising the protocol's own error so callers need one except."""
    try:
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8")
        return json.loads(data)
    except UnicodeDecodeError as exc:
        raise BadJson("request body is not valid UTF-8",
                      hint="Send JSON encoded as UTF-8.") from exc
    except ValueError as exc:
        raise BadJson("could not parse JSON: %s" % exc,
                      hint="Check for a trailing comma, a NaN, or a truncated body.") from exc


def sha256_hex(data: bytes) -> str:
    """Lowercase hex SHA-256. Used for blob ids and for the body hash in SPEC 3.3."""
    return hashlib.sha256(data).hexdigest()


def format_rfc3339(when: _dt.datetime) -> str:
    """Render an aware datetime in the one timestamp format SPEC 1.2 allows.

    Milliseconds, always three digits, always a literal ``Z``. Python's own
    ``isoformat`` gives microseconds and ``+00:00``, which is a different string
    for the same instant -- and different strings break naive consumers.
    """
    if when.tzinfo is None:
        raise ValueError("refusing to format a naive datetime as UTC")
    when = when.astimezone(_dt.timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (when.microsecond // 1000)


def now_rfc3339() -> str:
    """Current UTC time in SPEC 1.2 form."""
    return format_rfc3339(_dt.datetime.now(_dt.timezone.utc))


_RFC3339_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"[Tt ](?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<tz>[Zz]|[+-]\d{2}:?\d{2})$"
)


def parse_rfc3339(s: str) -> float:
    """Parse an RFC 3339 timestamp to a Unix-seconds float.

    Hand-rolled rather than ``datetime.fromisoformat`` because that function
    rejects the trailing ``Z`` until Python 3.11 and accepts only 3- or 6-digit
    fractions -- and we must read timestamps written by other implementations,
    not only our own.
    """
    if not isinstance(s, str):
        raise ValueError("timestamp must be a string, got %r" % type(s).__name__)
    m = _RFC3339_RE.match(s.strip())
    if not m:
        raise ValueError("not an RFC 3339 timestamp: %r" % s)
    frac = m.group("frac") or ""
    micro = int((frac + "000000")[:6]) if frac else 0
    tz = m.group("tz")
    if tz in ("Z", "z"):
        offset = _dt.timezone.utc
    else:
        sign = 1 if tz[0] == "+" else -1
        tz_digits = tz[1:].replace(":", "")
        delta = _dt.timedelta(hours=int(tz_digits[:2]), minutes=int(tz_digits[2:]))
        offset = _dt.timezone(sign * delta)
    when = _dt.datetime(int(m.group("y")), int(m.group("mo")), int(m.group("d")),
                        int(m.group("h")), int(m.group("mi")), int(m.group("s")),
                        micro, tzinfo=offset)
    return when.timestamp()


def atomic_write(path: Union[str, Path], data: bytes) -> None:
    """Replace ``path`` with ``data``, or leave the old contents untouched.

    A reader must never see a half-written file, which rules out opening the
    target for writing. So: write a sibling temp file (same directory, therefore
    same filesystem, therefore ``os.replace`` is atomic), fsync it so the bytes
    are really on disk before the rename publishes them, then rename.

    Windows needs two extra allowances. ``os.replace`` fails with
    ``PermissionError`` if another process has the target open -- commonly a
    virus scanner that woke up on the temp file -- so we retry briefly. And the
    temp file inherits mode 0600 from ``mkstemp``, which would silently tighten
    permissions on an existing workspace file, so we copy the old mode over.
    """
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)

    old_mode = None
    try:
        old_mode = path.stat().st_mode & 0o7777
    except OSError:
        pass

    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if old_mode is not None:
            try:
                os.chmod(tmp_name, old_mode)
            except OSError:
                pass

        last_error = None
        for attempt in range(10):
            try:
                os.replace(tmp_name, str(path))
                last_error = None
                break
            except PermissionError as exc:        # Windows: target momentarily open
                last_error = exc
                time.sleep(0.02 * (attempt + 1))
        if last_error is not None:
            raise last_error

        _fsync_directory(directory)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _fsync_directory(directory: Path) -> None:
    """Best effort: make the rename itself durable.

    Only meaningful on POSIX. Windows has no directory handle to sync and raises,
    which is not an error worth propagating -- the data is already fsynced.
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
