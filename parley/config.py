"""On-disk configuration: the Hub's descriptor and a client's credentials.

Both files hold key material, so both are written through
:func:`~parley.jsonutil.atomic_write` (a reader never sees a half-written file)
and then chmodded to 0600 where the OS supports it. Secret fields are marked
``repr=False`` so a dataclass landing in a traceback or a debug log cannot print
a key.

Both structures round-trip through JSON and preserve fields they do not know
about: a newer Parley may add a key, and an older one reading and rewriting the
file must not silently delete it.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import MISSING as _MISSING
from dataclasses import dataclass, field, fields
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Dict, Optional, Type, TypeVar
from urllib.parse import urlsplit

from .jsonutil import atomic_write, canonical, loads, now_rfc3339

__all__ = ["DEFAULTS", "DEFAULT_PORT", "STATE_DIR_NAME", "CREDENTIALS_FILE", "HUB_FILE",
           "HubConfig", "Credentials", "workspace_state_dir", "default_workspace",
           "resolve_hub_url", "harden_path"]

#: Default port for a Hub, and the port a bare ``--hub 192.168.1.20`` means.
DEFAULT_PORT = 7777

STATE_DIR_NAME = ".parley"
CREDENTIALS_FILE = "credentials.json"
HUB_FILE = "hub.json"

#: Policy defaults (SPEC 3.4, 5, 7.2). A Hub copies these into its descriptor at
#: ``init`` and hands the client-relevant subset back in the enrolment response,
#: so a client never has to guess.
DEFAULTS: Dict[str, Any] = {
    "heartbeat_s": 15,
    "psr_max_age_s": 30,
    "poll_ms": 2000,
    "max_blob_bytes": 26214400,          # 25 MiB
    "max_agents": 16,
    "skew_s": 300,
    "nonce_ttl_s": 600,
    "enroll_open": True,
    "enroll_ttl_s": 0,                   # 0 == never expires
    "enroll_max_uses": 0,                # 0 == unlimited
    "require_approval": False,
    "sealed": False,
}

_T = TypeVar("_T")


def _from_mapping(cls: Type[_T], data: Mapping[str, Any]) -> _T:
    """Build a dataclass from JSON, keeping unknown keys in ``_extra``.

    Discarding unknown keys would mean that an old client which loads and saves a
    config file written by a newer one silently strips the newer settings. That
    is the kind of data loss nobody notices until it matters.
    """
    if not isinstance(data, Mapping):
        raise ValueError("%s: expected a JSON object" % cls.__name__)
    names = {f.name for f in fields(cls) if f.name != "_extra"}   # type: ignore[arg-type]
    known = {k: v for k, v in data.items() if k in names}
    missing = [f.name for f in fields(cls)                        # type: ignore[arg-type]
               if f.name not in known and f.name != "_extra"
               and f.default is _MISSING and f.default_factory is _MISSING]  # type: ignore
    if missing:
        raise ValueError("%s: missing required field(s): %s"
                         % (cls.__name__, ", ".join(sorted(missing))))
    obj = cls(**known)                                            # type: ignore[call-arg]
    obj._extra = {k: v for k, v in data.items() if k not in names}  # type: ignore[attr-defined]
    return obj


def _to_mapping(obj: Any) -> Dict[str, Any]:
    data = {f.name: getattr(obj, f.name) for f in fields(obj) if f.name != "_extra"}
    for key, value in getattr(obj, "_extra", {}).items():
        data.setdefault(key, value)
    return data


def harden_path(path: Path, *, directory: bool = False) -> None:
    """Restrict a path to its owner. Best effort, by design.

    POSIX gets 0700/0600. Windows has no equivalent bit we can set through
    ``os.chmod`` -- its access control lives in ACLs -- so this is a no-op there
    rather than a failure. ``parley doctor`` reports the real state so nobody is
    told their keys are protected when they are not.
    """
    mode = 0o700 if directory else 0o600
    try:
        os.chmod(str(path), mode)
    except (OSError, NotImplementedError):
        pass


def workspace_state_dir(workspace: Path) -> Path:
    """``<workspace>/.parley``, created 0700 if it does not exist."""
    state_dir = Path(workspace).expanduser() / STATE_DIR_NAME
    state_dir.mkdir(parents=True, exist_ok=True)
    harden_path(state_dir, directory=True)
    return state_dir


def default_workspace() -> Path:
    """The current directory. Parley never guesses a project location."""
    return Path.cwd()


@dataclass
class HubConfig:
    """What a Hub needs to restart and still be the same parley.

    The watchword itself is never stored. ``root_key_hex`` is the derived key, so
    rotating the watchword (SPEC 3.8) replaces this value while every existing
    agent key -- which does not descend from it -- keeps working.
    """

    session: str
    name: str
    created: str
    bind: str
    port: int
    watchword_hash: str = field(repr=False)
    root_key_hex: str = field(repr=False)
    fingerprint: str = ""
    host_token: str = field(default="", repr=False)
    policy: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS))
    pbkdf2_iterations: int = 200_000
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def load(cls, state_dir: Path) -> "HubConfig":
        path = Path(state_dir) / HUB_FILE
        return _from_mapping(cls, loads(path.read_bytes()))

    def save(self, state_dir: Path) -> None:
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        harden_path(state_dir, directory=True)
        path = state_dir / HUB_FILE
        atomic_write(path, canonical(_to_mapping(self)) + b"\n")
        harden_path(path)

    def to_dict(self) -> Dict[str, Any]:
        return _to_mapping(self)

    @classmethod
    def new(cls, session: str, name: str, *, bind: str, port: int,
            watchword_hash: str, root_key_hex: str, fingerprint: str,
            host_token: str, policy: Optional[Dict[str, Any]] = None,
            pbkdf2_iterations: int = 200_000) -> "HubConfig":
        """Constructor that stamps ``created`` so no caller has to remember to."""
        merged = dict(DEFAULTS)
        if policy:
            merged.update(policy)
        return cls(session=session, name=name, created=now_rfc3339(), bind=bind, port=port,
                   watchword_hash=watchword_hash, root_key_hex=root_key_hex,
                   fingerprint=fingerprint, host_token=host_token, policy=merged,
                   pbkdf2_iterations=pbkdf2_iterations)


@dataclass
class Credentials:
    """What a client needs to rejoin without the watchword.

    This is the file that makes the watchword an enrolment secret only: once an
    agent holds ``agent_key_hex`` it never needs the invite again, and revoking
    the agent does not require rotating anything else.
    """

    hub_url: str
    session: str
    agent_id: str
    agent_key_hex: str = field(repr=False)
    fingerprint: str = ""
    name: str = ""
    kind: str = ""
    sealed: bool = False
    policy: Dict[str, Any] = field(default_factory=dict)
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def load(cls, workspace: Path) -> "Credentials":
        path = Path(workspace).expanduser() / STATE_DIR_NAME / CREDENTIALS_FILE
        return _from_mapping(cls, loads(path.read_bytes()))

    def save(self, workspace: Path) -> None:
        state_dir = workspace_state_dir(Path(workspace))
        path = state_dir / CREDENTIALS_FILE
        atomic_write(path, canonical(_to_mapping(self)) + b"\n")
        harden_path(path)

    def to_dict(self) -> Dict[str, Any]:
        return _to_mapping(self)

    @property
    def agent_key(self) -> bytes:
        """The agent key as bytes. Not stored as an attribute, so it cannot be
        picked up by ``asdict`` or printed by a default repr."""
        return bytes.fromhex(self.agent_key_hex)

    def is_world_readable(self, workspace: Path) -> bool:
        """True when other users on this machine can read the key file.

        Used by ``parley doctor``: on Windows the permission bits mean nothing,
        so a False here is "we could not tell", not "it is safe".
        """
        path = Path(workspace).expanduser() / STATE_DIR_NAME / CREDENTIALS_FILE
        try:
            mode = path.stat().st_mode
        except OSError:
            return False
        return bool(mode & (stat.S_IRGRP | stat.S_IROTH))


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def resolve_hub_url(raw: str) -> str:
    """Turn whatever a human typed into a canonical base URL.

    Accepts ``192.168.1.20``, ``192.168.1.20:7777``, ``hub.local``,
    ``http://hub.local/``, ``https://parley.example.com/team/``, ``::1`` and
    ``[::1]:7777``.

    A bare host with no port gets :data:`DEFAULT_PORT`, because that is
    unambiguously what someone reading an IP address off a colleague's screen
    means. An explicit scheme is left alone -- ``https://parley.example.com``
    means port 443 and adding 7777 would be actively wrong.

    Any path is preserved (Hubs do get reverse-proxied under a prefix) with its
    trailing slash removed, so callers can always concatenate ``/v1/...``.

    Raises ``ValueError`` on anything it cannot make sense of, including
    credentials embedded in the URL: those end up in logs and shell history, and
    Parley has no use for them.
    """
    if not isinstance(raw, str):
        raise ValueError("hub URL must be a string")
    text = raw.strip()
    if not text:
        raise ValueError("hub URL is empty")

    had_scheme = bool(_SCHEME_RE.match(text))
    if had_scheme:
        scheme, rest = text.split("://", 1)
    else:
        scheme, rest = "http", text
    scheme = scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("hub URL scheme must be http or https, got %r" % scheme)

    if "/" in rest:
        authority, tail = rest.split("/", 1)
        path = "/" + tail
    else:
        authority, path = rest, ""

    if "@" in authority:
        raise ValueError("hub URL must not contain credentials")

    # A bare IPv6 literal has no brackets but does have several colons, which is
    # exactly how it is distinguishable from host:port.
    if authority.count(":") >= 2 and not authority.startswith("["):
        authority = "[%s]" % authority

    try:
        parts = urlsplit("%s://%s" % (scheme, authority))
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("hub URL is not parseable: %s" % exc) from exc
    if not host:
        raise ValueError("hub URL has no host: %r" % raw)
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("hub URL port %d is out of range" % port)

    host = host.lower()
    if ":" in host:                      # IPv6 literal: brackets are part of the URL
        host = "[%s]" % host
    if port is None and not had_scheme:
        port = DEFAULT_PORT

    authority_out = host if port is None else "%s:%d" % (host, port)
    path = path.rstrip("/")
    if path in ("", "/"):
        path = ""
    return "%s://%s%s" % (scheme, authority_out, path)
