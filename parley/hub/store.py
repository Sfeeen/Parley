"""Durable state for one parley: the event log, the roster, the file index and
the content-addressed blob store.

Why SQLite and not a JSONL file: the log is written by many threads and read with
``WHERE seq > ?`` on nearly every request (SSE replay, long-poll, index, ledger).
A B-tree on ``seq`` makes that an index seek rather than a full scan, and WAL
means a reader never blocks the writer -- which matters because an SSE replay can
be reading 10 000 rows while another agent is appending.

Threading rules: ONE connection, created with ``check_same_thread=False`` and
guarded by ``self._lock``.  Every statement in this module runs with that lock
held.  ``_lock`` is a *low* lock in the Hub's ordering (see ``server.py``): code
here never calls back out into the Hub, the StateView or the network.

The authoritative ``seq`` counter lives in memory (``_head``) and is only ever
advanced inside a committed transaction, so the in-memory value and the table
cannot disagree.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Sequence, Tuple

from ..errors import BadRequest, NoSuchBlob
from ..jsonutil import dumps, loads, sha256_hex

log = logging.getLogger("parley.hub.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq     INTEGER PRIMARY KEY,
    id      TEXT    NOT NULL,
    actor   TEXT    NOT NULL,
    type    TEXT    NOT NULL,
    ts      TEXT    NOT NULL,
    recv_ts REAL    NOT NULL,
    doc     TEXT    NOT NULL
);
-- dedup lookup: (actor, id) newest-first.  SPEC §5.2.
CREATE INDEX IF NOT EXISTS ix_events_actor_id ON events (actor, id, seq DESC);
-- `read(since, types)` with a type filter.
CREATE INDEX IF NOT EXISTS ix_events_type_seq ON events (type, seq);

CREATE TABLE IF NOT EXISTS agents (
    agent_id  TEXT PRIMARY KEY,
    name      TEXT NOT NULL DEFAULT '',
    kind      TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL DEFAULT 'active',
    last_seen REAL NOT NULL DEFAULT 0,
    rec       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_agents_status ON agents (status);

CREATE TABLE IF NOT EXISTS files (
    path   TEXT PRIMARY KEY,
    hash   TEXT NOT NULL,
    size   INTEGER NOT NULL DEFAULT 0,
    author TEXT NOT NULL DEFAULT '',
    seq    INTEGER NOT NULL DEFAULT 0,
    rec    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_files_seq ON files (seq);

CREATE TABLE IF NOT EXISTS nonces (
    agent_id TEXT NOT NULL,
    nonce    TEXT NOT NULL,
    ts       REAL NOT NULL,
    PRIMARY KEY (agent_id, nonce)
);
CREATE INDEX IF NOT EXISTS ix_nonces_ts ON nonces (ts);

CREATE TABLE IF NOT EXISTS viewer_tokens (
    token_hash TEXT PRIMARY KEY,
    expires    REAL NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    revoked    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

_HEX = "0123456789abcdef"


def normalise_blob_hash(value: str) -> str:
    """Accept ``sha256:<hex>`` or a bare 64-hex digest; return the bare digest.

    The wire form (SPEC §1.1) carries the algorithm prefix, but it is a terrible
    thing to put in a filename on Windows, so on disk we use the bare digest.
    """
    if not isinstance(value, str):
        raise BadRequest("blob hash must be a string", hint="Use 'sha256:<64 hex>'.")
    v = value.strip().lower()
    if v.startswith("sha256:"):
        v = v[7:]
    if len(v) != 64 or any(c not in _HEX for c in v):
        raise BadRequest(
            "malformed blob hash",
            hint="A blob hash is 'sha256:' followed by 64 lowercase hex characters.",
        )
    return v


def wire_blob_hash(value: str) -> str:
    """The ``sha256:<hex>`` form used inside event bodies."""
    return "sha256:" + normalise_blob_hash(value)


class Store:
    """SQLite-backed durable state.  All methods are safe to call from any thread."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:  # Windows / exotic filesystems
            pass
        self.blob_dir = self.state_dir / "blobs"
        self.blob_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._closed = False
        self.db_path = self.state_dir / "parley.db"
        self._db = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=15.0)
        self._db.row_factory = sqlite3.Row
        # Autocommit; this module issues BEGIN/COMMIT explicitly where a group of
        # statements has to land together.
        self._db.isolation_level = None
        self._configure()
        with self._lock:
            self._db.executescript(_SCHEMA)
            row = self._db.execute("SELECT COALESCE(MAX(seq), 0) AS h FROM events").fetchone()
            self._head = int(row["h"])
        self._last_nonce_purge = 0.0
        log.debug("store open at %s head=%d", self.db_path, self._head)

    # ------------------------------------------------------------------ setup

    def _configure(self) -> None:
        if sqlite3.sqlite_version_info < (3, 24, 0):
            # UPSERT (INSERT ... ON CONFLICT DO UPDATE) landed in SQLite 3.24
            # (June 2018).  Failing loudly here beats failing cryptically on the
            # first roster update three hours into a session.
            raise RuntimeError(
                "Parley's Hub needs SQLite 3.24 or newer; this Python is linked "
                "against %s. Upgrade SQLite, or host the Hub on another machine "
                "(any participant can host it)." % sqlite3.sqlite_version
            )
        with self._lock:
            self._db.execute("PRAGMA busy_timeout=15000")
            try:
                mode = self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if str(mode).lower() != "wal":
                    raise sqlite3.DatabaseError(mode)
            except sqlite3.DatabaseError:
                # WAL is unavailable on some network filesystems.  Degrade rather
                # than die (R7) -- correctness is unaffected, concurrency is worse.
                log.warning(
                    "SQLite WAL mode unavailable at %s; falling back to the rollback "
                    "journal (readers will block writers)",
                    self.db_path,
                )
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._db.execute("PRAGMA optimize")
            except sqlite3.Error:
                pass
            self._db.close()

    # ----------------------------------------------------------------- events

    def head_seq(self) -> int:
        with self._lock:
            return self._head

    def append(self, event: dict) -> dict:
        return self.append_many([event])[0]

    def append_many(self, events: List[dict]) -> List[dict]:
        """Assign ``seq`` to each event and commit them as one transaction.

        The caller is responsible for validation, signing and ordering; this is
        the storage primitive only.  Either every event lands or none does, which
        is what makes a batch append atomic (SPEC §5, ``/v1/events``).
        """
        if not events:
            return []
        now = time.time()
        with self._lock:
            start = self._head
            stored: List[dict] = []
            rows: List[Tuple[Any, ...]] = []
            for i, ev in enumerate(events):
                e = dict(ev)
                e["seq"] = start + i + 1
                recv = float(e.pop("_recv_ts", now))
                stored.append(e)
                rows.append(
                    (
                        e["seq"],
                        str(e.get("id", "")),
                        str(e.get("actor", "")),
                        str(e.get("type", "")),
                        str(e.get("ts", "")),
                        recv,
                        dumps(e),
                    )
                )
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.executemany(
                    "INSERT INTO events (seq, id, actor, type, ts, recv_ts, doc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                self._db.execute("COMMIT")
            except BaseException:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            self._head = start + len(events)
            return stored

    def read(
        self,
        since: int = 0,
        limit: int = 1000,
        types: "Optional[List[str]]" = None,
    ) -> List[dict]:
        """Events with ``seq > since``, in seq order.

        ``types`` is a list of dotted *prefixes* (SPEC §5): ``"chat"`` matches
        ``chat.message`` and ``chat.reaction`` but not ``chatter.x``.
        """
        since = max(0, int(since))
        limit = max(0, int(limit))
        if limit == 0:
            return []
        sql = "SELECT doc FROM events WHERE seq > ?"
        args: List[Any] = [since]
        clause = self._type_clause(types, args)
        if clause:
            sql += " AND (" + clause + ")"
        sql += " ORDER BY seq LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [loads(r["doc"]) for r in rows]

    @staticmethod
    def _type_clause(types: "Optional[Sequence[str]]", args: List[Any]) -> str:
        if not types:
            return ""
        parts: List[str] = []
        for raw in types:
            t = str(raw).strip().strip(".")
            if not t:
                continue
            # LIKE wildcards have to be escaped or a type filter of "x_" would
            # silently match everything beginning "x" plus one character.
            esc = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parts.append("type = ? OR type LIKE ? ESCAPE '\\'")
            args.append(t)
            args.append(esc + ".%")
        return " OR ".join(parts)

    def count_events(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"])

    def get_event(self, seq: int) -> "Optional[dict]":
        with self._lock:
            row = self._db.execute("SELECT doc FROM events WHERE seq = ?", (int(seq),)).fetchone()
        return loads(row["doc"]) if row else None

    def find_by_author_id(self, actor: str, event_id: str) -> "Optional[dict]":
        """The most recent event this actor appended under ``event_id``.

        SPEC §5.2's 24 h dedup window is applied by the caller via
        :meth:`find_by_author_id_within`; this form is the unbounded lookup.
        """
        found = self.find_by_author_id_within(actor, event_id, 0.0)
        return found

    def find_by_author_id_within(
        self, actor: str, event_id: str, window_s: float
    ) -> "Optional[dict]":
        """Dedup lookup restricted to the last ``window_s`` seconds (0 = no limit)."""
        if not actor or not event_id:
            return None
        sql = "SELECT doc FROM events WHERE actor = ? AND id = ?"
        args: List[Any] = [actor, event_id]
        if window_s > 0:
            sql += " AND recv_ts >= ?"
            args.append(time.time() - window_s)
        sql += " ORDER BY seq DESC LIMIT 1"
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return loads(row["doc"]) if row else None

    # ----------------------------------------------------------------- agents

    def put_agent(self, rec: dict) -> None:
        agent_id = str(rec["agent_id"])
        with self._lock:
            self._db.execute(
                "INSERT INTO agents (agent_id, name, kind, status, last_seen, rec) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(agent_id) DO UPDATE SET "
                "  name=excluded.name, kind=excluded.kind, status=excluded.status, "
                "  last_seen=excluded.last_seen, rec=excluded.rec",
                (
                    agent_id,
                    str(rec.get("name", "")),
                    str(rec.get("kind", "")),
                    str(rec.get("status", "active")),
                    float(rec.get("last_seen", 0.0)),
                    dumps(rec),
                ),
            )

    def get_agent(self, agent_id: str) -> "Optional[dict]":
        if not agent_id:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT rec, status, last_seen FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        if not row:
            return None
        rec = loads(row["rec"])
        # The columns are authoritative: set_agent_status/touch_agent update them
        # without rewriting the blob.
        rec["status"] = row["status"]
        rec["last_seen"] = row["last_seen"]
        return rec

    def list_agents(self) -> List[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT rec, status, last_seen FROM agents ORDER BY rowid"
            ).fetchall()
        out: List[dict] = []
        for row in rows:
            rec = loads(row["rec"])
            rec["status"] = row["status"]
            rec["last_seen"] = row["last_seen"]
            out.append(rec)
        return out

    def count_agents(self, status: str = "") -> int:
        with self._lock:
            if status:
                row = self._db.execute(
                    "SELECT COUNT(*) AS c FROM agents WHERE status = ?", (status,)
                ).fetchone()
            else:
                row = self._db.execute("SELECT COUNT(*) AS c FROM agents").fetchone()
        return int(row["c"])

    def set_agent_status(self, agent_id: str, status: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE agents SET status = ? WHERE agent_id = ?", (status, agent_id)
            )

    def touch_agent(self, agent_id: str, when: float) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE agents SET last_seen = ? WHERE agent_id = ? AND last_seen < ?",
                (float(when), agent_id, float(when)),
            )

    def agent_key(self, agent_id: str) -> "Optional[bytes]":
        """The agent's HMAC key as raw bytes, or None.

        Kept separate from :meth:`get_agent` so callers that only need to verify a
        signature never have the key sitting in a dict they might log.
        """
        rec = self.get_agent(agent_id)
        if not rec:
            return None
        key_hex = rec.get("key_hex") or ""
        try:
            return bytes.fromhex(key_hex)
        except ValueError:
            return None

    # ------------------------------------------------------------------ blobs

    def _blob_path(self, blob_hash: str) -> Path:
        h = normalise_blob_hash(blob_hash)
        return self.blob_dir / h[:2] / h

    def has_blob(self, blob_hash: str) -> bool:
        try:
            return self._blob_path(blob_hash).is_file()
        except BadRequest:
            return False

    def put_blob(self, blob_hash: str, data: bytes) -> int:
        """Store ``data`` under ``blob_hash``, verifying the digest first.

        A blob whose body does not hash to the advertised value is rejected: the
        hash is the only name the file has, so storing a mismatched body would
        silently corrupt every future fetch of that hash.
        """
        h = normalise_blob_hash(blob_hash)
        actual = sha256_hex(data)
        if not hmac.compare_digest(actual, h):
            raise BadRequest(
                "blob content does not match its declared sha256",
                detail={"declared": "sha256:" + h, "actual": "sha256:" + actual},
                hint="Hash the exact bytes you are uploading (after any gzip "
                "decoding) and send that in X-Parley-Blob-SHA256.",
            )
        target = self._blob_path(h)
        if target.is_file():
            return target.stat().st_size
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / (h + ".tmp-" + os.urandom(6).hex())
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
        return len(data)

    def open_blob(self, blob_hash: str) -> BinaryIO:
        path = self._blob_path(blob_hash)
        try:
            return open(path, "rb")
        except FileNotFoundError:
            raise NoSuchBlob(
                "no such blob",
                detail={"hash": wire_blob_hash(blob_hash)},
                hint="Upload it with POST /v1/blobs before referencing it in a file.put.",
            )

    def read_blob(self, blob_hash: str) -> bytes:
        with self.open_blob(blob_hash) as fh:
            return fh.read()

    def blob_size(self, blob_hash: str) -> "Optional[int]":
        try:
            return self._blob_path(blob_hash).stat().st_size
        except (OSError, BadRequest):
            return None

    def blob_stats(self) -> dict:
        count = 0
        total = 0
        for sub in self.blob_dir.iterdir() if self.blob_dir.is_dir() else []:
            if not sub.is_dir():
                continue
            for f in sub.iterdir():
                if f.is_file() and not f.name.endswith(".tmp"):
                    count += 1
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        return {"count": count, "bytes": total}

    # ------------------------------------------------------------- file index

    def put_file(self, path: str, rec: dict) -> None:
        full = dict(rec)
        full["path"] = path
        with self._lock:
            self._db.execute(
                "INSERT INTO files (path, hash, size, author, seq, rec) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET hash=excluded.hash, size=excluded.size, "
                "  author=excluded.author, seq=excluded.seq, rec=excluded.rec",
                (
                    path,
                    str(full.get("hash", "")),
                    int(full.get("size", 0) or 0),
                    str(full.get("author", "")),
                    int(full.get("seq", 0) or 0),
                    dumps(full),
                ),
            )

    def get_file(self, path: str) -> "Optional[dict]":
        with self._lock:
            row = self._db.execute("SELECT rec FROM files WHERE path = ?", (path,)).fetchone()
        return loads(row["rec"]) if row else None

    def list_files(self) -> Dict[str, dict]:
        with self._lock:
            rows = self._db.execute("SELECT path, rec FROM files ORDER BY path").fetchall()
        return {r["path"]: loads(r["rec"]) for r in rows}

    def list_files_since(self, since: int = 0) -> List[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT rec FROM files WHERE seq > ? ORDER BY seq", (int(since),)
            ).fetchall()
        return [loads(r["rec"]) for r in rows]

    def file_stats(self) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS c, COALESCE(SUM(size), 0) AS b FROM files"
            ).fetchone()
        return {"count": int(row["c"]), "bytes": int(row["b"])}

    def delete_file(self, path: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM files WHERE path = ?", (path,))

    # --------------------------------------------------- viewer tokens, nonces

    @staticmethod
    def _token_hash(token: str) -> str:
        # Tokens are stored hashed so a leaked database file does not hand out
        # live read access, and so lookups compare a digest rather than a secret.
        return hashlib.sha256(("parley/vt/" + token).encode("utf-8")).hexdigest()

    def put_viewer_token(self, token: str, expires: float, label: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO viewer_tokens (token_hash, expires, label, revoked) "
                "VALUES (?, ?, ?, 0) "
                "ON CONFLICT(token_hash) DO UPDATE SET expires=excluded.expires, "
                "  label=excluded.label, revoked=0",
                (self._token_hash(token), float(expires), str(label)),
            )

    def check_viewer_token(self, token: str) -> bool:
        if not token:
            return False
        th = self._token_hash(token)
        with self._lock:
            row = self._db.execute(
                "SELECT token_hash, expires, revoked FROM viewer_tokens WHERE token_hash = ?",
                (th,),
            ).fetchone()
        if not row:
            return False
        if not hmac.compare_digest(str(row["token_hash"]), th):
            return False
        if int(row["revoked"]):
            return False
        return float(row["expires"]) > time.time()

    def revoke_viewer_token(self, token: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE viewer_tokens SET revoked = 1 WHERE token_hash = ?",
                (self._token_hash(token),),
            )

    def revoke_all_viewer_tokens(self) -> None:
        with self._lock:
            self._db.execute("UPDATE viewer_tokens SET revoked = 1")

    def seen_nonce(self, agent_id: str, nonce: str, ts: float, ttl: float) -> bool:
        """Record a request nonce.  Returns True when it is a replay.

        The (agent, nonce) pair is the primary key, so the insert itself is the
        test: a conflict means we have seen it.  Entries older than ``ttl`` are
        outside the replay window and are treated as fresh, then overwritten.

        ``ts`` is the *request's own* timestamp (SPEC 3.3), and it -- not the
        wall clock -- defines the window, so the whole window is injectable and a
        caller can test expiry without sleeping.  The Hub has already bounded
        ``ts`` to +/- 300 s of real time before getting here, so an attacker
        cannot widen or shift the window with a forged value.  Only the purge
        *cadence* is wall-clock: it is housekeeping, not a freshness decision.
        """
        ts = float(ts)
        cutoff = ts - ttl
        with self._lock:
            wall = time.time()
            if wall - self._last_nonce_purge > 60.0:
                self._last_nonce_purge = wall
                # Purge below whichever cutoff is older, so a request whose ts sits
                # at the far edge of the skew window can never evict a nonce that
                # is still inside the *next* request's window.
                self._db.execute("DELETE FROM nonces WHERE ts < ?",
                                 (min(cutoff, wall - ttl),))
            row = self._db.execute(
                "SELECT ts FROM nonces WHERE agent_id = ? AND nonce = ?", (agent_id, nonce)
            ).fetchone()
            if row is not None and float(row["ts"]) >= cutoff:
                return True
            self._db.execute(
                "INSERT INTO nonces (agent_id, nonce, ts) VALUES (?, ?, ?) "
                "ON CONFLICT(agent_id, nonce) DO UPDATE SET ts = excluded.ts",
                (agent_id, nonce, ts),
            )
            return False

    # ------------------------------------------------------------------- meta

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, str(value)),
            )

    def bump_meta_counter(self, key: str, by: int = 1) -> int:
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
            try:
                cur = int(row["v"]) if row else 0
            except (TypeError, ValueError):
                cur = 0
            cur += by
            self._db.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, str(cur)),
            )
            return cur
