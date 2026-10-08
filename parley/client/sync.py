"""Workspace synchronisation (SPEC 7).

This is the module where a bug destroys somebody's files, so the invariants are
stated up front and every method is written to preserve them.

I1  A file in the workspace is never partially written. Remote content lands in
    a temp file **in the same directory**, is fsynced, then ``os.replace``d into
    place. A crash leaves either the old file or the new file, never a mixture.

I2  Nothing is written outside the workspace. Every incoming path is re-validated
    with ``protocol.safe_join`` *and* re-checked against the ignore rules before
    the filesystem is touched, so a malicious or buggy Hub cannot drop a file in
    ``~/.ssh`` or overwrite ``.parley/credentials.json``.

I3  A file is never shipped half-written. A locally-changed file must look
    unchanged for the debounce window, and its ``(size, mtime_ns)`` is
    re-checked *after* hashing: if it moved while we read it, we discard that
    read and try again next poll.

I4  A remote write never bounces back as a local edit (the "echo" problem, see
    :meth:`_publish_change`).

I5  Deletion loses ties (SPEC 7.7). We never delete a local file that has
    unsynchronised local content, and we require a path to be missing on two
    consecutive scans before publishing a delete, so one flaky ``scandir`` can
    never erase a peer's work.

Concurrency
-----------
One lock, ``self._lock`` (an ``RLock``), guards ``_index``, ``_pending``,
``_missing`` and ``_dirty``. It is **never** held across network or
long-running filesystem I/O; hashing and blob transfer happen outside it and the
result is re-validated inside it. That ordering is what makes I3 and I4 race-free
rather than merely likely.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import stat as stat_mod
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .. import errors, protocol
from ..config import workspace_state_dir
from ..jsonutil import atomic_write, dumps, loads, now_rfc3339

log = logging.getLogger("parley.client.sync")

#: Infix used for our own in-flight temp files. The scanner skips anything
#: containing it so a half-downloaded blob is never mistaken for a user file.
TEMP_INFIX = ".parley-tmp-"

#: Chunk size for streaming hashes (SPEC 7.4).
HASH_CHUNK = 1024 * 1024

#: Default debounce: a changed file must hold still this long before we hash
#: and publish it (SPEC 7.3).
DEBOUNCE_MS = 400

#: How many consecutive scans a path must be absent before we publish a delete.
DELETE_CONFIRMATIONS = 2

_POSIX = os.name == "posix"

INDEX_VERSION = 1


def _norm(rel: str) -> str:
    """NFC-normalise a wire path (SPEC 7.1).

    macOS hands us NFD filenames; Linux hands us whatever was written. Without
    normalising, the same file syncs forever between a Mac and a Linux box
    because the two spell its name differently on the wire.
    """
    return unicodedata.normalize("NFC", rel)


def hash_file(path: Path) -> str:
    """Streaming sha256 of a file, as the ``sha256:<hex>`` blob id of SPEC 1.1."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _is_exec(st: os.stat_result) -> bool:
    return bool(st.st_mode & 0o111)


class WorkspaceSync:
    """Two-way file sync between one workspace directory and the Hub."""

    def __init__(
        self,
        client: Any,
        workspace: Path,
        *,
        poll_ms: int = 2000,
        max_blob_bytes: int = 26214400,
        debounce_ms: int = DEBOUNCE_MS,
    ) -> None:
        from .ignore import IgnoreRules  # local import: keeps the module graph shallow

        self.client = client
        self.workspace = Path(workspace).resolve()
        self.poll_ms = max(200, int(poll_ms))
        self.max_blob_bytes = int(max_blob_bytes)
        self.debounce_s = max(0.0, debounce_ms / 1000.0)

        self.state_dir = workspace_state_dir(self.workspace)
        self._index_path = self.state_dir / "index.json"
        self.rules = IgnoreRules.load(self.workspace)

        self._lock = threading.RLock()
        self._index: Dict[str, Dict[str, Any]] = {}
        self._pending: Dict[str, Tuple[int, int, float]] = {}
        self._missing: Dict[str, int] = {}
        self._dirty = False
        self._last_save = 0.0
        self._too_large: Set[str] = set()
        self._case_insensitive = self._probe_case_insensitive()
        self._bootstrapped = False
        #: Diagnostics surfaced by the runtime in ``agent.heartbeat``.
        self.stats: Dict[str, int] = {
            "uploaded": 0, "downloaded": 0, "deleted": 0,
            "conflicts": 0, "skipped_large": 0, "errors": 0,
        }

        self._load_index()

    # ------------------------------------------------------------------ #
    # index persistence
    # ------------------------------------------------------------------ #
    @property
    def index(self) -> Dict[str, Dict[str, Any]]:
        """Snapshot of ``path -> {hash, size, mtime_ns, mode}``."""
        with self._lock:
            return {k: dict(v) for k, v in self._index.items()}

    def _load_index(self) -> None:
        """Read ``.parley/index.json``.

        A corrupt or truncated index is *not* fatal: we start from empty, which
        costs one full rehash and a reconciliation against the Hub's index. That
        is strictly better than trusting half a file and mistaking unchanged
        files for edits.
        """
        try:
            raw = self._index_path.read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("cannot read %s (%s); starting with an empty index", self._index_path, exc)
            return
        try:
            doc = loads(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("index at %s is unreadable (%s); starting fresh", self._index_path, exc)
            return
        if not isinstance(doc, dict) or doc.get("v") != INDEX_VERSION:
            log.info("index version mismatch at %s; starting fresh", self._index_path)
            return
        files = doc.get("files")
        if not isinstance(files, dict):
            return
        clean: Dict[str, Dict[str, Any]] = {}
        for path, rec in files.items():
            if not isinstance(path, str) or not isinstance(rec, dict):
                continue
            if not isinstance(rec.get("hash"), str):
                continue
            clean[_norm(path)] = {
                "hash": rec["hash"],
                "size": int(rec.get("size") or 0),
                "mtime_ns": int(rec.get("mtime_ns") or 0),
                "mode": rec.get("mode"),
                "seq": rec.get("seq") if isinstance(rec.get("seq"), int) else None,
            }
        with self._lock:
            self._index = clean
        log.debug("loaded local index: %d file(s)", len(clean))

    def save_index(self, *, force: bool = False) -> None:
        """Persist the index atomically (temp in the same dir + fsync + replace).

        Throttled to once a second unless forced, because a big scan can touch
        thousands of entries and rewriting the whole index per file would turn a
        sync into an I/O storm.
        """
        with self._lock:
            if not self._dirty and not force:
                return
            now = time.monotonic()
            if not force and (now - self._last_save) < 1.0:
                return
            payload = {
                "v": INDEX_VERSION,
                "workspace": str(self.workspace),
                "updated": now_rfc3339(),
                "files": {k: dict(v) for k, v in self._index.items()},
            }
            self._dirty = False
            self._last_save = now
        try:
            atomic_write(self._index_path, dumps(payload).encode("utf-8"))
        except OSError as exc:
            log.warning("could not persist sync index (%s); will retry", exc)
            with self._lock:
                self._dirty = True

    # ------------------------------------------------------------------ #
    # scanning
    # ------------------------------------------------------------------ #
    def scan_once(self) -> List[dict]:
        """One polling pass. Returns the ``file.*`` events it published."""
        self.rules.reload_if_changed()
        emitted: List[dict] = []
        seen: Set[str] = set()
        now_wall = time.time()
        now_mono = time.monotonic()

        for rel, entry_path, st in self._walk():
            seen.add(rel)
            try:
                emitted.extend(self._consider(rel, entry_path, st, now_wall, now_mono))
            except errors.ParleyError as exc:
                self.stats["errors"] += 1
                log.warning("sync: could not publish %s (%s)", rel, exc)
            except OSError as exc:
                # Vanished or became unreadable between scandir and here. Normal
                # in a live working tree; the next scan will catch up.
                log.debug("sync: skipping %s (%s)", rel, exc)

        emitted.extend(self._consider_deletions(seen))
        self.save_index()
        return emitted

    def _walk(self) -> Iterable[Tuple[str, Path, os.stat_result]]:
        """Yield ``(rel_posix, path, stat)`` for every syncable file.

        Ignored directories are pruned rather than walked, which is what keeps
        the scan cost bounded on a tree containing ``node_modules`` (SPEC 7.3).
        Symlinks are skipped entirely: PARLEY/1 has no representation for them,
        and following one is a straightforward way to write outside the
        workspace.
        """
        stack: List[Tuple[str, Path]] = [("", self.workspace)]
        while stack:
            rel_dir, directory = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError as exc:
                log.debug("sync: cannot scan %s (%s)", directory, exc)
                continue
            for entry in entries:
                name = entry.name
                if TEMP_INFIX in name:
                    continue
                rel = _norm(name if not rel_dir else rel_dir + "/" + name)
                try:
                    if entry.is_symlink():
                        log.debug("sync: skipping symlink %s", rel)
                        continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    if self.rules.ignored(rel, is_dir=True):
                        continue
                    stack.append((rel, Path(entry.path)))
                    continue
                if self.rules.ignored(rel, is_dir=False):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat_mod.S_ISREG(st.st_mode):
                    continue  # fifos, sockets, devices: not syncable content
                yield rel, Path(entry.path), st

    def _consider(
        self,
        rel: str,
        path: Path,
        st: os.stat_result,
        now_wall: float,
        now_mono: float,
    ) -> List[dict]:
        """Decide whether ``rel`` changed, and publish it if it has settled."""
        if st.st_size > self.max_blob_bytes:
            self._notice_too_large(rel, st.st_size)
            return []
        self._too_large.discard(rel)

        stamp = (st.st_size, st.st_mtime_ns)
        with self._lock:
            self._missing.pop(rel, None)
            known = self._index.get(rel)
            if known is not None and known["size"] == stamp[0] and known["mtime_ns"] == stamp[1]:
                # Fast path (SPEC 7.3): unchanged, no hashing.
                self._pending.pop(rel, None)
                mode_changed = _POSIX and known.get("mode") not in (None, _mode_value(st))
                if not mode_changed:
                    return []
            prior = self._pending.get(rel)
            if prior is None or prior[0] != stamp[0] or prior[1] != stamp[1]:
                # First sighting of this exact (size, mtime) - start the clock.
                self._pending[rel] = (stamp[0], stamp[1], now_mono)
                prior = self._pending[rel]

        settled_for = now_mono - prior[2]
        quiet_by_mtime = (now_wall - st.st_mtime) >= self.debounce_s
        if settled_for < self.debounce_s and not quiet_by_mtime:
            # Still being written (or just written this instant). Wait a poll.
            return []

        return self._publish_change(rel, path, stamp)

    def _publish_change(
        self, rel: str, path: Path, stamp: Tuple[int, int]
    ) -> List[dict]:
        """Hash, upload and announce one changed file.

        **This is where the write-echo problem is solved.** Hashing happens
        outside the lock (it is slow); the decision to publish is then made
        inside the lock against three conditions, all of which must hold:

        1. the file's ``(size, mtime_ns)`` is *still* what we hashed - if a
           remote ``apply_event`` replaced the file while we were reading it,
           the stamp moved and we abandon this read rather than publishing
           stale bytes (that is the race that otherwise produces a bogus
           conflict);
        2. the freshly computed hash differs from the hash currently in the
           index - and ``apply_event`` writes the hash it just wrote into the
           index *before* releasing the lock, so content we received from the
           Hub always compares equal here and is never re-published;
        3. the path is still not ignored.

        Together these mean a remote write can never come back out as a local
        edit, no matter how the scan interleaves with it.
        """
        try:
            digest = hash_file(path)
            size = path.stat().st_size
        except OSError as exc:
            log.debug("sync: cannot hash %s (%s)", rel, exc)
            return []

        with self._lock:
            try:
                st = path.stat()
            except OSError:
                return []
            if (st.st_size, st.st_mtime_ns) != stamp:
                # Condition 1 failed: it moved under us. Re-observe next poll.
                self._pending[rel] = (st.st_size, st.st_mtime_ns, time.monotonic())
                return []
            known = self._index.get(rel)
            mode_only = False
            if known is not None and known["hash"] == digest:
                # Condition 2: identical content. Either this is a pure
                # touch/echo (emit nothing) or only the executable bit moved,
                # which is a real change peers need (SPEC 7.4 carries `mode`).
                mode_only = _POSIX and known.get("mode") not in (None, _mode_value(st))
                known["size"] = st.st_size
                known["mtime_ns"] = st.st_mtime_ns
                if _POSIX:
                    known["mode"] = _mode_value(st)
                self._pending.pop(rel, None)
                self._dirty = True
                if not mode_only:
                    return []
            base = known["hash"] if known else None
            self._pending.pop(rel, None)

        # Network I/O happens with the lock released.
        try:
            self._ensure_blob(digest, path, size)
        except errors.ParleyError:
            raise
        except OSError as exc:
            log.warning("sync: cannot read %s for upload (%s)", rel, exc)
            return []

        body: Dict[str, Any] = {"path": rel, "hash": digest, "size": size}
        if _POSIX:
            body["mode"] = _mode_value(st)
        if base and base != digest:
            body["base"] = base
        body["mtime"] = _rfc3339_from_epoch(st.st_mtime)
        event = self.client.emit("file.put", body)

        assigned = event.get("seq") if isinstance(event, dict) else None
        with self._lock:
            try:
                after: Optional[os.stat_result] = path.stat()
            except OSError:
                after = None
            if after is not None and (after.st_size, after.st_mtime_ns) == (st.st_size, st.st_mtime_ns):
                self._index[rel] = {
                    "hash": digest,
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "mode": _mode_value(st) if _POSIX else None,
                    "seq": assigned if isinstance(assigned, int) else None,
                }
            elif after is not None:
                # The file changed while the blob was uploading. Whoever wrote
                # it last owns the index entry now - and if that writer was
                # `apply_event` landing a remote version, it has already
                # recorded the correct hash. Stamping our just-published hash
                # over it would make the next scan see a phantom edit and bounce
                # their content straight back at them. Re-examine instead.
                self._pending[rel] = (after.st_size, after.st_mtime_ns, time.monotonic())
            self._dirty = True

        self.stats["uploaded"] += 1
        log.info("sync: published %s (%d bytes)", rel, size)
        return [event] if isinstance(event, dict) else []

    def _ensure_blob(self, digest: str, path: Path, size: int) -> None:
        """Upload the blob unless the Hub already has it (SPEC 7.4 step 2)."""
        try:
            if self.client.has_blob(digest):
                return
        except errors.ParleyError as exc:
            log.debug("sync: blob existence check failed (%s); uploading anyway", exc)
        with open(path, "rb") as fh:
            data = fh.read()
        if len(data) != size:
            # It grew between stat and read; the caller's stamp re-check will
            # catch it, but do not upload a hash/content mismatch.
            raise OSError("file changed while reading")
        self.client.put_blob(data, digest)

    def _consider_deletions(self, seen: Set[str]) -> List[dict]:
        """Publish deletes for indexed paths that are gone - cautiously (I5)."""
        emitted: List[dict] = []
        with self._lock:
            candidates = [p for p in self._index if p not in seen]
        for rel in candidates:
            if self.rules.ignored(rel):
                # The rules changed, not the file. Forget it locally; publishing
                # a delete would remove a peer's perfectly good file.
                with self._lock:
                    self._index.pop(rel, None)
                    self._dirty = True
                log.info("sync: %s is now ignored; dropped from the local index", rel)
                continue
            target = self.workspace / Path(*rel.split("/"))
            if target.exists():
                continue  # scandir hiccup
            with self._lock:
                count = self._missing.get(rel, 0) + 1
                self._missing[rel] = count
                known = self._index.get(rel)
            if count < DELETE_CONFIRMATIONS or known is None:
                continue
            body: Dict[str, Any] = {"path": rel}
            if known.get("hash"):
                body["base"] = known["hash"]
            try:
                event = self.client.emit("file.delete", body)
            except errors.ParleyError as exc:
                self.stats["errors"] += 1
                log.warning("sync: could not publish delete of %s (%s)", rel, exc)
                continue
            with self._lock:
                self._index.pop(rel, None)
                self._missing.pop(rel, None)
                self._dirty = True
            self.stats["deleted"] += 1
            log.info("sync: published deletion of %s", rel)
            if isinstance(event, dict):
                emitted.append(event)
        return emitted

    def _notice_too_large(self, rel: str, size: int) -> None:
        """Skip an oversized file *visibly* - silence would violate SPEC R6."""
        if rel in self._too_large:
            return
        self._too_large.add(rel)
        self.stats["skipped_large"] += 1
        log.warning(
            "sync: skipping %s (%d bytes > max_blob_bytes %d)", rel, size, self.max_blob_bytes
        )
        try:
            self.client.emit(
                "hub.notice",
                {
                    "text": "skipped {0}: {1} bytes exceeds max_blob_bytes ({2})".format(
                        rel, size, self.max_blob_bytes
                    ),
                    "level": "warn",
                    "path": rel,
                },
            )
        except errors.ParleyError as exc:
            # Some Hubs may reserve the hub.* namespace for themselves. The log
            # line above is the fallback; never let this stop the scan.
            log.debug("sync: hub.notice for oversized file rejected (%s)", exc)

    # ------------------------------------------------------------------ #
    # applying remote changes
    # ------------------------------------------------------------------ #
    def apply_event(self, event: dict) -> None:
        """Apply one remote ``file.*`` event to disk. Never raises."""
        etype = event.get("type")
        if not isinstance(etype, str) or not etype.startswith("file."):
            return
        if event.get("actor") == getattr(self.client, "agent_id", None):
            return  # our own event coming back; already on disk and indexed
        body = event.get("body")
        if not isinstance(body, dict):
            return
        seq = event.get("seq")
        seq = seq if isinstance(seq, int) else None
        try:
            if etype == "file.put":
                self._apply_put(body, seq)
            elif etype == "file.delete":
                self._apply_delete(body)
            elif etype == "file.move":
                self._apply_move(body, seq)
            elif etype == "file.conflict":
                self._apply_conflict(body)
        except errors.ParleyError as exc:
            self.stats["errors"] += 1
            log.error("sync: refusing %s %r (%s)", etype, body.get("path"), exc)
        except OSError as exc:
            self.stats["errors"] += 1
            log.error("sync: failed to apply %s %r (%s)", etype, body.get("path"), exc)

    def _checked_target(self, raw_path: Any) -> Tuple[str, Path]:
        """Validate a wire path and return ``(rel, absolute)``.

        Two independent gates, because they stop different attacks:
        ``normalise_path``/``safe_join`` stop traversal out of the workspace
        (SPEC 7.1), and the ignore check stops a Hub from writing *into* our
        own control directory - ``.parley/credentials.json`` is inside the
        workspace, so containment alone would happily allow it.
        """
        if not isinstance(raw_path, str):
            raise errors.BadPath("file event has no path", hint="Path must be a string.")
        rel = _norm(protocol.normalise_path(raw_path))
        target = protocol.safe_join(self.workspace, rel)
        if self.rules.ignored(rel):
            raise errors.BadPath(
                "refusing to write an ignored path: {0}".format(rel),
                detail={"path": rel},
                hint="The Hub may not write into .parley/, .git/ or anything .parleyignore excludes.",
            )
        return rel, Path(target)

    def _apply_put(self, body: Dict[str, Any], seq: Optional[int] = None) -> None:
        rel, target = self._checked_target(body.get("path"))
        digest = body.get("hash")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise errors.BadEvent("file.put without a usable hash", detail={"path": rel})
        size = body.get("size")
        if isinstance(size, int) and size > self.max_blob_bytes:
            log.warning("sync: not fetching %s (%d bytes exceeds max_blob_bytes)", rel, size)
            return

        with self._lock:
            known = self._index.get(rel)
            if known is not None and known["hash"] == digest:
                return  # idempotent (SPEC 7.5)
            if known is not None and seq is not None:
                applied = known.get("seq")
                if isinstance(applied, int) and seq < applied:
                    # A replay (lost cursor, backfill) delivering a put that the
                    # log has already superseded. Writing it would silently roll
                    # the file back to an older version, and the next scan would
                    # then publish that rollback as if it were a fresh edit.
                    log.debug(
                        "sync: ignoring stale file.put for %s (seq %d < applied %d)",
                        rel, seq, applied,
                    )
                    return

        # Already correct on disk but missing from the index (e.g. after an
        # index loss): index it rather than re-downloading.
        if target.exists() and target.is_file():
            try:
                if hash_file(target) == digest:
                    self._record(rel, target, body.get("mode"), seq)
                    return
            except OSError:
                pass
            if self._has_unpublished_edit(rel, target, digest):
                # R5: this file holds local work that has not reached the Hub.
                # Overwriting it would destroy that work with no record
                # anywhere. Instead we leave it alone; the next scan publishes
                # it with the stale `base`, the Hub arbitrates per SPEC 7.6,
                # and the version we are declining to write here comes straight
                # back as its own `file.put` at the conflict sidecar path -
                # where there is nothing to clobber. Both versions end up on
                # every participant's disk, which is the whole point of R5.
                log.warning(
                    "sync: not overwriting %s - it holds unpublished local changes; "
                    "the hub's version will arrive as a conflict sidecar", rel,
                )
                self.stats["conflicts"] += 1
                with self._lock:
                    self._pending.pop(rel, None)  # publish it on the next scan
                return

        collision = self._case_collision(rel)
        if collision is not None:
            self._handle_case_collision(rel, collision, digest, body)
            return

        data = self.client.get_blob(digest)
        self._write_atomic(rel, target, data, body.get("mode"), seq)
        self.stats["downloaded"] += 1
        log.info("sync: wrote %s (%d bytes) from %s", rel, len(data), body.get("author") or "hub")

    def _has_unpublished_edit(self, rel: str, target: Path, incoming: str) -> bool:
        """True if the local file differs from what we last published.

        Uses the index's ``(size, mtime_ns)`` fast path first, so the common
        case - a file we have not touched - costs one ``stat`` rather than a
        full hash of every incoming put.
        """
        with self._lock:
            known = self._index.get(rel)
        if known is None:
            # Unknown to us but present on disk: content we never published.
            return True
        try:
            st = target.stat()
        except OSError:
            return False
        if (st.st_size, st.st_mtime_ns) == (known["size"], known["mtime_ns"]):
            return False
        try:
            current = hash_file(target)
        except OSError:
            return False
        return current != known["hash"] and current != incoming

    def _apply_delete(self, body: Dict[str, Any]) -> None:
        rel, target = self._checked_target(body.get("path"))
        if not target.exists():
            with self._lock:
                if self._index.pop(rel, None) is not None:
                    self._dirty = True
            return
        with self._lock:
            known = self._index.get(rel)
        try:
            current = hash_file(target)
        except OSError as exc:
            log.warning("sync: cannot verify %s before deleting (%s); keeping it", rel, exc)
            return
        if known is None or known.get("hash") != current:
            # Unsynchronised local content. Deletion loses ties (SPEC 7.7): keep
            # the file; the next scan republishes it.
            log.warning(
                "sync: not deleting %s - it holds local changes that were never published", rel
            )
            self.stats["conflicts"] += 1
            return
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        with self._lock:
            self._index.pop(rel, None)
            self._dirty = True
        self._prune_empty_dirs(target.parent)
        self.stats["deleted"] += 1
        log.info("sync: deleted %s", rel)

    def _apply_move(self, body: Dict[str, Any], seq: Optional[int] = None) -> None:
        src_rel, src = self._checked_target(body.get("from"))
        dst_rel, dst = self._checked_target(body.get("to"))
        digest = body.get("hash")
        with self._lock:
            known = self._index.get(src_rel)
        can_rename = (
            src.exists()
            and known is not None
            and (not isinstance(digest, str) or known.get("hash") == digest)
        )
        if can_rename:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(src), str(dst))
            self._record(dst_rel, dst, (known or {}).get("mode"), seq)
            with self._lock:
                self._index.pop(src_rel, None)
                self._dirty = True
            self._prune_empty_dirs(src.parent)
            log.info("sync: moved %s -> %s", src_rel, dst_rel)
            return
        if isinstance(digest, str):
            # Source missing or wrong: treat as a plain put of the destination.
            self._apply_put({"path": dst_rel, "hash": digest, "size": body.get("size")}, seq)

    def _apply_conflict(self, body: Dict[str, Any]) -> None:
        self.stats["conflicts"] += 1
        log.warning(
            "sync: conflict on %s - the displaced version was kept as %s",
            body.get("path"),
            body.get("kept_as"),
        )

    # ------------------------------------------------------------------ #
    # atomic write
    # ------------------------------------------------------------------ #
    def _write_atomic(
        self, rel: str, target: Path, data: bytes, mode: Any = None,
        seq: Optional[int] = None,
    ) -> None:
        """Write ``data`` to ``target`` without ever exposing a partial file (I1).

        The temp file is created in the *same directory* so that ``os.replace``
        is a rename within one filesystem, which is atomic on POSIX and on NTFS.
        A temp file in ``/tmp`` would turn into a copy across devices and lose
        that guarantee.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / ("." + target.name + TEMP_INFIX + secrets.token_hex(4))
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            if _POSIX:
                want = 0o755 if _wants_exec(mode) else 0o644
                try:
                    os.chmod(str(tmp), want)
                except OSError as exc:
                    log.debug("sync: chmod on %s failed (%s)", tmp, exc)
            # On Windows os.replace over an open file raises; retry briefly,
            # because an editor or indexer may hold it for a few milliseconds.
            self._replace_with_retry(tmp, target)
            tmp = None  # type: ignore[assignment]
        finally:
            if tmp is not None:
                try:
                    os.unlink(str(tmp))
                except OSError:
                    pass
        self._fsync_dir(target.parent)
        self._record(rel, target, mode, seq)

    @staticmethod
    def _replace_with_retry(tmp: Path, target: Path, attempts: int = 6) -> None:
        delay = 0.02
        for i in range(attempts):
            try:
                os.replace(str(tmp), str(target))
                return
            except PermissionError:
                # Windows: the destination is open in another process.
                if i == attempts - 1:
                    raise
                time.sleep(delay)
                delay = min(0.5, delay * 2)

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """Make the rename itself durable. Best effort: Windows has no dir fd."""
        if not _POSIX:
            return
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

    def _record(self, rel: str, target: Path, mode: Any = None,
                seq: Optional[int] = None) -> None:
        """Index what we just wrote, **before** the scanner can look at it.

        This single call is the other half of the echo fix: by the time this
        returns, the index holds the exact ``(hash, size, mtime_ns)`` that is on
        disk, so the scanner's fast path sees "unchanged" and, even if it had
        already begun hashing, its hash-vs-index comparison matches and it
        publishes nothing.
        """
        try:
            st = target.stat()
            digest = hash_file(target)
        except OSError as exc:
            log.warning("sync: wrote %s but could not index it (%s)", rel, exc)
            return
        with self._lock:
            previous = self._index.get(rel) or {}
            self._index[rel] = {
                "hash": digest,
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
                "mode": _mode_value(st) if _POSIX else (mode if isinstance(mode, int) else None),
                "seq": seq if seq is not None else previous.get("seq"),
            }
            self._pending.pop(rel, None)
            self._missing.pop(rel, None)
            self._dirty = True
        self.save_index()

    def _prune_empty_dirs(self, directory: Path) -> None:
        """Remove directories emptied by a delete, stopping at the workspace."""
        cur = directory.resolve()
        root = self.workspace
        while cur != root and str(cur).startswith(str(root)):
            try:
                with os.scandir(cur) as it:
                    for _entry in it:
                        return  # not empty
            except OSError:
                return
            try:
                os.rmdir(str(cur))
            except OSError:
                return
            cur = cur.parent

    # ------------------------------------------------------------------ #
    # case-insensitive filesystems (SPEC 7.1)
    # ------------------------------------------------------------------ #
    def _probe_case_insensitive(self) -> bool:
        """Detect a case-insensitive filesystem once, by experiment not by OS name.

        ``sys.platform`` is the wrong test: macOS can be case-sensitive and
        Linux can host a case-insensitive mount. A one-off probe in ``.parley``
        is cheap and always right for the filesystem that actually matters.
        """
        probe = self.state_dir / ".caseprobe"
        try:
            probe.write_bytes(b"parley")
            result = (self.state_dir / ".CASEPROBE").exists()
        except OSError:
            return os.name != "posix"
        finally:
            try:
                probe.unlink()
            except OSError:
                pass
        return bool(result)

    def _case_collision(self, rel: str) -> Optional[str]:
        """Return an indexed path differing from ``rel` only by case, if any."""
        if not self._case_insensitive:
            return None
        folded = rel.casefold()
        with self._lock:
            for known in self._index:
                if known != rel and known.casefold() == folded:
                    return known
        return None

    def _handle_case_collision(
        self, rel: str, existing: str, digest: str, body: Dict[str, Any]
    ) -> None:
        """Never clobber across a case-only collision; publish a conflict instead."""
        short = digest.split(":")[-1][:8]
        sidecar = "{0}.parley-conflict-case-{1}".format(rel, short)
        log.warning(
            "sync: %r collides with %r on this case-insensitive filesystem; "
            "writing the incoming version to %s",
            rel, existing, sidecar,
        )
        self.stats["conflicts"] += 1
        try:
            side_rel, side_path = self._checked_target(sidecar)
            data = self.client.get_blob(digest)
            self._write_atomic(side_rel, side_path, data, body.get("mode"))
        except (errors.ParleyError, OSError) as exc:
            log.error("sync: could not preserve case-collision sidecar for %s (%s)", rel, exc)
            return
        try:
            self.client.emit(
                "file.conflict",
                {
                    "path": rel,
                    "ours": {"hash": (self.index.get(existing) or {}).get("hash", ""),
                             "agent": getattr(self.client, "agent_id", "")},
                    "theirs": {"hash": digest, "agent": body.get("author", "")},
                    "kept_as": sidecar,
                    "reason": "case_collision",
                },
            )
        except errors.ParleyError as exc:
            log.debug("sync: case-collision file.conflict rejected (%s)", exc)

    # ------------------------------------------------------------------ #
    # bootstrap
    # ------------------------------------------------------------------ #
    def bootstrap(self) -> None:
        """Reconcile with the Hub's authoritative index on first join (SPEC 7.4/7.5).

        Policy, chosen so that nothing is ever lost (R5):

        * remote only                -> download it;
        * both, same content         -> just index it;
        * both, different content    -> keep the **local** file in place and
          preserve the remote version alongside it as a
          ``.parley-conflict-<hash>`` sidecar. The following scan then publishes
          the local version with ``base`` set to the Hub's current hash, so the
          Hub accepts it cleanly instead of manufacturing a second conflict;
        * local only                 -> the following scan uploads it.
        """
        try:
            remote = self.client.file_index()
        except errors.ParleyError as exc:
            log.warning("sync: could not fetch the hub file index (%s); falling back to a local scan", exc)
            remote = {}
        for rel_raw, rec in sorted(remote.items()):
            if not isinstance(rec, dict):
                continue
            digest = rec.get("hash")
            if not isinstance(digest, str):
                continue
            try:
                rel, target = self._checked_target(rel_raw)
            except errors.ParleyError as exc:
                log.warning("sync: hub index contains an unusable path %r (%s)", rel_raw, exc)
                continue
            try:
                if not target.exists():
                    self._apply_put({"path": rel, "hash": digest, "size": rec.get("size"),
                                     "mode": rec.get("mode")})
                    continue
                # Fast path: a restart with an intact index should cost one
                # stat per file, not a full rehash of the whole workspace.
                with self._lock:
                    known = self._index.get(rel)
                if known is not None and known.get("hash") == digest:
                    st = target.stat()
                    if (st.st_size, st.st_mtime_ns) == (known["size"], known["mtime_ns"]):
                        continue
                local_hash = hash_file(target)
                if local_hash == digest:
                    self._record(rel, target, rec.get("mode"),
                                 rec.get("seq") if isinstance(rec.get("seq"), int) else None)
                    continue
                self._preserve_remote_version(rel, digest, rec)
                with self._lock:
                    # Give the scanner a `base` the Hub will accept, so our
                    # local copy supersedes rather than conflicts.
                    st = target.stat()
                    self._index[rel] = {
                        "hash": digest,
                        "size": -1,  # deliberately impossible, forces a rescan
                        "mtime_ns": st.st_mtime_ns,
                        "mode": _mode_value(st) if _POSIX else None,
                        "seq": rec.get("seq") if isinstance(rec.get("seq"), int) else None,
                    }
                    self._dirty = True
            except errors.ParleyError as exc:
                log.warning("sync: bootstrap of %s failed (%s)", rel, exc)
            except OSError as exc:
                log.warning("sync: bootstrap of %s failed (%s)", rel, exc)
        self._bootstrapped = True
        self.save_index(force=True)
        log.info("sync: bootstrap complete (%d remote path(s))", len(remote))

    def _preserve_remote_version(self, rel: str, digest: str, rec: Dict[str, Any]) -> None:
        short = digest.split(":")[-1][:8]
        sidecar = "{0}.parley-conflict-{1}".format(rel, short)
        try:
            side_rel, side_path = self._checked_target(sidecar)
        except errors.ParleyError:
            return
        if side_path.exists():
            return
        try:
            data = self.client.get_blob(digest)
        except errors.ParleyError as exc:
            log.warning("sync: could not preserve the remote version of %s (%s)", rel, exc)
            return
        self._write_atomic(side_rel, side_path, data, rec.get("mode"))
        self.stats["conflicts"] += 1
        log.warning(
            "sync: %s differs from the hub's copy; the hub's version is preserved as %s",
            rel, sidecar,
        )

    # ------------------------------------------------------------------ #
    # loop
    # ------------------------------------------------------------------ #
    def run(self, stop: threading.Event) -> None:
        """Poll until ``stop`` is set.

        A transient scan failure must not take the daemon down - a workspace on
        a flaky network mount throws ``OSError`` all the time. But an unbounded
        run of failures means something is genuinely wrong, so we give up after
        a streak and let the runtime report it rather than pretending to sync.
        """
        interval = self.poll_ms / 1000.0
        failures = 0
        if not self._bootstrapped:
            try:
                self.bootstrap()
            except Exception as exc:  # noqa: BLE001
                log.warning("sync: bootstrap failed (%s); continuing with a local scan", exc)
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.scan_once()
                failures = 0
            except errors.ParleyError as exc:
                failures += 1
                log.warning("sync: scan failed (%s) [%d in a row]", exc, failures)
            except OSError as exc:
                failures += 1
                log.warning("sync: scan failed (%s) [%d in a row]", exc, failures)
            if failures >= 20:
                self.save_index(force=True)
                raise errors.TransportError(
                    "workspace sync failed 20 times in a row; giving up",
                    hint="Check that the workspace is readable and the Hub is reachable.",
                )
            elapsed = time.monotonic() - started
            stop.wait(max(0.05, interval - elapsed))
        self.save_index(force=True)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _mode_value(st: os.stat_result) -> int:
    """The only permission bit PARLEY/1 carries: executable or not.

    Syncing the full mode would mean a Windows participant (whose files are all
    0o666) flattening everyone else's permissions on every round trip. The
    executable bit is the one that actually breaks things when lost, so that is
    the one we carry, and on a filesystem without it we simply omit ``mode``.
    """
    return 0o755 if _is_exec(st) else 0o644


def _wants_exec(mode: Any) -> bool:
    return isinstance(mode, int) and bool(mode & 0o111)


def _rfc3339_from_epoch(epoch: float) -> str:
    """Render a filesystem mtime in the SPEC 1.2 wire format.

    Advisory only: the authoritative ordering is the Hub's ``seq``. We send it
    so a human reading the log can see when the file was actually touched.
    """
    import datetime

    dt = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "{0:03d}Z".format(dt.microsecond // 1000)
