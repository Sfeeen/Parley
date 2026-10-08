"""Pigeonhole mode (SPEC 10) - participation with files alone.

This is what makes "any agent can join" true rather than aspirational. An agent
that can only read and write files - a shell script, a cron job, an LLM harness
with a filesystem tool and nothing else - gets the whole parley through
``<workspace>/.parley/``:

===================  =========  ===============================================
File                 Direction  Content
===================  =========  ===============================================
``inbox.jsonl``      Hub -> you Every event, one JSON object per line, appended.
``outbox.jsonl``     you -> Hub Append ``{"type": ..., "body": {...}}`` lines.
``outbox.ack.jsonl`` Hub -> you What was published, with the ``seq`` it got.
``outbox.errors.jsonl`` Hub -> you Lines we could not publish, and why.
``roster.json``      Hub -> you Who is here and their latest PSR.
``state.json``       Hub -> you The ``/v1/state`` snapshot.
``chat.md``          Hub -> you A readable rolling transcript.
``me.json``          you -> Hub Your current PSR; re-emitted for freshness.
===================  =========  ===============================================

The design assumption is that **the agent on the other end is sloppy**. It will
write invalid JSON, a final line with no newline, two lines in one write, a
``body`` that is a string, or a file it truncated by accident. None of those may
lose data or crash the daemon, and every one of them must be *visible* to the
agent that caused it - hence ``outbox.errors.jsonl``, which is the only channel
we have back to a participant that cannot read our logs.

Exactly-once publishing without a transaction
---------------------------------------------
``outbox.jsonl`` is consumed with a persisted **byte offset**. Only whole lines
(those terminated by ``\\n``) are consumed, so a half-written final line is
never parsed. The offset is persisted *after* publishing, which leaves the
classic window: crash between publish and cursor-write and we would republish.

We close that window by not needing it closed: each line's event id is derived
deterministically from ``(agent_id, byte offset, line bytes)``. A replay after a
crash therefore produces the **same** ``event.id``, and the Hub's ``(actor, id)``
deduplication (SPEC 5.2) returns the original ``seq`` instead of appending a
second copy. Idempotence by construction beats a two-phase commit we cannot
have on a plain file.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import errors, protocol
from ..config import workspace_state_dir
from ..jsonutil import atomic_write, dumps, loads, now_rfc3339

log = logging.getLogger("parley.client.pigeonhole")

INBOX = "inbox.jsonl"
OUTBOX = "outbox.jsonl"
OUTBOX_CURSOR = "outbox.cursor.json"
OUTBOX_ACK = "outbox.ack.jsonl"
OUTBOX_ERRORS = "outbox.errors.jsonl"
ROSTER = "roster.json"
STATE = "state.json"
CHAT_MD = "chat.md"
ME = "me.json"

#: `chat.md` is a convenience, not a log. Keep it small enough that an agent can
#: read the whole thing into a prompt; `inbox.jsonl` is the complete record.
CHAT_MD_MAX_BYTES = 512 * 1024
CHAT_MD_KEEP_BYTES = 256 * 1024

#: Longest outbox line we will even try to parse. A runaway writer must not be
#: able to make us buffer a gigabyte.
MAX_OUTBOX_LINE = 1024 * 1024

#: Cap on the stored copy of a bad line in outbox.errors.jsonl.
ERROR_EXCERPT = 2000


def _retry_open(path: Path, mode: str, attempts: int = 5):
    """Open a file, tolerating a brief Windows share-violation (SPEC 10).

    On Windows a file the agent is still writing is locked, and the correct
    response is to come back in a few milliseconds, not to declare an error.
    """
    delay = 0.03
    last: Optional[OSError] = None
    for i in range(attempts):
        try:
            return open(path, mode)
        except FileNotFoundError:
            return None
        except PermissionError as exc:  # Windows share violation
            last = exc
            if i == attempts - 1:
                break
            time.sleep(delay)
            delay = min(0.4, delay * 2)
        except OSError as exc:
            last = exc
            break
    if last is not None:
        log.warning("pigeonhole: cannot open %s (%s)", path, last)
    return None


def _append_line(path: Path, text: str) -> bool:
    """Append one newline-terminated line, with the same Windows tolerance."""
    fh = _retry_open(path, "a")
    if fh is None:
        # The file may genuinely not exist yet (first write after a wipe).
        try:
            fh = open(path, "a")
        except OSError as exc:
            log.warning("pigeonhole: cannot append to %s (%s)", path, exc)
            return False
    try:
        with fh:
            fh.write(text if text.endswith("\n") else text + "\n")
            fh.flush()
        return True
    except OSError as exc:
        log.warning("pigeonhole: write to %s failed (%s)", path, exc)
        return False


class Pigeonhole:
    """Mirrors the log into ``.parley/`` and publishes what an agent appends."""

    def __init__(self, client: Any, workspace: Path) -> None:
        self.client = client
        self.workspace = Path(workspace).resolve()
        self.dir = workspace_state_dir(self.workspace)

        self.inbox = self.dir / INBOX
        self.outbox = self.dir / OUTBOX
        self.cursor_path = self.dir / OUTBOX_CURSOR
        self.ack_path = self.dir / OUTBOX_ACK
        self.errors_path = self.dir / OUTBOX_ERRORS
        self.roster_path = self.dir / ROSTER
        self.state_path = self.dir / STATE
        self.chat_path = self.dir / CHAT_MD
        self.me_path = self.dir / ME

        self._lock = threading.Lock()
        self._names: Dict[str, str] = {}
        self._last_chat_date = ""
        self._state_dirty = True
        self._last_state_refresh = 0.0
        self._me_signature = ""
        self._me_cache: Optional[Dict[str, Any]] = None
        self._published = 0

        self._ensure_files()
        self._seed_names()

    # ------------------------------------------------------------------ #
    # setup
    # ------------------------------------------------------------------ #
    def _ensure_files(self) -> None:
        """Create the two agent-facing files so a newcomer can see the interface.

        An agent that opens ``.parley/`` and finds nothing concludes the feature
        does not exist. An empty ``outbox.jsonl`` is an invitation.
        """
        for path in (self.inbox, self.outbox):
            try:
                if not path.exists():
                    path.touch()
            except OSError as exc:
                log.warning("pigeonhole: cannot create %s (%s)", path, exc)
        if not self.chat_path.exists():
            self._write_chat_header()

    def _seed_names(self) -> None:
        """Pre-load agent names from the last ``roster.json``.

        Without this, the first lines of ``chat.md`` after a restart read
        "agt_f85261eb left" - we only learn names from `agent.hello`, and the
        stream resumes past the hellos that happened before we last stopped.
        """
        try:
            doc = loads(self.roster_path.read_bytes())
        except Exception:  # noqa: BLE001 - absent or stale is fine
            return
        if not isinstance(doc, dict):
            return
        for rec in doc.get("agents") or []:
            if isinstance(rec, dict):
                aid, name = rec.get("agent_id"), rec.get("name")
                if isinstance(aid, str) and isinstance(name, str) and name:
                    self._names[aid] = name

    # ------------------------------------------------------------------ #
    # inbound
    # ------------------------------------------------------------------ #
    def on_event(self, event: dict) -> None:
        """Mirror one event into the agent-facing files."""
        self.on_events([event])

    def on_events(self, events: List[dict]) -> None:
        """Batch form - one file open for a whole replay burst, not one per event."""
        if not events:
            return
        lines = []
        for event in events:
            if not isinstance(event, dict):
                continue
            self._learn_names(event)
            try:
                lines.append(dumps(event))
            except Exception as exc:  # noqa: BLE001 - never let one bad event stop the mirror
                log.warning("pigeonhole: could not serialise event %r (%s)", event.get("id"), exc)
        if lines:
            fh = _retry_open(self.inbox, "a")
            if fh is None:
                try:
                    fh = open(self.inbox, "a")
                except OSError as exc:
                    log.warning("pigeonhole: cannot append to %s (%s)", self.inbox, exc)
                    fh = None
            if fh is not None:
                try:
                    with fh:
                        fh.write("\n".join(lines) + "\n")
                        fh.flush()
                except OSError as exc:
                    log.warning("pigeonhole: inbox append failed (%s)", exc)

        chat_lines = []
        for event in events:
            if not isinstance(event, dict):
                continue
            rendered = self._render_chat(event)
            if rendered:
                chat_lines.append(rendered)
            etype = event.get("type") or ""
            if isinstance(etype, str) and (
                etype.startswith("agent.")
                or etype.startswith("status.")
                or etype.startswith("task.")
                or etype.startswith("decision.")
                or etype.startswith("lock.")
                or etype.startswith("file.")
            ):
                with self._lock:
                    self._state_dirty = True
        if chat_lines:
            self._append_chat(chat_lines)

    def _learn_names(self, event: dict) -> None:
        actor = event.get("actor")
        body = event.get("body")
        if event.get("type") == "agent.hello" and isinstance(body, dict):
            agent_id = body.get("agent_id") or actor
            if isinstance(agent_id, str):
                name = body.get("name")
                if isinstance(name, str) and name:
                    self._names[agent_id] = name

    def _label(self, agent_id: Any) -> str:
        if not isinstance(agent_id, str) or not agent_id:
            return "?"
        if agent_id == "hub":
            return "hub"
        return self._names.get(agent_id) or agent_id[:12]

    # ------------------------------------------------------------------ #
    # chat.md
    # ------------------------------------------------------------------ #
    def _write_chat_header(self) -> None:
        creds = getattr(self.client, "creds", None)
        name = getattr(creds, "name", "") or ""
        session = getattr(creds, "session", "") or ""
        fingerprint = getattr(creds, "fingerprint", "") or ""
        header = [
            "# Parley transcript",
            "",
            "_Session `{0}`{1}{2}_".format(
                session,
                " · fingerprint `{0}`".format(fingerprint) if fingerprint else "",
                " · you are **{0}**".format(name) if name else "",
            ),
            "",
            "> Rolling, human-readable view of the parley. The complete, machine-readable",
            "> record is `inbox.jsonl`. To speak, append a line to `outbox.jsonl`:",
            "> `{\"type\": \"chat.message\", \"body\": {\"text\": \"hello\"}}`",
            "",
        ]
        try:
            atomic_write(self.chat_path, ("\n".join(header) + "\n").encode("utf-8"))
        except OSError as exc:
            log.warning("pigeonhole: cannot create chat.md (%s)", exc)

    def _render_chat(self, event: dict) -> str:
        """One event as a readable transcript line, or "" to omit it.

        Deliberately selective: a transcript cluttered with every heartbeat is
        not readable, and readability is this file's entire reason to exist.
        """
        etype = event.get("type")
        body = event.get("body") if isinstance(event.get("body"), dict) else {}
        who = self._label(event.get("actor"))
        clock = _clock(event.get("ts"))

        if etype == "chat.message":
            text = str(body.get("text", "")).rstrip()
            to = body.get("to")
            addressed = ""
            if isinstance(to, list) and to and to != ["all"]:
                addressed = " → " + ", ".join(self._label(t) for t in to if isinstance(t, str))
            quoted = "\n".join("> " + ln for ln in (text.split("\n") or [""]))
            return "\n**{0}** · {1}{2}\n\n{3}\n".format(who, clock, addressed, quoted)

        if etype == "status.update":
            headline = str(body.get("headline", "")).strip()
            state = str(body.get("state", "")).strip() or "unknown"
            if not headline:
                return ""
            return "- `{0}` **{1}** is _{2}_: {3}".format(clock, who, state, headline)

        if etype == "knowledge.contribution":
            return "- `{0}` **{1}** recorded a {2}: **{3}**".format(
                clock, who, body.get("kind", "contribution"), body.get("title", "")
            )

        if etype == "agent.hello":
            kind = body.get("kind", "")
            return "- `{0}` **{1}** joined{2}".format(
                clock, body.get("name") or who, " ({0})".format(kind) if kind else ""
            )

        if etype == "agent.bye":
            reason = body.get("reason")
            return "- `{0}` **{1}** left{2}".format(
                clock, who, " ({0})".format(reason) if reason else ""
            )

        if etype == "agent.offline":
            return "- `{0}` **{1}** went offline ({2})".format(
                clock, self._label(body.get("agent_id")), body.get("reason", "timeout")
            )

        if etype == "task.create":
            return "- `{0}` **{1}** created task `{2}`: {3}".format(
                clock, who, body.get("id", ""), body.get("title", "")
            )
        if etype == "task.claim":
            return "- `{0}` **{1}** claimed task `{2}`".format(clock, who, body.get("id", ""))
        if etype == "task.done":
            return "- `{0}` **{1}** finished task `{2}`".format(clock, who, body.get("id", ""))

        if etype == "decision.propose":
            return "- `{0}` **{1}** proposed a decision: {2}".format(
                clock, who, body.get("question", "")
            )
        if etype == "decision.vote":
            return "- `{0}` **{1}** voted `{2}`".format(clock, who, body.get("option", ""))
        if etype == "decision.resolve":
            return "- `{0}` decision `{1}` resolved as **{2}**".format(
                clock, body.get("id", ""), body.get("option", "")
            )

        if etype == "file.conflict":
            return "- `{0}` ⚠ conflict on `{1}` - the other version is kept as `{2}`".format(
                clock, body.get("path", ""), body.get("kept_as", "")
            )

        if etype == "hub.notice":
            return "- `{0}` _hub_: {1}".format(clock, body.get("text", ""))

        return ""

    def _append_chat(self, lines: List[str]) -> None:
        date = time.strftime("%Y-%m-%d")
        out: List[str] = []
        with self._lock:
            if date != self._last_chat_date:
                self._last_chat_date = date
                out.append("\n## {0}\n".format(date))
        out.extend(lines)
        if not _append_line(self.chat_path, "\n".join(out)):
            return
        self._roll_chat_if_large()

    def _roll_chat_if_large(self) -> None:
        """Trim the transcript from the front, keeping the recent tail.

        Rewritten atomically so a crash mid-roll cannot leave a truncated file:
        chat.md is what a human looks at when something has gone wrong, and it
        being half-written at exactly that moment would be perverse.
        """
        try:
            size = self.chat_path.stat().st_size
        except OSError:
            return
        if size <= CHAT_MD_MAX_BYTES:
            return
        try:
            with open(self.chat_path, "rb") as fh:
                fh.seek(max(0, size - CHAT_MD_KEEP_BYTES))
                tail = fh.read()
        except OSError as exc:
            log.warning("pigeonhole: cannot roll chat.md (%s)", exc)
            return
        cut = tail.find(b"\n")
        if cut >= 0:
            tail = tail[cut + 1:]
        header = (
            "# Parley transcript (trimmed)\n\n"
            "_Earlier history was trimmed to keep this file readable; "
            "the complete record is `inbox.jsonl`._\n"
        ).encode("utf-8")
        try:
            atomic_write(self.chat_path, header + tail)
            log.info("pigeonhole: rolled chat.md (%d -> %d bytes)", size, len(header) + len(tail))
        except OSError as exc:
            log.warning("pigeonhole: cannot roll chat.md (%s)", exc)

    # ------------------------------------------------------------------ #
    # roster.json / state.json
    # ------------------------------------------------------------------ #
    def refresh_state(self, *, force: bool = False, min_interval: float = 3.0) -> bool:
        """Rewrite ``state.json`` and ``roster.json`` from ``/v1/state``.

        Throttled: the snapshot is a network round trip and a replay burst would
        otherwise ask for it hundreds of times a second.
        """
        now = time.monotonic()
        with self._lock:
            if not force:
                if not self._state_dirty:
                    return False
                if (now - self._last_state_refresh) < min_interval:
                    return False
            self._state_dirty = False
            self._last_state_refresh = now
        try:
            snapshot = self.client.state()
        except errors.ParleyError as exc:
            log.debug("pigeonhole: state refresh failed (%s)", exc)
            with self._lock:
                self._state_dirty = True
            return False
        if not isinstance(snapshot, dict):
            return False

        agents = snapshot.get("agents")
        if isinstance(agents, list):
            for rec in agents:
                if isinstance(rec, dict):
                    aid, name = rec.get("agent_id"), rec.get("name")
                    if isinstance(aid, str) and isinstance(name, str) and name:
                        self._names[aid] = name
            roster = {
                "updated": now_rfc3339(),
                "session": snapshot.get("session", ""),
                "head_seq": snapshot.get("head_seq", 0),
                "agents": agents,
            }
            self._atomic_json(self.roster_path, roster)
        self._atomic_json(self.state_path, snapshot)
        return True

    def _atomic_json(self, path: Path, obj: Any) -> None:
        try:
            atomic_write(path, dumps(obj).encode("utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            log.warning("pigeonhole: cannot write %s (%s)", path.name, exc)

    # ------------------------------------------------------------------ #
    # outbox
    # ------------------------------------------------------------------ #
    def _read_cursor(self) -> int:
        try:
            raw = self.cursor_path.read_bytes()
        except FileNotFoundError:
            return 0
        except OSError as exc:
            log.warning("pigeonhole: cannot read the outbox cursor (%s); restarting at 0", exc)
            return 0
        try:
            doc = loads(raw)
        except Exception:  # noqa: BLE001 - a half-written cursor is a cold start
            log.warning("pigeonhole: outbox cursor is unreadable; restarting at 0")
            return 0
        if isinstance(doc, dict) and isinstance(doc.get("offset"), int) and doc["offset"] >= 0:
            return doc["offset"]
        return 0

    def _write_cursor(self, offset: int, size: int) -> None:
        self._atomic_json(
            self.cursor_path,
            {"offset": int(offset), "file_size": int(size), "updated": now_rfc3339()},
        )

    def drain_outbox(self) -> List[dict]:
        """Publish every whole line the agent has appended since last time.

        Returns the events that were accepted by the Hub, in file order.
        """
        fh = _retry_open(self.outbox, "rb")
        if fh is None:
            return []
        try:
            with fh:
                try:
                    size = os.fstat(fh.fileno()).st_size
                except OSError:
                    return []
                offset = self._read_cursor()
                if offset > size:
                    # Truncated or replaced underneath us. Rewinding is the only
                    # safe choice: the alternative is silently never reading the
                    # agent's new content again. Deterministic event ids mean
                    # anything genuinely re-read is deduplicated by the Hub.
                    log.warning(
                        "pigeonhole: outbox.jsonl shrank (%d < cursor %d); rewinding to 0",
                        size, offset,
                    )
                    offset = 0
                if offset == size:
                    return []
                fh.seek(offset)
                data = fh.read(size - offset)
        except OSError as exc:
            log.warning("pigeonhole: cannot read outbox.jsonl (%s)", exc)
            return []

        last_nl = data.rfind(b"\n")
        if last_nl < 0:
            # No complete line yet. Guard against a writer that never terminates.
            if len(data) > MAX_OUTBOX_LINE:
                self._record_error(
                    offset,
                    "line exceeds {0} bytes without a newline; skipped".format(MAX_OUTBOX_LINE),
                    data[:ERROR_EXCERPT],
                )
                self._write_cursor(offset + len(data), size)
            return []
        complete = data[: last_nl + 1]

        published: List[dict] = []
        acks: List[str] = []
        cursor = offset
        for raw_line in complete.split(b"\n")[:-1]:
            line_offset = cursor
            cursor += len(raw_line) + 1
            stripped = raw_line.strip()
            if not stripped:
                continue
            result = self._publish_line(line_offset, raw_line)
            if result is not None:
                published.append(result)
                acks.append(
                    dumps(
                        {
                            "ts": now_rfc3339(),
                            "offset": line_offset,
                            "id": result.get("id"),
                            "seq": result.get("seq"),
                            "type": result.get("type"),
                        }
                    )
                )

        if acks:
            _append_line(self.ack_path, "\n".join(acks))
        # Persist last: a crash before this point replays the same lines with the
        # same derived ids, which the Hub deduplicates (see the module docstring).
        self._write_cursor(cursor, size)
        if published:
            self._published += len(published)
            log.info("pigeonhole: published %d outbox line(s)", len(published))
        return published

    def _publish_line(self, offset: int, raw: bytes) -> Optional[dict]:
        """Parse and publish one outbox line. Errors are reported, never raised."""
        if len(raw) > MAX_OUTBOX_LINE:
            self._record_error(offset, "line is too large to publish", raw[:ERROR_EXCERPT])
            return None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            self._record_error(offset, "line is not valid UTF-8: {0}".format(exc), raw[:ERROR_EXCERPT])
            return None
        try:
            doc = loads(text)
        except Exception as exc:  # noqa: BLE001 - any parse failure is the agent's bug to see
            self._record_error(offset, "invalid JSON: {0}".format(exc), raw[:ERROR_EXCERPT])
            return None
        if not isinstance(doc, dict):
            self._record_error(offset, "each line must be a JSON object", raw[:ERROR_EXCERPT])
            return None

        etype = doc.get("type")
        if not isinstance(etype, str) or not etype:
            self._record_error(offset, "missing a string \"type\" field", raw[:ERROR_EXCERPT])
            return None
        body = doc.get("body", {})
        if body is None:
            body = {}
        if not isinstance(body, dict):
            self._record_error(offset, "\"body\" must be an object", raw[:ERROR_EXCERPT])
            return None

        supplied_id = doc.get("id")
        event_id = supplied_id if _is_event_id(supplied_id) else self._derive_id(offset, raw)

        try:
            event = self.client.emit(etype, body, event_id=event_id)
        except errors.ParleyError as exc:
            self._record_error(
                offset,
                "hub rejected the event ({0}): {1}".format(getattr(exc, "code", "error"), exc),
                raw[:ERROR_EXCERPT],
                extra={"hint": getattr(exc, "hint", "")},
            )
            return None
        if not isinstance(event, dict):
            return None
        return event

    def _derive_id(self, offset: int, raw: bytes) -> str:
        """Deterministic ``evt_`` id for one outbox line.

        Derived from our agent id, the line's byte offset and its exact bytes,
        so republishing the same line after a crash yields the same id and the
        Hub deduplicates it. Not secret and not required to be unguessable - it
        only has to be stable and collision-resistant.
        """
        agent_id = str(getattr(self.client, "agent_id", "") or "")
        material = agent_id.encode("utf-8") + b"\x00" + str(offset).encode("ascii") + b"\x00" + raw
        return "evt_" + hashlib.sha256(material).hexdigest()[:16]

    def _record_error(
        self, offset: int, message: str, excerpt: bytes, extra: Optional[dict] = None
    ) -> None:
        """Tell the agent what it got wrong, in a file it can read.

        A pigeonhole agent cannot see our logs. Without this file a malformed
        line would vanish silently and the agent would have no way to find out,
        which is the worst possible failure mode for an integration surface.
        """
        record: Dict[str, Any] = {
            "ts": now_rfc3339(),
            "offset": offset,
            "error": message,
            "line": excerpt.decode("utf-8", "replace"),
        }
        if extra:
            record.update({k: v for k, v in extra.items() if v})
        log.warning("pigeonhole: outbox line at byte %d rejected: %s", offset, message)
        _append_line(self.errors_path, dumps(record))

    # ------------------------------------------------------------------ #
    # me.json -> PSR
    # ------------------------------------------------------------------ #
    def refresh_psr_from_me_json(self) -> Optional[dict]:
        """Read ``me.json`` and publish it as a PSR when it changes.

        Returns the current PSR body (whether or not it was re-published) so the
        runtime can re-emit it on the freshness timer (SPEC 6.1), or ``None``
        when the agent has not written one.
        """
        try:
            raw = self.me_path.read_bytes()
        except FileNotFoundError:
            return self._me_cache
        except OSError as exc:
            log.debug("pigeonhole: cannot read me.json (%s)", exc)
            return self._me_cache

        signature = hashlib.sha256(raw).hexdigest()
        if signature == self._me_signature:
            return self._me_cache

        try:
            doc = loads(raw)
        except Exception as exc:  # noqa: BLE001
            self._record_error(-1, "me.json is not valid JSON: {0}".format(exc), raw[:ERROR_EXCERPT])
            self._me_signature = signature
            return self._me_cache
        if not isinstance(doc, dict):
            self._record_error(-1, "me.json must be a JSON object", raw[:ERROR_EXCERPT])
            self._me_signature = signature
            return self._me_cache

        # A PSR written by hand is often *nearly* right; say exactly what is
        # wrong instead of letting the Hub reject it with a 422 the agent will
        # never see.
        problems = protocol.validate_psr(doc)
        fatal = [p for p in problems if "headline" in p.lower()]
        if fatal:
            self._record_error(-1, "me.json is not a valid PSR: " + "; ".join(problems), raw[:ERROR_EXCERPT])
            self._me_signature = signature
            return self._me_cache
        if problems:
            log.info("pigeonhole: me.json has warnings: %s", "; ".join(problems))

        self._me_signature = signature
        self._me_cache = doc
        try:
            self.client.emit("status.update", dict(doc))
            log.info("pigeonhole: published PSR from me.json: %s", doc.get("headline"))
        except errors.ParleyError as exc:
            self._record_error(
                -1, "hub rejected the PSR from me.json: {0}".format(exc), raw[:ERROR_EXCERPT]
            )
        return self._me_cache

    @property
    def current_psr(self) -> Optional[dict]:
        """The last PSR read from ``me.json``, for the freshness re-emit."""
        return self._me_cache

    @property
    def published_count(self) -> int:
        return self._published


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _is_event_id(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("evt_"):
        return False
    rest = value[4:]
    return len(rest) == 16 and all(c in "0123456789abcdef" for c in rest)


def _clock(ts: Any) -> str:
    """``HH:MM:SS`` from a wire timestamp, falling back to the raw value."""
    if isinstance(ts, str) and len(ts) >= 19 and ts[10] == "T":
        return ts[11:19]
    return str(ts or "")
