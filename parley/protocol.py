"""Event construction, validation, and the workspace path boundary.

Two different kinds of code live here.

The event half (SPEC 2, 4 and 6) is deliberately *permissive about what it does
not know*: unknown keys inside a known body are forwarded untouched and unknown
``x.*`` types are accepted, because forward compatibility is a hard requirement.
It is strict about the handful of fields other participants actually act on.

The path half (SPEC 7.1) is the opposite: :func:`normalise_path` and
:func:`safe_join` are a security boundary. Every path a client writes to disk
arrives from the Hub or from another agent, either of which may be hostile, and
the only thing standing between a malicious ``file.put`` and someone's
``~/.ssh/authorized_keys`` is this code. Assume it is being attacked.
"""
from __future__ import annotations

import os
import unicodedata
from pathlib import Path
from collections.abc import Mapping
from typing import Any, List, Optional, Union

from .errors import BadPath
from .ids import is_blob_hash, is_id, new_event_id, short
from .jsonutil import canonical, now_rfc3339, parse_rfc3339
from .version import WIRE_VERSION

__all__ = [
    "EVENT_TYPES", "PSR_STATES", "TASK_STATUSES", "CONTRIBUTION_KINDS",
    "MAX_BODY_BYTES", "MAX_CHAT_BYTES", "MAX_PATH_BYTES", "MAX_FOCUS_PATHS",
    "MAX_HEADLINE_CHARS", "HUB_ACTOR", "EXTENSION_PREFIX",
    "make_event", "validate_event", "validate_psr", "normalise_path", "safe_join",
    "is_hub_authored", "event_summary", "is_known_type",
]

MAX_BODY_BYTES = 256 * 1024
MAX_CHAT_BYTES = 16 * 1024
MAX_PATH_BYTES = 1024
MAX_PATH_SEGMENT_BYTES = 255
MAX_FOCUS_PATHS = 8
MAX_HEADLINE_CHARS = 80

#: The actor value Hub-authored events carry instead of an ``agt_`` id.
HUB_ACTOR = "hub"

#: Namespace reserved for extensions. Types here are always relayed untouched.
EXTENSION_PREFIX = "x."

#: Every type SPEC 4 defines. Anything else must be in the ``x.*`` namespace.
EVENT_TYPES = frozenset({
    "agent.hello", "agent.heartbeat", "agent.offline", "agent.bye", "agent.revoked",
    "status.update",
    "chat.message", "chat.reaction",
    "file.put", "file.delete", "file.conflict", "file.move",
    "lock.acquire", "lock.release", "lock.denied",
    "task.create", "task.claim", "task.release", "task.update", "task.done",
    "knowledge.contribution",
    "decision.propose", "decision.vote", "decision.resolve",
    "hub.started", "hub.policy", "hub.notice",
})

PSR_STATES = ("idle", "planning", "working", "reviewing", "blocked", "waiting", "offline")

TASK_STATUSES = ("todo", "doing", "blocked", "review", "done")

CONTRIBUTION_KINDS = ("decision", "design", "finding", "review", "doc", "code", "fix", "answer")

#: Problems that start with this prefix are advisory. ``validate_event`` drops
#: them when ``strict=False`` so that a Hub can treat "any problem at all" as
#: grounds for a 422 without rejecting events over a style nit.
WARNING_PREFIX = "warning: "


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
def make_event(actor: str, session: str, etype: str, body: dict,
               *, event_id: Optional[str] = None, ts: Optional[str] = None) -> dict:
    """Build an unsigned, unsequenced event.

    No ``seq`` (only the Hub may assign one) and no ``sig`` (the caller signs
    afterwards, because only the caller has the key). The author sets ``id`` so
    it can recognise its own event coming back down the stream and so a resend
    after an ambiguous failure deduplicates instead of double-posting.
    """
    if not isinstance(body, dict):
        raise TypeError("event body must be a dict")
    return {
        "v": WIRE_VERSION,
        "id": event_id or new_event_id(),
        "ts": ts or now_rfc3339(),
        "session": session,
        "actor": actor,
        "type": etype,
        "body": body,
    }


def is_known_type(etype: Any) -> bool:
    """True for a SPEC 4 type or anything in the open ``x.*`` extension space."""
    return isinstance(etype, str) and (etype in EVENT_TYPES or etype.startswith(EXTENSION_PREFIX))


def is_hub_authored(event: Mapping[str, Any]) -> bool:
    """Hub-authored events are signed with the session root key, not an agent key."""
    return isinstance(event, Mapping) and event.get("actor") == HUB_ACTOR


def validate_event(event: dict, *, strict: bool = True) -> List[str]:
    """Return a list of problems; an empty list means the event is acceptable.

    ``strict=False`` is what the Hub uses on the ingest path: it keeps every
    hard error (unknown non-extension type, oversized body, malformed ids) and
    drops the advisory ones (a PSR headline ending in a full stop, ``blocked_on``
    set while not blocked). ``strict=True`` is for ``parley doctor`` and for
    authors who want to be told about style problems before they publish.

    Types in the ``x.*`` namespace are never reported in either mode -- SPEC 2.1
    requires them to be stored and relayed.
    """
    problems: List[str] = []
    if not isinstance(event, Mapping):
        return ["event is not an object"]

    version = event.get("v")
    if version != WIRE_VERSION:
        problems.append("v must be %r, got %r" % (WIRE_VERSION, version))

    event_id = event.get("id")
    if event_id is not None and not is_id(event_id, "evt"):
        problems.append("id is not an evt_ identifier: %r" % (event_id,))

    ts = event.get("ts")
    if ts is None:
        problems.append("ts is required")
    else:
        try:
            parse_rfc3339(ts)
        except (ValueError, TypeError) as exc:
            problems.append("ts is not an RFC 3339 timestamp: %s" % exc)

    session = event.get("session")
    if not is_id(session, "ses"):
        problems.append("session is not a ses_ identifier: %r" % (session,))

    actor = event.get("actor")
    if actor != HUB_ACTOR and not is_id(actor, "agt"):
        problems.append("actor must be an agt_ identifier or %r, got %r" % (HUB_ACTOR, actor))

    if "seq" in event:
        seq = event["seq"]
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            problems.append("seq must be a positive integer when present")

    etype = event.get("type")
    if not isinstance(etype, str) or not etype:
        problems.append("type is required and must be a string")
        etype = None
    elif not is_known_type(etype):
        problems.append("unknown event type %r (extensions must be namespaced %s*)"
                        % (etype, EXTENSION_PREFIX))

    body = event.get("body")
    if not isinstance(body, dict):
        problems.append("body must be an object")
        body = None
    else:
        try:
            size = len(canonical(body))
        except (TypeError, ValueError) as exc:
            problems.append("body is not JSON-serialisable: %s" % exc)
        else:
            if size > MAX_BODY_BYTES:
                problems.append("body is %d bytes, limit is %d" % (size, MAX_BODY_BYTES))

    if body is not None and isinstance(etype, str) and etype in EVENT_TYPES:
        problems.extend(_validate_body(etype, body))

    if not strict:
        problems = [p for p in problems if not p.startswith(WARNING_PREFIX)]
    return problems


def _require(body: Mapping[str, Any], problems: List[str], *names: str) -> None:
    for name in names:
        if body.get(name) in (None, ""):
            problems.append("%s is required" % name)


def _require_str(body: Mapping[str, Any], problems: List[str], name: str,
                 *, max_bytes: Optional[int] = None) -> None:
    value = body.get(name)
    if not isinstance(value, str) or not value:
        problems.append("%s must be a non-empty string" % name)
        return
    if max_bytes is not None and len(value.encode("utf-8")) > max_bytes:
        problems.append("%s is longer than %d bytes" % (name, max_bytes))


def _check_path_field(body: Mapping[str, Any], problems: List[str], name: str) -> None:
    value = body.get(name)
    if not isinstance(value, str):
        problems.append("%s must be a string path" % name)
        return
    try:
        normalise_path(value)
    except BadPath as exc:
        problems.append("%s: %s" % (name, exc.message))


def _check_path_list(body: Mapping[str, Any], problems: List[str], name: str) -> None:
    value = body.get(name)
    if not isinstance(value, list) or not value:
        problems.append("%s must be a non-empty list of paths" % name)
        return
    for item in value:
        if not isinstance(item, str):
            problems.append("%s contains a non-string entry" % name)
            continue
        try:
            normalise_path(item)
        except BadPath as exc:
            problems.append("%s: %s" % (name, exc.message))


def _check_enum(body: Mapping[str, Any], problems: List[str], name: str,
                allowed: Union[tuple, frozenset], *, required: bool = True) -> None:
    value = body.get(name)
    if value is None:
        if required:
            problems.append("%s is required" % name)
        return
    if value not in allowed:
        problems.append("%s must be one of %s, got %r" % (name, ", ".join(sorted(allowed)), value))


def _validate_body(etype: str, body: Mapping[str, Any]) -> List[str]:
    """Per-type body checks for the fields other participants actually act on."""
    problems: List[str] = []

    if etype == "agent.hello":
        _require_str(body, problems, "name")
        _require_str(body, problems, "os")
        _require_str(body, problems, "client_version")
        caps = body.get("capabilities")
        if caps is not None and (not isinstance(caps, list)
                                 or any(not isinstance(c, str) for c in caps)):
            problems.append("capabilities must be a list of strings")

    elif etype == "agent.offline":
        _require(body, problems, "agent_id")
        _check_enum(body, problems, "reason", ("timeout", "bye", "revoked"))

    elif etype == "agent.revoked":
        _require(body, problems, "agent_id", "by")

    elif etype == "status.update":
        problems.extend(validate_psr(body))

    elif etype == "chat.message":
        _require_str(body, problems, "text", max_bytes=MAX_CHAT_BYTES)
        to = body.get("to")
        if to is not None and (not isinstance(to, list)
                               or any(not isinstance(t, str) for t in to)):
            problems.append("to must be a list of agent ids or \"all\"")
        for field in ("thread", "reply_to"):
            value = body.get(field)
            if value is not None and not is_id(value, "evt"):
                problems.append("%s must be an evt_ identifier" % field)
        problems.extend(_validate_refs(body.get("refs")))
        _check_enum(body, problems, "format", ("text", "markdown"), required=False)

    elif etype == "chat.reaction":
        target = body.get("target")
        if not is_id(target, "evt"):
            problems.append("target must be an evt_ identifier")
        _require_str(body, problems, "reaction")

    elif etype == "file.put":
        _check_path_field(body, problems, "path")
        if not is_blob_hash(body.get("hash")):
            problems.append("hash must be a sha256: blob id")
        size = body.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            problems.append("size must be a non-negative integer")
        base = body.get("base")
        if base is not None and not is_blob_hash(base):
            problems.append("base must be a sha256: blob id when present")

    elif etype == "file.delete":
        _check_path_field(body, problems, "path")

    elif etype == "file.conflict":
        _check_path_field(body, problems, "path")
        _check_path_field(body, problems, "kept_as")
        for side in ("ours", "theirs"):
            if not isinstance(body.get(side), Mapping):
                problems.append("%s must be an object with hash and agent" % side)

    elif etype == "file.move":
        _check_path_field(body, problems, "from")
        _check_path_field(body, problems, "to")

    elif etype in ("lock.acquire", "lock.release", "lock.denied"):
        _check_path_list(body, problems, "paths")
        if etype == "lock.acquire":
            ttl = body.get("ttl_s")
            if ttl is not None and (not isinstance(ttl, (int, float))
                                    or isinstance(ttl, bool) or ttl <= 0):
                problems.append("ttl_s must be a positive number")
        if etype == "lock.denied":
            _require(body, problems, "held_by")

    elif etype.startswith("task."):
        task_id = body.get("id")
        if not is_id(task_id, "tsk"):
            problems.append("id must be a tsk_ identifier")
        if etype == "task.create":
            _require_str(body, problems, "title")
            priority = body.get("priority")
            if priority is not None and (not isinstance(priority, int)
                                         or isinstance(priority, bool)
                                         or not 1 <= priority <= 5):
                problems.append("priority must be an integer 1..5")
        if etype == "task.update":
            _check_enum(body, problems, "status", TASK_STATUSES)
            problems.extend(_validate_progress(body.get("progress")))
        if etype == "task.done":
            problems.extend(_validate_refs(body.get("refs")))

    elif etype == "knowledge.contribution":
        _check_enum(body, problems, "kind", CONTRIBUTION_KINDS)
        _require_str(body, problems, "title")
        problems.extend(_validate_refs(body.get("refs")))
        supersedes = body.get("supersedes")
        if supersedes is not None and not is_id(supersedes, "evt"):
            problems.append("supersedes must be an evt_ identifier")

    elif etype == "decision.propose":
        _require(body, problems, "id")
        _require_str(body, problems, "question")
        options = body.get("options")
        if not isinstance(options, list) or not options:
            problems.append("options must be a non-empty list")
        else:
            for option in options:
                if not isinstance(option, Mapping) or not option.get("key"):
                    problems.append("each option needs a key")
                    break
        _check_enum(body, problems, "quorum", ("any", "majority", "all"), required=False)

    elif etype == "decision.vote":
        _require(body, problems, "id", "option")

    elif etype == "decision.resolve":
        _require(body, problems, "id", "option")

    elif etype == "hub.notice":
        _require_str(body, problems, "text")

    return problems


def _validate_refs(refs: Any) -> List[str]:
    """``refs`` is what makes citation-based Ledger scoring possible, so a
    malformed one is worth complaining about rather than silently dropping."""
    if refs is None:
        return []
    if not isinstance(refs, list):
        return ["refs must be a list"]
    problems: List[str] = []
    for ref in refs:
        if not isinstance(ref, Mapping):
            problems.append("each ref must be an object with kind and value")
            continue
        if ref.get("kind") not in ("file", "event", "task"):
            problems.append("ref kind must be file, event or task")
        if not ref.get("value"):
            problems.append("ref value is required")
    return problems


def _validate_progress(progress: Any) -> List[str]:
    if progress is None:
        return []
    if isinstance(progress, bool) or not isinstance(progress, (int, float)):
        return ["progress must be a number between 0.0 and 1.0"]
    if not 0.0 <= float(progress) <= 1.0:
        return ["progress must be between 0.0 and 1.0, got %r" % (progress,)]
    return []


def validate_psr(body: dict) -> List[str]:
    """Validate a Parley Standing Report (SPEC 6).

    Advisory findings are prefixed with ``warning: `` so a caller can tell a
    "this will not render usefully" problem from a "this is not a PSR" one. An
    unknown ``state`` is only a warning because SPEC 6.1 requires consumers to
    render it as ``unknown`` rather than crash -- rejecting the event would lose
    the report entirely, which is worse.
    """
    problems: List[str] = []
    if not isinstance(body, Mapping):
        return ["PSR must be an object"]

    state = body.get("state")
    if state is None:
        problems.append("state is required")
    elif state not in PSR_STATES:
        problems.append(WARNING_PREFIX + "unknown state %r; consumers will show 'unknown'"
                        % (state,))

    headline = body.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        problems.append("headline is required and must be a non-empty string")
    else:
        if len(headline) > MAX_HEADLINE_CHARS:
            problems.append("headline is %d characters, limit is %d"
                            % (len(headline), MAX_HEADLINE_CHARS))
        if headline.rstrip().endswith("."):
            problems.append(WARNING_PREFIX + "headline should not end with a full stop")

    detail = body.get("detail")
    if detail is not None and not isinstance(detail, str):
        problems.append("detail must be a string")

    focus = body.get("focus")
    if focus is not None:
        if not isinstance(focus, list):
            problems.append("focus must be a list of workspace-relative paths")
        else:
            if len(focus) > MAX_FOCUS_PATHS:
                problems.append("focus lists %d paths, limit is %d"
                                % (len(focus), MAX_FOCUS_PATHS))
            for item in focus:
                if not isinstance(item, str):
                    problems.append("focus contains a non-string entry")
                    continue
                try:
                    normalise_path(item)
                except BadPath as exc:
                    problems.append("focus: %s" % exc.message)

    task = body.get("task")
    if task is not None and not is_id(task, "tsk"):
        problems.append("task must be a tsk_ identifier")

    problems.extend(_validate_progress(body.get("progress")))

    blocked_on = body.get("blocked_on")
    if blocked_on is not None:
        if not isinstance(blocked_on, Mapping):
            problems.append("blocked_on must be an object")
        elif not blocked_on.get("reason"):
            problems.append("blocked_on needs a reason")
        if state != "blocked":
            problems.append(WARNING_PREFIX + "blocked_on is set but state is %r" % (state,))

    needs = body.get("needs")
    if needs is not None and (not isinstance(needs, list)
                              or any(not isinstance(n, str) for n in needs)):
        problems.append("needs must be a list of strings")

    eta_s = body.get("eta_s")
    if eta_s is not None and (isinstance(eta_s, bool)
                              or not isinstance(eta_s, (int, float)) or eta_s < 0):
        problems.append("eta_s must be a non-negative number of seconds")

    since = body.get("since")
    if since is not None:
        try:
            parse_rfc3339(since)
        except (ValueError, TypeError):
            problems.append("since must be an RFC 3339 timestamp")

    return problems


# ---------------------------------------------------------------------------
# Paths (SPEC 7.1) -- the security boundary
# ---------------------------------------------------------------------------
#: Device names that Windows resolves *anywhere* on the filesystem, with or
#: without an extension. Writing to one does not create a file; it talks to the
#: device. A hostile peer must not be able to aim a sync at the printer port.
_WINDOWS_RESERVED = frozenset(
    ["con", "prn", "aux", "nul", "conin$", "conout$"]
    + ["com%d" % n for n in range(1, 10)]
    + ["lpt%d" % n for n in range(1, 10)]
)

#: Characters that change how a name is *displayed* without changing what gets
#: opened -- the classic "invoice<RLO>gpj.exe" trick -- plus the byte-order mark,
#: which silently becomes part of a filename. Written as codepoints on purpose:
#: a literal here would be invisible in a diff, which is the whole problem.
_DECEPTIVE_CHARS = frozenset(chr(cp) for cp in (
    0x200E, 0x200F,                                     # LRM, RLM
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,             # LRE, RLE, PDF, LRO, RLO
    0x2066, 0x2067, 0x2068, 0x2069,                     # LRI, RLI, FSI, PDI
    0xFEFF,                                             # BOM / zero-width no-break space
))


def normalise_path(p: str) -> str:
    """Validate and normalise a wire path (SPEC 7.1); raise :class:`BadPath` otherwise.

    The output is workspace-relative, POSIX-separated, NFC-normalised, and
    idempotent under a second call -- the Hub stores exactly what this returns.

    Beyond the letter of SPEC 7.1 this also rejects, on every platform so that
    behaviour is identical everywhere:

    * ``:`` anywhere. On Windows ``notes.txt:hidden`` writes an NTFS alternate
      data stream rather than the file you can see, and ``C:`` is a drive.
    * Windows reserved device names (``con``, ``nul``, ``com1`` ...).
    * Segments ending in a dot or a space, which Windows silently strips, so
      ``evil.`` and ``evil`` become the same file.
    * C0/C1 control characters and bidirectional-override characters.

    ``.`` segments are dropped as the no-ops they are; ``..`` is always refused.
    """
    if not isinstance(p, str):
        raise BadPath("path must be a string, got %s" % type(p).__name__)

    text = unicodedata.normalize("NFC", p)
    if not text or not text.strip():
        raise BadPath("path is empty")

    for ch in text:
        if ord(ch) < 0x20 or ord(ch) == 0x7F or 0x80 <= ord(ch) <= 0x9F:
            raise BadPath("path contains a control character")
        if ch in _DECEPTIVE_CHARS:
            raise BadPath("path contains a bidirectional or zero-width control character")

    # A backslash is never a literal character in a wire path (SPEC 7.1); treating
    # it as a separator here means a Windows-style traversal is caught by the same
    # segment checks as a POSIX one rather than slipping through as a filename.
    text = text.replace("\\", "/")

    if ":" in text:
        raise BadPath("path may not contain ':' (drive letter or NTFS alternate "
                      "data stream)")
    if text.startswith("/"):
        raise BadPath("path must be workspace-relative, not absolute")
    if text.endswith("/"):
        raise BadPath("path must not end with a separator")

    segments: List[str] = []
    for raw in text.split("/"):
        if raw == "":
            raise BadPath("path contains an empty segment")
        if raw == ".":
            continue
        if raw == "..":
            raise BadPath("path contains a '..' segment")
        if raw != raw.rstrip(". "):
            raise BadPath("path segment %r ends with a dot or a space" % raw)
        if raw.split(".", 1)[0].lower() in _WINDOWS_RESERVED:
            raise BadPath("path segment %r is a reserved device name" % raw)
        if len(raw.encode("utf-8")) > MAX_PATH_SEGMENT_BYTES:
            raise BadPath("path segment is longer than %d bytes" % MAX_PATH_SEGMENT_BYTES)
        segments.append(raw)

    if not segments:
        raise BadPath("path resolves to nothing")

    result = "/".join(segments)
    if len(result.encode("utf-8")) > MAX_PATH_BYTES:
        raise BadPath("path is longer than %d bytes" % MAX_PATH_BYTES)
    return result


def _is_within(child: Path, parent: Path) -> bool:
    """3.9-compatible ``Path.is_relative_to``, with a case-insensitive fallback.

    ``relative_to`` already casefolds on Windows flavours; the string comparison
    underneath is a belt-and-braces check for filesystems that are
    case-insensitive on a platform whose ``PurePath`` flavour is not.
    """
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        pass
    child_s = os.path.normcase(str(child))
    parent_s = os.path.normcase(str(parent)).rstrip(os.sep)
    return child_s == parent_s or child_s.startswith(parent_s + os.sep)


def safe_join(workspace: Union[str, Path], wire_path: str) -> Path:
    """Resolve a wire path inside a workspace, or raise :class:`BadPath`.

    This is the last check before a byte from the network reaches the
    filesystem. ``resolve()`` is what makes it real: it expands every symlink on
    the way, so a workspace containing ``logs -> /var/log`` cannot be used to
    write outside, even though the wire path itself was perfectly well-formed.

    One honest limitation: the check and the subsequent ``open()`` are not
    atomic, so a local process that can create symlinks inside the workspace
    could in principle swap one in between. Parley does not defend against an
    attacker who already has write access to your workspace -- see
    ``docs/SECURITY.md``.
    """
    relative = normalise_path(wire_path)
    root = Path(workspace).expanduser().resolve()
    target = (root / relative).resolve()
    if target == root or not _is_within(target, root):
        raise BadPath("path %r escapes the workspace" % wire_path,
                      detail={"path": wire_path},
                      hint="Wire paths are workspace-relative; this one resolved outside it.")
    return target


# ---------------------------------------------------------------------------
# Human rendering
# ---------------------------------------------------------------------------
def event_summary(event: Mapping[str, Any]) -> str:
    """One line per event, for ``parley watch`` and for log lines.

    Never prints anything that is not already in the log, and truncates
    aggressively: this is a tail, not an archive.
    """
    if not isinstance(event, Mapping):
        return "<malformed event>"
    seq = event.get("seq")
    seq_text = "%6d" % seq if isinstance(seq, int) else "     -"
    ts = str(event.get("ts") or "")
    clock = ts[11:23] if len(ts) >= 23 else ts
    actor = event.get("actor")
    who = HUB_ACTOR if actor == HUB_ACTOR else short(str(actor or "?"))
    etype = str(event.get("type") or "?")
    body = event.get("body")
    detail = _summarise_body(etype, body if isinstance(body, Mapping) else {})
    return "%s %s %-4s %-22s %s" % (seq_text, clock, who, etype, detail)


def _summarise_body(etype: str, body: Mapping[str, Any]) -> str:
    def text(value: Any, limit: int = 90) -> str:
        line = " ".join(str(value).split())
        return line if len(line) <= limit else line[:limit - 1] + "…"

    if etype == "chat.message":
        return text(body.get("text", ""))
    if etype == "status.update":
        state = body.get("state", "?")
        progress = body.get("progress")
        suffix = "" if progress is None else " (%d%%)" % round(float(progress) * 100)
        return "[%s] %s%s" % (state, text(body.get("headline", ""), 70), suffix)
    if etype in ("file.put", "file.delete"):
        return text(body.get("path", ""))
    if etype == "file.move":
        return "%s -> %s" % (text(body.get("from", ""), 40), text(body.get("to", ""), 40))
    if etype == "file.conflict":
        return "%s kept as %s" % (text(body.get("path", ""), 40),
                                  text(body.get("kept_as", ""), 40))
    if etype.startswith("lock."):
        paths = body.get("paths")
        return text(", ".join(paths) if isinstance(paths, list) else "")
    if etype == "knowledge.contribution":
        return "%s: %s" % (body.get("kind", "?"), text(body.get("title", ""), 70))
    if etype.startswith("task."):
        bits = [str(body.get("id", "?"))]
        for field in ("title", "status", "note", "reason", "result"):
            if body.get(field):
                bits.append(text(body[field], 50))
        return " ".join(bits)
    if etype.startswith("decision."):
        return text(body.get("question") or body.get("option") or body.get("id", ""))
    if etype == "agent.hello":
        return "%s (%s)" % (body.get("name", "?"), body.get("kind", "?"))
    if etype in ("agent.offline", "agent.revoked"):
        return "%s %s" % (body.get("agent_id", "?"), body.get("reason", ""))
    if etype == "hub.notice":
        return text(body.get("text", ""))
    if etype == "agent.heartbeat":
        return ""
    return text(", ".join(sorted(body)), 60)
