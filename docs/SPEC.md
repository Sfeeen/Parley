# PARLEY/1 — Normative Specification

> This document is the **contract**. Every module in this repository is written against it.
> If code and this document disagree, this document is right and the code is a bug.
>
> Status: v1.0.0-draft · Wire version string: `PARLEY/1`

---

## 0. What Parley is

Parley lets two or more autonomous agents — of **any kind** (Claude Code, Cursor, an
OpenAI-driven script, a cron job, a human in a terminal) running on **any OS** — collaborate on
one project. Pointed at this repository, an agent must be able to read `AGENTS.md`, run a
documented command, and be fully participating within one minute.

A parley gives its participants four things:

1. **A shared workspace folder** that is automatically synced between every participant.
2. **A continuous chat** that every participant reads and writes.
3. **A standard way to report what you are working on** right now (the *Standing Report*).
4. **A live webpage** (the *Deck*) showing who is here, what they are doing, how much each has
   contributed, and the chat.

### 0.1 Vocabulary (use these words everywhere — code, docs, UI)

| Term | Meaning |
|---|---|
| **a parley** | One collaboration session. Has an id `ses_…`. |
| **the Hub** | The server process. Exactly one per parley. Any participant may host it. |
| **a participant / an agent** | One connected party. Has an id `agt_…`. |
| **the watchword** | The human-readable invite sentence. Enrolment secret only. |
| **the Deck** | The visualisation webpage served by the Hub at `/`. |
| **PSR** | Parley Standing Report — the "what am I doing" standard (§6). |
| **the Ledger** | The explainable knowledge-contribution scoring (§9). |
| **the workspace** | The synced project folder on each participant's disk. |
| **Pigeonhole mode** | File-only participation via `.parley/inbox.jsonl` / `outbox.jsonl` (§10). |
| **the log** | The Hub's append-only, totally-ordered event log. The one source of truth. |

### 0.2 Design rules (non-negotiable)

- **R1 — Stdlib only.** The reference implementation imports nothing outside the Python 3
  standard library. Optional accelerators may be used *if already installed*, never required.
- **R2 — Python 3.9 floor.** No `match`, no PEP-604 `X | Y` at runtime, no `tomllib`.
  Every module starts with `from __future__ import annotations`.
- **R3 — Any OS.** No POSIX-only syscalls on the hot path. No `inotify`, no `fcntl` requirement,
  no shelling out to `git`/`curl`. Paths handled with `pathlib` + POSIX-style wire paths (§7.1).
- **R4 — Plain HTTP/1.1.** No WebSockets. Live updates use Server-Sent Events, with a long-poll
  fallback for hostile proxies. This survives tunnels, corporate proxies and ancient clients.
- **R5 — Never lose a byte.** A sync conflict always preserves both sides (§7.6). The log is
  append-only; nothing is ever rewritten or deleted in place.
- **R6 — Explainable, not magic.** Every number the Deck shows (especially the Ledger) must be
  traceable to events in the log, and the UI must be able to show that breakdown.
- **R7 — Degrade, don't die.** Loss of the Hub must leave every participant with a complete local
  workspace and a replayable local log. Reconnect must be automatic and idempotent.

---

## 1. Identifiers, time and canonical form

### 1.1 Identifiers

| Kind | Format | Example |
|---|---|---|
| session | `ses_` + 16 lowercase hex | `ses_9f2c41ab77e0d315` |
| agent | `agt_` + 16 lowercase hex | `agt_0c5518aa91be7742` |
| event | `evt_` + 16 lowercase hex | `evt_41d0e8be2a7c9f03` |
| task | `tsk_` + 8 lowercase hex | `tsk_4b19ac72` |
| blob | `sha256:` + 64 lowercase hex | `sha256:e3b0c442…` |

Random ids use `secrets.token_hex(8)`. Ids are opaque; never parse meaning out of them.

### 1.2 Time

All timestamps on the wire are **RFC 3339 UTC with milliseconds and a literal `Z`**:
`2026-10-08T12:34:56.789Z`. Produced by
`datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{micro//1000:03d}Z"`.

Authentication timestamps (§3.3) are **integer Unix seconds**, separately.

Clients MUST NOT trust their own clock for ordering. The Hub assigns `seq`; `seq` is the only
ordering authority. The Hub returns its own time in `hub_time` on enrolment and in the
`X-Parley-Time` response header on every request, so clients can measure and report skew.

### 1.3 Canonical JSON

Used for every signature and every hash of a structured object:

```python
canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False).encode("utf-8")
```

Floats MUST be finite. Objects MUST NOT contain `None`-valued keys — omit the key instead.

---

## 2. The event

Everything that happens in a parley is an event appended to the log.

```json
{
  "v": "PARLEY/1",
  "seq": 1284,
  "id": "evt_41d0e8be2a7c9f03",
  "ts": "2026-10-08T12:34:56.789Z",
  "session": "ses_9f2c41ab77e0d315",
  "actor": "agt_0c5518aa91be7742",
  "type": "chat.message",
  "body": { "text": "I'll take the sync reconciler." },
  "sig": "6b1f…"
}
```

- `v` — wire version. Always `PARLEY/1` in this spec.
- `seq` — assigned by the Hub. Strictly increasing by 1 from 1. **Clients MUST NOT set it.**
- `id` — set by the *author*, so an author can recognise its own event coming back. If absent,
  the Hub assigns one.
- `ts` — set by the author; the Hub additionally records `recv_ts` internally. If the author's
  `ts` is more than 300 s from Hub time, the Hub rewrites it to Hub time and sets
  `body._clock_skew_corrected = true`.
- `actor` — the authoring agent. The Hub MUST reject an event whose `actor` is not the
  authenticated agent, except for Hub-authored events where `actor` is `"hub"`.
- `type` — dotted lowercase, see §4.
- `body` — type-specific object.
- `sig` — hex HMAC-SHA256 over the canonical JSON of the event **with `seq` and `sig` removed**,
  keyed by the agent's key (§3.2). Lets any participant verify authorship independently of the
  Hub. Hub-authored events are signed with the session root key.

### 2.1 Validation

The Hub MUST reject (HTTP 422, §12) an event that: has an unknown `type` whose namespace is not
in the extension space `x.*`; has a `body` larger than 256 KiB; fails signature verification; or
whose `session` does not match the authenticated session.

Unknown fields inside a known `body` MUST be preserved and forwarded untouched — forward
compatibility is a hard requirement. Unknown event types in the `x.*` namespace MUST be accepted,
stored and relayed, and MUST be ignored gracefully by the Deck and the Ledger.

---

## 3. Security

Full threat model in `docs/SECURITY.md`. Normative mechanics here.

### 3.1 The watchword

The invite is a readable sentence a human can say out loud over a phone:

```
copper-otter-climbs-the-quiet-hill
```

- Generated as **5 words** drawn with `secrets.choice` from the bundled wordlist of ≥2048 short,
  unambiguous, non-homophonic English words → **≥55 bits** of entropy.
- Pattern: `adjective-noun-verb-the-adjective-noun` is *not* required; words are drawn
  independently. The connecting `the` is a literal filler word that carries no entropy and is
  inserted at a fixed position purely so the result reads as a sentence. The entropy claim counts
  only the 5 drawn words.
- **Normalisation before use** (so "Copper Otter Climbs the Quiet Hill" works too):
  lowercase → NFKD → strip accents → replace every run of non-`[a-z0-9]` with a single `-` →
  strip leading/trailing `-`.

The watchword is an **enrolment secret only**. It is never a session-long credential.

### 3.2 Key hierarchy

```
watchword ──PBKDF2-HMAC-SHA256(salt=session_id, 200_000 iter, 32 B)──> root_key
root_key ──HKDF-SHA256(info=b"parley/v1/enroll")──> enroll_key     (proves you know the watchword)
root_key ──HKDF-SHA256(info=b"parley/v1/seal")────> seal_key       (optional body encryption, §3.6)
root_key ──HKDF-SHA256(info=b"parley/v1/fingerprint")──> fp_bytes  (verbal verification, §3.5)
Hub mints per agent: agent_key = secrets.token_bytes(32)           (all post-enrolment auth)
```

HKDF-SHA256 is implemented over `hmac`/`hashlib` (RFC 5869). PBKDF2 via
`hashlib.pbkdf2_hmac`. Iteration count is recorded in the session descriptor so it can be
raised later without breaking old sessions.

### 3.3 Request authentication

Every authenticated request carries:

```
X-Parley-Version:   PARLEY/1
X-Parley-Session:   ses_9f2c41ab77e0d315
X-Parley-Agent:     agt_0c5518aa91be7742      (or the literal "enroll" during enrolment)
X-Parley-Timestamp: 1791456896                (integer Unix seconds)
X-Parley-Nonce:     3f8a1c04bb9e7d62          (16 hex, fresh per request)
Authorization:      Parley-HMAC-SHA256 <hex signature>
```

The signature is `HMAC-SHA256(key, string_to_sign)` where the key is the `agent_key`
(or `enroll_key` when enrolling) and:

```
string_to_sign = "\n".join([
    "PARLEY/1",
    METHOD,                       # upper case, e.g. "POST"
    path_with_query,              # exactly as sent, e.g. "/v1/events?since=10"
    sha256_hex(raw_request_body), # sha256 of b"" when there is no body
    timestamp,                    # same value as the header, as a string
    nonce,
    session_id,
    agent_id,                     # or "enroll"
])
```

The Hub MUST reject if: skew > 300 s; the `(agent_id, nonce)` pair was seen within the last
600 s; or the signature does not verify. Comparison MUST use `hmac.compare_digest`.

This gives authentication, integrity and replay protection **over plain HTTP**, which is what
makes LAN use safe without certificates. It does *not* give confidentiality — see §3.6.

### 3.4 Enrolment

`POST /v1/enroll` signed with `enroll_key`, `X-Parley-Agent: enroll`:

```json
{ "session": "ses_…", "agent": { "name": "Ada", "kind": "claude-code",
  "model": "claude-opus-5", "os": "linux", "host": "workbench",
  "client_version": "1.0.0", "capabilities": ["chat","sync","tasks","psr"],
  "workspace_hint": "/home/sven/work" } }
```

Response `201`:

```json
{ "agent_id": "agt_…", "agent_key": "<64 hex, shown exactly once>",
  "session": "ses_…", "fingerprint": "lemon-anchor-fox",
  "hub_time": "2026-10-08T12:34:56.789Z", "seq": 128,
  "policy": { "heartbeat_s": 15, "psr_max_age_s": 30, "max_blob_bytes": 26214400,
              "sealed": false, "poll_ms": 2000 } }
```

The client stores credentials in `<workspace>/.parley/credentials.json`, mode `0600` where the
OS supports it.

**Enrolment policy**, enforced by the Hub, configurable at `init`:

| Option | Default (LAN) | Default (`--public`) | Meaning |
|---|---|---|---|
| `enroll_open` | `true` | `true` | Whether `/v1/enroll` accepts requests at all |
| `enroll_ttl_s` | `0` (no expiry) | `3600` | Watchword stops working after this long |
| `enroll_max_uses` | `0` (unlimited) | `8` | Watchword stops working after N successful enrolments |
| `require_approval` | `false` | `true` | New agents land in `pending` until the host approves |
| `max_agents` | `16` | `16` | Hard cap |

A `pending` agent receives credentials but every write returns `403 pending_approval`, and it may
read nothing but its own status. The host approves with `parley approve <agent_id>` or from the
Deck (host token required).

### 3.5 Verbal fingerprint

`fingerprint = ` first 6 bytes of `HKDF(root_key, info=b"parley/v1/fingerprint")`, rendered as
three words from the same wordlist joined by `-` (e.g. `lemon-anchor-fox`).

The Hub prints it at startup; every client prints it after enrolment. Two humans comparing three
words out loud confirms they joined the *same* parley and that nobody is relaying them to a
different Hub. The client MUST display it prominently and MUST NOT auto-accept a changed
fingerprint for a known session — that is a `fingerprint_changed` hard error.

### 3.6 Confidentiality

Two supported postures:

1. **TLS tunnel (recommended for internet use).** Put the Hub behind `cloudflared`, `ngrok`,
   Tailscale, or a reverse proxy with a real certificate. `docs/DEPLOY.md` covers all four.
   `scripts/tunnel.sh` automates the common cases.
2. **Sealed mode (`--seal`)** for when no TLS is available. Request and response **bodies** are
   encrypted with ChaCha20-Poly1305 under `seal_key`:
   - Header `X-Parley-Seal: v1`.
   - Body becomes raw bytes `nonce(12) || ciphertext || tag(16)`.
   - The AAD is the `string_to_sign` of §3.3, binding ciphertext to method, path and identity.
   - The signature of §3.3 is computed over the **sealed** (outer) body.
   - Nonces are `secrets.token_bytes(12)`; a repeated nonce under the same key MUST abort.
   - Blobs are sealed in independent 256 KiB frames, each `nonce||ct||tag`, AAD =
     `b"parley/blob/v1" || blob_hash || frame_index_u32_be`.

   **Honest performance note:** pure-Python ChaCha20-Poly1305 runs at roughly 1–3 MB/s. It is
   fine for events and chat; it makes large blob transfer slow. The implementation MUST therefore
   try, in order: `cryptography`, `PyNaCl`, then the bundled pure-Python fallback — and MUST log
   which one it selected. Headers and URLs are never encrypted in sealed mode; an observer still
   learns traffic volume and timing.

Sealed mode protects content. It does **not** replace TLS for a Hub on the public internet; it is
the fallback for when you cannot have TLS.

### 3.7 Viewer tokens (for the Deck)

A browser's `EventSource` cannot set headers, so the Deck cannot use §3.3. The Hub mints
**viewer tokens**: opaque `vwr_` + 32 hex, read-only, no write scope, individually revocable,
with an expiry (default 12 h). Passed as `?vt=<token>`.

- `GET /v1/stream?vt=…` and all `GET /v1/deck/*` data endpoints accept a viewer token.
- A viewer token grants **read of chat, roster, PSR, tasks, ledger, and the file index**.
  It grants **no** blob content, **no** writes, and **never** reveals the watchword.
- A separate **host token** (`hst_` + 32 hex, printed once at `init`, stored in the Hub's state
  dir) unlocks the Deck's admin affordances: approving agents, revoking agents, rotating the
  watchword, and the explicit *Reveal invite* action.
- The watchword MUST NOT be rendered on the Deck without a host token, and MUST NOT appear in
  any log line, any event body, or any error message, ever.

### 3.8 Revocation

`POST /v1/admin/revoke {agent_id}` (host token) invalidates the agent key immediately and emits
`agent.revoked`. `POST /v1/admin/rotate-watchword` generates a new watchword and root key for
future enrolments; **existing agent keys keep working** (they do not derive from the watchword),
which is exactly why the two-tier key design exists.

---

## 4. Event types

Namespaces: `agent`, `status`, `chat`, `file`, `lock`, `task`, `knowledge`, `decision`, `hub`,
and the open extension space `x.*`.

### 4.1 `agent.*`

| Type | Body | Notes |
|---|---|---|
| `agent.hello` | `{name, kind, model?, os, host?, client_version, capabilities[], workspace_hint?}` | Emitted by the Hub on successful enrolment and by the client on every reconnect. |
| `agent.heartbeat` | `{psr_seq?, workspace_files?, workspace_bytes?}` | Every `heartbeat_s` (default 15 s). |
| `agent.offline` | `{agent_id, reason: "timeout"\|"bye"\|"revoked"}` | Hub-authored. Emitted after `3 × heartbeat_s` of silence. |
| `agent.bye` | `{reason?}` | Clean departure. |
| `agent.revoked` | `{agent_id, by}` | Hub-authored. |

### 4.2 `status.update` — see §6, the PSR.

### 4.3 `chat.*`

| Type | Body |
|---|---|
| `chat.message` | `{text, to?: [agt_…\|"all"], thread?: evt_…, reply_to?: evt_…, refs?: [{kind:"file"\|"event"\|"task", value}], format?: "text"\|"markdown"}` |
| `chat.reaction` | `{target: evt_…, reaction: string}` |

`text` is ≤ 16 KiB and MUST be treated as untrusted when rendered (§8.5). `refs` is what makes
citation-based Ledger scoring possible — agents are instructed to cite what they are responding
to.

### 4.4 `file.*` — see §7.

| Type | Body |
|---|---|
| `file.put` | `{path, hash, size, mode?, base?, mtime?}` |
| `file.delete` | `{path, base?}` |
| `file.conflict` | `{path, ours:{hash,agent}, theirs:{hash,agent}, kept_as}` (Hub-authored) |
| `file.move` | `{from, to, hash}` |

### 4.5 `lock.*` — advisory, cooperative, never enforced by the filesystem.

| Type | Body |
|---|---|
| `lock.acquire` | `{paths:[…], ttl_s (default 600), intent}` |
| `lock.release` | `{paths:[…]}` |
| `lock.denied` | `{paths:[…], held_by}` (Hub-authored) |

A lock is a **social signal**, not a mutex: it makes the Deck show "Ada is editing `hub.py`" and
makes a well-behaved agent pick different work. The Hub MUST still accept writes to locked paths
(R5 — never lose a byte) but MUST flag them with `body.lock_violation = true`.

### 4.6 `task.*`

| Type | Body |
|---|---|
| `task.create` | `{id, title, detail?, tags?[], priority?: 1..5, depends_on?[]}` |
| `task.claim` | `{id}` |
| `task.release` | `{id, reason?}` |
| `task.update` | `{id, status: "todo"\|"doing"\|"blocked"\|"review"\|"done", progress?: 0..1, note?}` |
| `task.done` | `{id, result?, refs?[]}` |

### 4.7 `knowledge.contribution` — the Ledger's primary input, see §9.

```json
{ "kind": "decision|design|finding|review|doc|code|fix|answer",
  "title": "SSE beats WebSockets here",
  "detail": "Survives corporate proxies; stdlib-implementable; long-poll fallback is trivial.",
  "refs": [{"kind":"file","value":"parley/hub/server.py"}],
  "supersedes": "evt_…" }
```

### 4.8 `decision.*` — lightweight consensus, used to divide work without a human referee.

| Type | Body |
|---|---|
| `decision.propose` | `{id, question, options:[{key,label,detail?}], deadline_s?, quorum?: "any"\|"majority"\|"all"}` |
| `decision.vote` | `{id, option, rationale?}` |
| `decision.resolve` | `{id, option, tally}` (Hub-authored when quorum is met or the deadline passes) |

### 4.9 `hub.*` — Hub-authored lifecycle: `hub.started`, `hub.policy`, `hub.notice`.

---

## 5. HTTP API

Base path `/v1`. All request and response bodies are `application/json; charset=utf-8` unless
stated. Every response carries `X-Parley-Time` and `X-Parley-Seq` (current head of the log).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/v1/hello` | none | Unauthenticated discovery: `{v, session, fingerprint, name, agents_online, requires_seal, server_time}`. Never leaks the watchword or the agent roster detail. |
| `POST` | `/v1/enroll` | enroll_key | Join (§3.4). |
| `POST` | `/v1/events` | agent | Append one event or a batch `{events:[…]}` (≤ 64). Returns assigned `seq`s. |
| `GET` | `/v1/events?since=&limit=&wait=&types=` | agent \| viewer | Long-poll fallback. `wait` ≤ 30 s. `types` is a comma-separated prefix filter. |
| `GET` | `/v1/stream?since=&types=` | agent \| viewer(`?vt=`) | SSE live feed. |
| `GET` | `/v1/state` | agent \| viewer | Materialised snapshot: roster, PSR per agent, open tasks, locks, file index summary, ledger. Lets a client or the Deck start without replaying the whole log. |
| `POST` | `/v1/blobs` | agent | Upload. Raw body, `X-Parley-Blob-SHA256`, optional `Content-Encoding: gzip`. |
| `GET` | `/v1/blobs/<sha256>` | agent | Download. `Accept-Encoding: gzip` honoured. |
| `GET` | `/v1/index?since=` | agent \| viewer | Authoritative file index (viewer sees metadata only). |
| `POST` | `/v1/admin/*` | host token | `approve`, `revoke`, `rotate-watchword`, `viewer-token`, `shutdown`. |
| `GET` | `/` and `/deck/*` | viewer | The Deck (§8). |

### 5.1 SSE framing

```
id: 1284
event: parley
data: {"v":"PARLEY/1","seq":1284,…}

: ping
```

- `id:` is the `seq`, so a browser's automatic `Last-Event-ID` reconnect resumes exactly.
- A `: ping` comment every 15 s keeps proxies from idling the connection out.
- On connect with `?since=N`, the Hub replays `N+1…head` before going live. `since=0` means
  "everything"; `since=-1` means "live only".
- The Hub MUST set `Cache-Control: no-store`, `X-Accel-Buffering: no` and MUST flush after every
  event.

### 5.2 Reconnection

Clients implement exponential backoff with full jitter: `min(30, 0.5 × 2^attempt) × random()`,
capped at 30 s, reset on a successful event. On reconnect a client resumes from its last known
`seq`. **All client operations are idempotent by `event.id`** — a resend after an ambiguous
failure MUST NOT produce a duplicate, and the Hub MUST deduplicate on `(actor, id)` within a
24 h window, returning the original `seq`.

---

## 6. PSR — the Parley Standing Report

**The standard every agent follows to say what it is doing.** Normative details in
`docs/STANDING-REPORT.md`; the schema is normative here.

Emitted as `status.update`:

```json
{
  "state": "working",
  "headline": "Wiring the SSE reconnect backoff",
  "detail": "Full-jitter backoff, resume from last seq; testing against a killed hub.",
  "focus": ["parley/client/client.py", "tests/test_reconnect.py"],
  "task": "tsk_4b19ac72",
  "progress": 0.4,
  "blocked_on": { "agent": "agt_…", "reason": "needs the conflict-naming decision" },
  "needs": ["decision on conflict file naming"],
  "eta_s": 900,
  "since": "2026-10-08T12:30:00.000Z"
}
```

### 6.1 Rules

- `state` ∈ `idle` · `planning` · `working` · `reviewing` · `blocked` · `waiting` · `offline`.
  Closed set; unknown values render as `unknown` and MUST NOT crash a consumer.
- `headline` — **required**, ≤ 80 chars, present tense, no trailing period. It is what the Deck
  shows in the roster. "Refactoring the sync reconciler", not "I am going to maybe look at sync".
- `focus` — ≤ 8 workspace-relative POSIX paths (§7.1). Drives the Deck's file heat-map.
- `progress` — 0.0–1.0, optional, monotonic within a task.
- **Freshness contract:** an agent MUST emit a PSR on every state change **and** at least every
  `psr_max_age_s` (default 30 s). A PSR older than `3 × psr_max_age_s` is rendered **stale** on
  the Deck. An agent with no PSR at all is non-conforming and the Deck MUST say so visibly —
  this is deliberate social pressure to follow the standard.
- `blocked_on` with `state != "blocked"` is a validation warning, not an error.

---

## 7. Workspace synchronisation

### 7.1 Paths

Wire paths are **always** workspace-relative, POSIX-separated, NFC-normalised, no leading `/`,
no `.` or `..` segment, no drive letter, no backslash, no trailing slash, ≤ 1024 bytes UTF-8.

The Hub MUST reject any other path with `422 bad_path`. Clients MUST re-validate every path
received from the Hub **before touching the filesystem** — a malicious Hub or peer must not be
able to write outside the workspace. Resolve and confirm containment with
`Path(ws, p).resolve().is_relative_to(Path(ws).resolve())` (implement the 3.9 equivalent).

Case-insensitive filesystems: the Hub keeps the exact path; a client that detects a case-only
collision on its own filesystem MUST emit `file.conflict` rather than clobber.

### 7.2 Ignore rules

Always ignored: `.parley/`, `.git/`, `.hg/`, `.svn/`, `__pycache__/`, `*.pyc`, `node_modules/`,
`.venv/`, `venv/`, `.DS_Store`, `Thumbs.db`, `*.swp`, `*~`, `.#*`, and anything matching
`<workspace>/.parleyignore` (gitignore syntax subset: `#` comments, `!` negation, `/` anchoring,
`*`/`**`/`?` globs, trailing `/` = directory-only).

Files larger than `max_blob_bytes` (default 25 MiB) are skipped with a `hub.notice` naming the
path — silently skipping would violate R6.

### 7.3 Detection

Portable polling (R3): scan every `poll_ms` (default 2000). Fast path compares
`(size, mtime_ns)` against the local index; hash only on change. Debounce 400 ms so a file being
written is not shipped half-finished. Directory scan cost is bounded by the ignore rules.

A client MAY use a platform watcher if one is available, but MUST NOT require it.

### 7.4 Upload

1. Hash the file (`sha256`, streaming, 1 MiB chunks).
2. If the Hub does not have the blob (`HEAD /v1/blobs/<hash>` → 404), `POST /v1/blobs`.
3. Append `file.put {path, hash, size, mode, base}` where `base` is the hash this edit started
   from (the hash in the client's local index), or omitted for a new file.

### 7.5 Download

On receiving `file.put` from another agent: if the local index already has that hash at that
path, ignore (idempotent). Otherwise fetch the blob, write to a temp file **in the same
directory**, `fsync`, then atomically `os.replace` into place, then update the local index. Never
write a partial file into the workspace.

### 7.6 Conflicts — never lose a byte (R5)

The Hub is the arbiter. For `file.put {path, base}`:

- `base` equals the Hub's current hash for `path` → **accept**, becomes current.
- `base` is absent and `path` is unknown → **accept**, becomes current.
- otherwise → **divergence**. The Hub:
  1. accepts the newly-arrived blob as current (last-writer-wins at `path` — predictable),
  2. preserves the *displaced* version at the sidecar path
     `<path>.parley-conflict-<short_agent>-<short_hash>` as its own `file.put`,
  3. emits `file.conflict {path, ours, theirs, kept_as}`.

Every client then holds both versions on disk and the Deck shows a conflict badge. Resolution is
a human/agent decision — delete the sidecar when merged. Parley never auto-merges text; guessing
a merge is worse than showing two files.

### 7.7 Deletes

`file.delete` is honoured only when `base` matches; otherwise the Hub treats it as a divergence
and **keeps** the file, emitting `file.conflict` with `kept_as` = the original path. Deletion
loses data, so it loses ties.

---

## 8. The Deck

A single self-contained page served at `/`. **No external requests of any kind** — no CDN, no web
fonts, no analytics. CSP: `default-src 'self'; connect-src 'self'; img-src 'self' data:`.

### 8.1 Panels

1. **Roster** — one card per agent: colour chip, name, kind + model, OS, online/stale/offline dot,
   PSR `state` badge, `headline`, progress bar, `focus` paths, "blocked on" edge, time in state.
2. **Chat** — the continuous conversation, newest at the bottom, auto-scroll with a
   "jump to latest" affordance once the user scrolls up. System events interleaved behind a
   toggle. Mentions and `refs` rendered as chips.
3. **Ledger** — horizontal stacked bars of contribution share per agent, with a hover/click
   breakdown showing the exact components and the events behind them (R6).
4. **Collaboration graph** — agents as nodes; edges weighted by replies, citations, co-edited
   files and `blocked_on`. This is the "which agents are working together" view.
5. **Activity timeline** — swimlane per agent over time, coloured by PSR state, with file-sync
   and conflict markers.
6. **Workspace** — recent `file.put`s, who authored them, conflict badges, a file heat-map built
   from PSR `focus` and `file.put` frequency.
7. **Tasks** — board grouped by status, showing claimant.
8. **Session bar** — parley name, fingerprint, agent count, Hub uptime, head `seq`, connection
   health, and (host token only) *Reveal invite* / *Approve pending* / *Rotate watchword*.

### 8.2 Behaviour

- Live via `EventSource('/v1/stream?vt=…&since=' + lastSeq)`; initial paint from `/v1/state`.
- Automatic reconnect with the same backoff as §5.2 and a visible "reconnecting" state. The page
  must survive the Hub restarting without a manual refresh.
- Works at 1280 px and at 390 px (phone). Wide panels scroll inside themselves; the body never
  scrolls horizontally.
- Light **and** dark theme via `prefers-color-scheme`, plus a manual toggle that wins.
- Fully keyboard-navigable; respects `prefers-reduced-motion`; contrast ≥ 4.5:1 for text.
- Degrades to long-poll if `EventSource` fails twice in a row.

### 8.3 Agent colour

Deterministic from `agent_id`: hue = `int(agent_id[-4:], 16) % 360`, fixed S/L per theme, so the
same agent is the same colour on every participant's Deck without coordination.

### 8.4 Performance

The Deck must stay smooth with 10 000 events and 16 agents: cap the chat DOM at the most recent
500 nodes with virtualised older history, batch SSE-driven renders with
`requestAnimationFrame`, and never re-layout the whole page per event.

### 8.5 Untrusted content

Everything from the log is attacker-controlled. Render with `textContent`, never `innerHTML`.
No `eval`. Markdown, if rendered at all, goes through a strict allow-list that excludes raw HTML,
`javascript:`/`data:` URLs, and `<img>` from remote origins.

---

## 9. The Ledger — knowledge contribution

**The Ledger must be explainable (R6).** Every point is attributable to an event the user can be
shown. No learned weights, no opaque model.

Default weights, overridable in `<workspace>/.parley/ledger.json`:

```json
{
  "contribution_weights": { "decision": 8, "design": 6, "finding": 5, "fix": 3,
                            "review": 3, "doc": 2, "code": 2, "answer": 1 },
  "surviving_line_points": 0.02,
  "surviving_lines_cap_per_file": 400,
  "task_done_points": 2.0,
  "citation_received_points": 0.5,
  "chat_message_points": 0.05,
  "chat_points_cap": 10.0,
  "decay_half_life_days": 0
}
```

Components per agent:

| Component | Derived from |
|---|---|
| **Contributions** | `knowledge.contribution` events × kind weight |
| **Authored substance** | lines in the *current* version of each text file last written by that agent (blame-lite, capped per file) |
| **Delivery** | `task.done` events the agent claimed |
| **Influence** | times another agent's `chat.message.refs` or `knowledge.contribution.refs` cited this agent's event |
| **Presence** | chat messages, hard-capped, so chattiness cannot beat substance |

The Deck shows absolute points, the percentage share, and the breakdown. `parley ledger --why
<agent>` prints the same thing in the terminal with the contributing event ids.

Explicitly out of scope: judging *quality*. The Ledger measures recorded contribution, and the
docs must say so plainly rather than implying it measures worth.

---

## 10. Pigeonhole mode — participation with files alone

Some agents can only read and write files. They are still first-class participants. The local
daemon (`parley run`) maintains, inside `<workspace>/.parley/`:

| File | Direction | Content |
|---|---|---|
| `inbox.jsonl` | Hub → agent | Every event, one JSON object per line, append-only. |
| `outbox.jsonl` | agent → Hub | The agent appends `{"type":…, "body":…}` lines; the daemon publishes them, then records the assigned seq in `outbox.ack.jsonl` and never re-sends. |
| `roster.json` | Hub → agent | Current agents + their latest PSR. Rewritten atomically. |
| `state.json` | Hub → agent | The §5 `/v1/state` snapshot. Rewritten atomically. |
| `chat.md` | Hub → agent | Human-readable rolling transcript. |
| `me.json` | agent → daemon | The agent's own current PSR; the daemon re-emits it to satisfy the freshness contract. |

`outbox.jsonl` is read with a byte offset cursor so a partially-written final line is never
parsed. Writers MUST append whole lines ending in `\n`. On Windows, the daemon tolerates a
briefly-locked file and retries.

This is the mechanism that makes the "any agent" claim true, and `AGENTS.md` presents it as the
universal fallback.

---

## 11. CLI

```
parley init     [--name NAME] [--workspace DIR] [--port N] [--bind ADDR] [--public]
                [--seal] [--approve] [--words 5]
parley join     --hub URL --invite "watchword" [--name NAME] [--kind KIND]
                [--workspace DIR] [--seal]
parley run      [--workspace DIR] [--no-sync] [--psr-from me.json]
parley say      "message" [--to AGENT] [--reply EVT] [--ref PATH]
parley status   "headline" [--state STATE] [--focus PATH]... [--progress F] [--task ID]
parley know     "title" --kind KIND [--detail TEXT] [--ref PATH]...
parley task     create|claim|update|done|list …
parley watch    [--types PREFIX] [--since N]        # tail the log to stdout
parley roster
parley ledger   [--why AGENT]
parley invite   [--reveal] [--rotate]               # host token required
parley approve  AGENT_ID
parley doctor                                        # diagnose everything (§13)
```

`parley` and `python3 -m parley` are equivalent. Every subcommand supports `--json` for machine
consumption — an agent should never have to parse human prose.

Exit codes: `0` ok · `1` generic error · `2` usage · `3` auth/credential failure ·
`4` cannot reach Hub · `5` fingerprint mismatch.

---

## 12. Errors

```json
{ "error": { "code": "bad_signature", "message": "…", "detail": {…},
             "retryable": false, "hint": "…" } }
```

| HTTP | Codes |
|---|---|
| 400 | `bad_request`, `bad_json` |
| 401 | `bad_signature`, `unknown_agent`, `stale_timestamp`, `replayed_nonce` |
| 403 | `pending_approval`, `revoked`, `enroll_closed`, `read_only_token`, `host_token_required` |
| 404 | `no_such_session`, `no_such_blob`, `no_such_agent` |
| 409 | `seq_conflict`, `duplicate_event` |
| 413 | `too_large` |
| 422 | `bad_event`, `bad_path`, `unknown_type` |
| 429 | `rate_limited` (with `Retry-After`) |
| 503 | `shutting_down` |

The `hint` field is for humans and agents alike and should say what to *do*. Error messages MUST
NOT contain the watchword, any key, or any token.

### 12.1 Rate limits

Per agent, token bucket: 60 events/minute burst 120; 120 blob ops/minute; 10 enrolments/minute
per source address. Exceeding returns 429 with `Retry-After`; clients MUST honour it.

---

## 13. `parley doctor`

Prints a pass/fail table and exits non-zero on any failure:

Python version · stdlib completeness · workspace writability · `.parley` permissions ·
credentials present and parseable · Hub reachable · `/v1/hello` version match · fingerprint match ·
clock skew vs Hub · SSE works · long-poll fallback works · blob round-trip · PSR freshness ·
ignore-rule sanity · free disk · effective crypto backend · listening address reachability
(warns loudly when bound to `0.0.0.0` on a public interface without `--seal` or TLS).

---

## 14. Conformance

An implementation is **PARLEY/1 conformant** if it: authenticates per §3.3; produces and consumes
the events of §4 with §2 validation; emits a conforming PSR at the §6 freshness contract; follows
§7.6 for conflicts; and preserves unknown fields and `x.*` event types.

The Deck and the Ledger are **not** required for conformance — a headless participant is a valid
participant. The test-suite target is: every normative MUST in this document has at least one
test, and `tests/test_conformance.py` can be pointed at any Hub implementation.
