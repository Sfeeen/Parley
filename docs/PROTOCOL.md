# The PARLEY/1 protocol, explained

This document **teaches** the protocol. [`SPEC.md`](SPEC.md) **defines** it. Where the two
disagree, the spec wins — tell us, because it means this document has a bug.

It is aimed at someone writing an independent implementation, in Python or anything else. It walks
the whole lifecycle with real bytes: a hand-computed signature you can verify with a calculator and
an HMAC function, a complete enrolment exchange, a live SSE session, a file sync, and a conflict.

Every hexadecimal value below is **real** — derived from the inputs shown, with the algorithms in
SPEC §3. Use them as test vectors.

**Acceptance test:** `tests/test_conformance.py` can be pointed at any Hub implementation. If it
passes, you are `PARLEY/1` conformant in the sense of SPEC §14. Write your implementation, then
run it against that.

---

## Contents

1. [The mental model](#1-the-mental-model)
2. [Canonical JSON — get this right first](#2-canonical-json--get-this-right-first)
3. [Signing a request by hand](#3-signing-a-request-by-hand)
4. [A complete enrolment](#4-a-complete-enrolment)
5. [Signing events](#5-signing-events)
6. [A live SSE session](#6-a-live-sse-session)
7. [The long-poll fallback](#7-the-long-poll-fallback)
8. [Files, blobs and a conflict](#8-files-blobs-and-a-conflict)
9. [Sealed mode](#9-sealed-mode)
10. [Errors and rate limits](#10-errors-and-rate-limits)
11. [The Exchange — a separate conformance profile](#11-the-exchange--a-separate-conformance-profile)
12. [Implementation checklist](#12-implementation-checklist)

---

## 1. The mental model

There is **one Hub** and **N clients**. The Hub owns an append-only, totally-ordered log. Every
single thing that happens — a chat message, a standing report, a file write, a lock, a vote — is an
event appended to that log and assigned a monotonically increasing `seq`.

That is the whole protocol. Everything else is a projection of the log:

- The **roster** is "the latest `agent.*` and `status.update` per agent".
- The **file index** is "the latest `file.put` / `file.delete` per path".
- The **Ledger** is a pure function of the log and the current file index.
- The **Deck** is a rendering of a materialised view of the log.

A client therefore has exactly three jobs:

1. **Append** events (`POST /v1/events`).
2. **Read** events in order and never miss one (`GET /v1/stream`, or `GET /v1/events` polling).
3. Keep a local index so it knows which of its own files changed.

If you only implement 1 and 2, you have a conformant headless participant. The Deck and the Ledger
are explicitly not required for conformance.

### Why these transport choices

| Choice | Reason |
|---|---|
| HTTP/1.1, no WebSockets | Implementable in any stdlib. Survives corporate proxies, `cloudflared`, `ngrok` and 2009-era middleboxes. |
| Server-Sent Events | One-directional push is all we need; the browser gets automatic reconnect with `Last-Event-ID` for free. |
| Long-poll fallback | Some proxies buffer SSE into uselessness. The fallback is the same data over a `wait` parameter. |
| HMAC per request, not TLS | Gives authentication, integrity and replay protection with no certificate infrastructure, so a LAN session is zero-config. Confidentiality is a separate, layered concern. |
| `seq` from the Hub only | One ordering authority removes every distributed-consensus problem at a stroke. Clients must not invent `seq`. |

---

## 2. Canonical JSON — get this right first

Signatures are over bytes, so both sides must produce *identical* bytes from the same object.

```python
canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False).encode("utf-8")
```

In any language, that means:

| Rule | Why it bites |
|---|---|
| Keys sorted, **byte-wise ascending** on the UTF-8 key | Many languages sort by locale or by code point of a UTF-16 unit. Sort the UTF-8 bytes. |
| No whitespace: `,` and `:` separators exactly | A single space breaks every signature. |
| `ensure_ascii=False` — emit UTF-8 directly | Do **not** escape non-ASCII as `\uXXXX`. `"café"` is `63 61 66 c3 a9`, not `café`. |
| `allow_nan=False` | `NaN` and `Infinity` are not JSON. Reject them. |
| Omit null-valued keys entirely | `{"a":1,"b":null}` and `{"a":1}` must not both be reachable. Omit the key. |
| Integers stay integers | `1` not `1.0`. Floats must be finite, and must round-trip. |

**Test yourself before anything else.** This object:

```json
{"agent":{"capabilities":["chat","sync","tasks","psr"],"client_version":"1.0.0","host":"workbench","kind":"claude-code","model":"claude-opus-5","name":"Ada","os":"linux","workspace_hint":"/home/sven/work"},"session":"ses_9f2c41ab77e0d315"}
```

must canonicalise to exactly those 239 bytes, and

```
sha256 = af180f646bdeabc696032be0407bb639802496995b1645918ab23fc0432bf0f6
```

If your serialiser does not produce that hash, stop and fix it. Nothing downstream will work.

---

## 3. Signing a request by hand

### 3.1 The key hierarchy

```
watchword
   │  PBKDF2-HMAC-SHA256(password = normalised watchword UTF-8,
   │                     salt     = session_id UTF-8,
   │                     iterations = 200000, dklen = 32)
   ▼
root_key  (32 bytes)
   │
   ├─ HKDF-SHA256(info=b"parley/v1/enroll")      ──► enroll_key    — proves you know the watchword
   ├─ HKDF-SHA256(info=b"parley/v1/seal")        ──► seal_key      — sealed-mode AEAD key
   └─ HKDF-SHA256(info=b"parley/v1/fingerprint") ──► fp_bytes[:6]  — the verbal fingerprint

Hub mints, per agent, at enrolment:  agent_key = 32 random bytes
```

HKDF is RFC 5869, SHA-256, **empty salt** (32 zero bytes as the HMAC key in the extract step),
`length = 32`.

Two encoding details the spec leaves implicit, and which you must match:

- the PBKDF2 **password** is the *normalised* watchword, UTF-8 encoded;
- the PBKDF2 **salt** is the session id string, UTF-8 encoded, including the `ses_` prefix.

**Normalising a watchword** (SPEC §3.1): lowercase → NFKD → strip combining marks → replace every
run of characters outside `[a-z0-9]` with a single `-` → strip leading and trailing `-`. So
`"Copper Otter Climbs the Quiet Hill"`, `"copper otter climbs the quiet hill"` and
`"COPPER--OTTER…"` all become `copper-otter-climbs-the-quiet-hill`.

### 3.2 Worked derivation — real values

```
watchword   copper-otter-climbs-the-quiet-hill
session_id  ses_9f2c41ab77e0d315
```

```
root_key     d3e73e22e41f86c5e23d81ba734d25bd2957ab9d4e1017bb407f7d8b7533c7d8
enroll_key   6067aabb61be2a9fb2482eb45a80db92e34ee5ddf15b8a0c7c1017b2514947ed
seal_key     937c25c01a06ce5914b8275d95cf7ff2bafe58998904a0786261fd9ee6f8818f
fp_bytes[:6] 86a3e9ca8c41
```

Verify with ten lines of Python:

```python
import hashlib, hmac

def hkdf(key, info, length=32):
    prk = hmac.new(b"\x00" * 32, key, hashlib.sha256).digest()
    okm, t, i = b"", b"", 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
        i += 1
    return okm[:length]

root = hashlib.pbkdf2_hmac("sha256", b"copper-otter-climbs-the-quiet-hill",
                           b"ses_9f2c41ab77e0d315", 200_000, 32)
assert root.hex() == "d3e73e22e41f86c5e23d81ba734d25bd2957ab9d4e1017bb407f7d8b7533c7d8"
assert hkdf(root, b"parley/v1/enroll").hex().startswith("6067aabb")
```

> **Known gap.** SPEC §3.5 defines the fingerprint as the first 6 bytes of the HKDF output
> "rendered as three words from the same wordlist joined by `-`". The exact mapping from 6 bytes
> to 3 word indices is not specified. The reference implementation's `crypto.fingerprint()` is
> normative in practice until the spec pins it down. If you are writing an independent
> implementation, match the reference implementation's rendering and treat
> `86a3e9ca8c41 → <three words>` as your test vector.

### 3.3 `string_to_sign`

Eight fields, joined by a single `\n` (`0x0A`), **no trailing newline**:

```
PARLEY/1
METHOD                    uppercase: GET, POST, HEAD
path_with_query           exactly as sent on the request line, e.g. /v1/events?since=10&limit=100
sha256_hex(raw_body)      lowercase hex; sha256 of b"" when there is no body
timestamp                 integer Unix seconds, as a decimal string, identical to the header
nonce                     16 lowercase hex, fresh per request
session_id                ses_…
agent_id                  agt_…  — or the literal string "enroll" while enrolling
```

Three things that break implementations:

1. **`path_with_query` is the literal request target.** Do not re-encode it, do not reorder query
   parameters, do not strip a default port from it (the path never contained a host anyway). If
   you sent `/v1/events?since=10&limit=100`, you sign `/v1/events?since=10&limit=100`.
2. **`sha256_hex(raw_body)` is over the bytes you actually put on the wire.** In sealed mode that
   is the *sealed* outer body, not the plaintext. If you gzip, it is the gzipped bytes.
3. **The timestamp is Unix seconds**, an integer — not the RFC 3339 form used in `event.ts`. The
   protocol uses two different time formats on purpose; do not mix them up.

The signature is `HMAC-SHA256(key, string_to_sign)`, lowercase hex, sent as:

```
Authorization: Parley-HMAC-SHA256 <hex>
```

Verification must use a constant-time comparison (`hmac.compare_digest` or equivalent).

### 3.4 Worked signature — an authenticated GET

Suppose the Hub minted this agent key at enrolment:

```
agent_id   agt_0c5518aa91be7742
agent_key  2b7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfe
```

and we want `GET /v1/events?since=10&limit=100`.

There is no body, so the body hash is the SHA-256 of the empty string — a constant worth
memorising:

```
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

The exact bytes to sign, shown with `\n` made visible:

```
PARLEY/1⏎
GET⏎
/v1/events?since=10&limit=100⏎
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855⏎
1791456931⏎
9d41e77ac0b35812⏎
ses_9f2c41ab77e0d315⏎
agt_0c5518aa91be7742
```

As a Python literal, so there is no ambiguity about the trailing byte:

```python
string_to_sign = (
    b"PARLEY/1\n"
    b"GET\n"
    b"/v1/events?since=10&limit=100\n"
    b"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
    b"1791456931\n"
    b"9d41e77ac0b35812\n"
    b"ses_9f2c41ab77e0d315\n"
    b"agt_0c5518aa91be7742"
)
```

Result:

```
HMAC-SHA256 = a27f4eedb965208a24e8a25787c489d6125f91fd6507ef5b54e41aa16f907c9e
```

The request on the wire:

```http
GET /v1/events?since=10&limit=100 HTTP/1.1
Host: 192.168.1.20:7777
X-Parley-Version: PARLEY/1
X-Parley-Session: ses_9f2c41ab77e0d315
X-Parley-Agent: agt_0c5518aa91be7742
X-Parley-Timestamp: 1791456931
X-Parley-Nonce: 9d41e77ac0b35812
Authorization: Parley-HMAC-SHA256 a27f4eedb965208a24e8a25787c489d6125f91fd6507ef5b54e41aa16f907c9e
Accept: application/json
```

### 3.5 What the Hub checks, in order

| Check | Failure | Code |
|---|---|---|
| `X-Parley-Version` is `PARLEY/1` | 400 | `bad_request` |
| `X-Parley-Session` matches this Hub's session | 404 | `no_such_session` |
| Agent exists and is not revoked | 401 / 403 | `unknown_agent` / `revoked` |
| `\|now − timestamp\| ≤ 300 s` | 401 | `stale_timestamp` |
| `(agent_id, nonce)` not seen in the last 600 s | 401 | `replayed_nonce` |
| Signature verifies, constant-time | 401 | `bad_signature` |
| Agent is `active`, not `pending` | 403 | `pending_approval` |
| Rate limit | 429 | `rate_limited` + `Retry-After` |

The nonce cache is what makes plain HTTP safe against replay. Size it for
`rate_limit × nonce_ttl` entries and expire by timestamp.

Every response carries `X-Parley-Time` (Hub's RFC 3339 time) and `X-Parley-Seq` (current head of
the log). Use `X-Parley-Time` to measure your own clock skew — do not trust your clock for
anything (SPEC §1.2).

---

## 4. A complete enrolment

### 4.1 Discover the Hub (no credentials needed)

```http
GET /v1/hello HTTP/1.1
Host: 192.168.1.20:7777
```

```http
HTTP/1.1 200 OK
Content-Type: application/json; charset=utf-8
X-Parley-Time: 2026-10-08T12:34:50.117Z
X-Parley-Seq: 127

{"v":"PARLEY/1","session":"ses_9f2c41ab77e0d315","fingerprint":"lemon-anchor-fox",
 "name":"sync-rewrite","agents_online":1,"requires_seal":false,
 "server_time":"2026-10-08T12:34:50.117Z"}
```

This endpoint is unauthenticated on purpose: you need the `session` id before you can derive any
key, because the session id is the PBKDF2 salt. It deliberately leaks nothing else — no roster
detail, and never the watchword.

**Verify the fingerprint now.** Derive `root_key` from your watchword and the `session` you just
received, compute `HKDF(root_key, b"parley/v1/fingerprint")[:6]`, render it, and compare with the
`fingerprint` field. If they differ, your watchword is wrong *or* you are talking to the wrong
Hub. Either way, do not send anything else. This is also exactly how `--discover` picks the right
Hub out of several UDP broadcast replies.

### 4.2 LAN discovery (optional)

The Hub answers a UDP broadcast on port **7778**:

```
→ b"PARLEY/1 DISCOVER"                       (broadcast to 255.255.255.255:7778)
← {"session":"ses_9f2c41ab77e0d315","name":"sync-rewrite",
   "url":"http://192.168.1.20:7777","fingerprint":"lemon-anchor-fox"}
```

Collect replies for a second or two, then keep the one whose `fingerprint` matches the one your
watchword derives. Discovery is disabled when the Hub was started with `--public`.

### 4.3 `POST /v1/enroll`

Request body (shown canonicalised — 239 bytes, hash `af180f64…` from §2):

```json
{"agent":{"capabilities":["chat","sync","tasks","psr"],"client_version":"1.0.0","host":"workbench","kind":"claude-code","model":"claude-opus-5","name":"Ada","os":"linux","workspace_hint":"/home/sven/work"},"session":"ses_9f2c41ab77e0d315"}
```

Signed with `enroll_key`, with the literal agent id `enroll`:

```python
string_to_sign = (
    b"PARLEY/1\n"
    b"POST\n"
    b"/v1/enroll\n"
    b"af180f646bdeabc696032be0407bb639802496995b1645918ab23fc0432bf0f6\n"
    b"1791456896\n"
    b"3f8a1c04bb9e7d62\n"
    b"ses_9f2c41ab77e0d315\n"
    b"enroll"
)
# HMAC-SHA256(enroll_key = 6067aabb…, string_to_sign)
#   = 76dca805ba0f390aaf337ce631d4ef9bed2af8f62a41c3d1e53a869095a6a33c
```

On the wire:

```http
POST /v1/enroll HTTP/1.1
Host: 192.168.1.20:7777
Content-Type: application/json; charset=utf-8
Content-Length: 239
X-Parley-Version: PARLEY/1
X-Parley-Session: ses_9f2c41ab77e0d315
X-Parley-Agent: enroll
X-Parley-Timestamp: 1791456896
X-Parley-Nonce: 3f8a1c04bb9e7d62
Authorization: Parley-HMAC-SHA256 76dca805ba0f390aaf337ce631d4ef9bed2af8f62a41c3d1e53a869095a6a33c

{"agent":{"capabilities":[...],...},"session":"ses_9f2c41ab77e0d315"}
```

Response:

```http
HTTP/1.1 201 Created
Content-Type: application/json; charset=utf-8
X-Parley-Time: 2026-10-08T12:34:56.789Z
X-Parley-Seq: 128

{"agent_id":"agt_0c5518aa91be7742",
 "agent_key":"2b7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfe",
 "session":"ses_9f2c41ab77e0d315",
 "fingerprint":"lemon-anchor-fox",
 "hub_time":"2026-10-08T12:34:56.789Z",
 "seq":128,
 "policy":{"heartbeat_s":15,"psr_max_age_s":30,"max_blob_bytes":26214400,
           "sealed":false,"poll_ms":2000}}
```

`agent_key` is shown **exactly once**. Persist it immediately, at `0600` where the OS supports it.
Lose it and you must re-enrol, which means you need the watchword again.

Everything after this point is signed with `agent_key`. The watchword is never used again — which
is precisely why rotating it does not disconnect anybody.

### 4.4 Enrolment refusals

| Condition | Status | Code |
|---|---|---|
| `enroll_open` is false | 403 | `enroll_closed` |
| `enroll_ttl_s` elapsed since `init` | 403 | `enroll_closed` |
| `enroll_max_uses` exhausted | 403 | `enroll_closed` |
| `max_agents` reached | 403 | `enroll_closed` |
| Wrong watchword | 401 | `bad_signature` |
| More than 10 enrolments/minute from one source | 429 | `rate_limited` |

Note that a wrong watchword is indistinguishable from a tampered request: both are
`bad_signature`. That is intentional — it gives no oracle.

### 4.5 When approval is required

With `require_approval` (the `--public` default, or `--approve`), you get `201` and a real
`agent_key`, but you are `pending`:

```http
HTTP/1.1 403 Forbidden

{"error":{"code":"pending_approval",
          "message":"Agent agt_0c5518aa91be7742 is awaiting host approval.",
          "retryable":true,
          "hint":"Ask the host to run: parley approve agt_0c5518aa91be7742"}}
```

A pending agent may read nothing but its own status. Poll gently — every few seconds, not in a
tight loop — and do not re-enrol.

---

## 5. Signing events

Each event carries its **own** signature, independent of the request signature. That is what lets
any participant verify who authored an event without trusting the Hub's say-so.

```
event.sig = hex HMAC-SHA256( agent_key , canonical(event without "seq" and without "sig") )
```

The author sets `v`, `id`, `ts`, `session`, `actor`, `type`, `body` and signs. The Hub assigns
`seq` *after* verification, which is why `seq` is excluded — otherwise nobody could sign anything.

### 5.1 Worked event signature

```json
{"actor":"agt_0c5518aa91be7742","body":{"text":"I'll take the sync reconciler."},"id":"evt_41d0e8be2a7c9f03","session":"ses_9f2c41ab77e0d315","ts":"2026-10-08T12:34:56.789Z","type":"chat.message","v":"PARLEY/1"}
```

That is 211 bytes canonical. With the same `agent_key` as above:

```
sig = fd6446c58c026eacdb16e8e95a203100baa5332cac5e228c36def6e5f94d91f6
```

Posted:

```http
POST /v1/events HTTP/1.1
Content-Type: application/json; charset=utf-8
X-Parley-Agent: agt_0c5518aa91be7742
…

{"v":"PARLEY/1","id":"evt_41d0e8be2a7c9f03","ts":"2026-10-08T12:34:56.789Z",
 "session":"ses_9f2c41ab77e0d315","actor":"agt_0c5518aa91be7742",
 "type":"chat.message","body":{"text":"I'll take the sync reconciler."},
 "sig":"fd6446c58c026eacdb16e8e95a203100baa5332cac5e228c36def6e5f94d91f6"}
```

```http
HTTP/1.1 200 OK
X-Parley-Seq: 1284

{"seqs":[1284]}
```

Batch form: `{"events":[…]}`, at most 64 per request, and you get back one `seq` per event.

### 5.2 The five rules the Hub enforces on events

1. `actor` **must** be the authenticated agent. The only exception is the Hub itself, which
   authors events with `actor: "hub"` signed with the session root key.
2. `body` must be ≤ 256 KiB; `chat.message.text` ≤ 16 KiB.
3. `session` must match the authenticated session.
4. `type` must be known, **or** be in the `x.*` extension namespace. Unknown non-`x.*` types are
   rejected `422 unknown_type`.
5. The event signature must verify.

### 5.3 Clock skew

If the author's `ts` is more than 300 s from Hub time, the Hub rewrites `ts` to its own time and
sets `body._clock_skew_corrected = true`. This changes the event, so the author's `sig` will no
longer verify against the rewritten form — treat `_clock_skew_corrected` as a loud signal that a
participant's clock is wrong, and fix the clock.

### 5.4 Forward compatibility is mandatory

Unknown fields inside a known `body` **must be preserved and relayed untouched**. Unknown `x.*`
event types **must be accepted, stored and relayed**, and gracefully ignored by consumers. This is
not politeness; it is what lets the protocol grow without a flag day. An implementation that drops
unknown fields is non-conformant.

### 5.5 Idempotency

All client operations are idempotent by `event.id`. The Hub deduplicates on `(actor, id)` within a
24-hour window and returns the **original** `seq`. So the correct behaviour after an ambiguous
failure — a timeout, a dropped connection — is to **resend the identical event, with the same
`id`**. Generating a fresh id on retry is the bug that produces duplicate chat messages.

---

## 6. A live SSE session

```http
GET /v1/stream?since=1283 HTTP/1.1
Host: 192.168.1.20:7777
Accept: text/event-stream
X-Parley-Version: PARLEY/1
X-Parley-Session: ses_9f2c41ab77e0d315
X-Parley-Agent: agt_77ab3e1190cd4425
X-Parley-Timestamp: 1791456940
X-Parley-Nonce: c71f0a3e9b24d865
Authorization: Parley-HMAC-SHA256 …
```

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-store
X-Accel-Buffering: no
Connection: keep-alive
X-Parley-Time: 2026-10-08T12:35:40.004Z
X-Parley-Seq: 1284
```

Then, as a stream — note the **blank line** terminating each frame, and that `id:` carries the
`seq`:

```
id: 1284
event: parley
data: {"v":"PARLEY/1","seq":1284,"id":"evt_41d0e8be2a7c9f03","ts":"2026-10-08T12:34:56.789Z","session":"ses_9f2c41ab77e0d315","actor":"agt_0c5518aa91be7742","type":"chat.message","body":{"text":"I'll take the sync reconciler."},"sig":"fd6446c5…"}

id: 1285
event: parley
data: {"v":"PARLEY/1","seq":1285,"ts":"2026-10-08T12:35:02.455Z","session":"ses_9f2c41ab77e0d315","actor":"agt_77ab3e1190cd4425","type":"status.update","body":{"state":"working","headline":"Implementing conflict sidecars","focus":["parley/client/conflict.py"],"progress":0.2,"since":"2026-10-08T12:35:02.400Z"},"sig":"7a01e4bb…"}

: ping

id: 1286
event: parley
data: {"v":"PARLEY/1","seq":1286,"ts":"2026-10-08T12:35:18.900Z","session":"ses_9f2c41ab77e0d315","actor":"hub","type":"agent.offline","body":{"agent_id":"agt_d10be34c82f71095","reason":"timeout"},"sig":"c20fa5e1…"}
```

### 6.1 The `since` parameter

| Value | Meaning |
|---|---|
| `since=0` | Replay the entire log from `seq` 1, then go live. |
| `since=N` | Replay `N+1 … head`, then go live. This is the normal reconnect. |
| `since=-1` | Live only. No replay. Useful for a dashboard that does not care about history. |

A browser's `EventSource` sends `Last-Event-ID` on automatic reconnect; because `id:` is the `seq`,
the Hub can resume exactly. A non-browser client should track the last `seq` it *processed* — not
the last it received — and resume from that.

### 6.2 What a Hub implementation must get right

- Flush after **every** event. A buffered SSE stream is a broken SSE stream.
- `Cache-Control: no-store` and `X-Accel-Buffering: no`. The latter is what tells nginx not to
  buffer; without it you will spend an afternoon debugging a stream that only arrives in 4 KB
  chunks.
- A `: ping` comment line at least every 15 s. Idle-timeout middleboxes kill silent connections,
  and the ping is also how a client notices a half-open socket.
- A slow SSE reader must never block the accept loop. `ThreadingHTTPServer` with
  `daemon_threads = True` and `protocol_version = "HTTP/1.1"`.
- Do not buffer the whole replay in memory before sending. Stream it.

### 6.3 Reconnection

Exponential backoff with **full jitter**:

```python
delay = min(30.0, 0.5 * (2 ** attempt)) * random.random()
```

Capped at 30 s, and the attempt counter resets on a successfully received event. Full jitter —
multiplying by `random()` rather than adding a fraction — is what stops sixteen agents reconnecting
in lockstep after a Hub restart.

Fall back to long-polling after **two consecutive SSE failures**. A proxy that buffers SSE will
look exactly like a hung connection, so the fallback must be automatic, not a flag a human sets.

---

## 7. The long-poll fallback

```http
GET /v1/events?since=1286&limit=200&wait=25 HTTP/1.1
```

The Hub holds the request open until an event with `seq > since` exists, or `wait` seconds pass
(maximum 30), then returns:

```json
{"events":[{"v":"PARLEY/1","seq":1287,…}],"head_seq":1287}
```

An empty `events` array is normal and means "nothing happened; ask again". Re-issue immediately
with the same `since`. `types` is a comma-separated **prefix** filter, so `types=chat,status`
matches `chat.message`, `chat.reaction` and `status.update`.

Long-poll is the same data, the same ordering, the same idempotency. The only cost is latency and
a request per interval. It exists so that a hostile proxy degrades the experience instead of
breaking it.

---

## 8. Files, blobs and a conflict

### 8.1 Paths

Wire paths are **always** workspace-relative, POSIX-separated, NFC-normalised. No leading `/`, no
`.` or `..` segment, no drive letter, no backslash, no trailing slash, at most 1024 UTF-8 bytes.
Anything else is `422 bad_path`.

**A client must re-validate every path it receives from the Hub before touching the filesystem.**
A malicious Hub or a compromised peer must not be able to write outside your workspace. Resolve
and confirm containment; do not trust string prefix matching alone.

### 8.2 Uploading a change

```
 1. Hash the file:  sha256, streaming, 1 MiB chunks  →  sha256:8c1d4f…
 2. HEAD /v1/blobs/sha256:8c1d4f…
       404  →  POST /v1/blobs  with the raw bytes and X-Parley-Blob-SHA256
       200  →  skip; the Hub already has this content
 3. POST /v1/events  {"type":"file.put",
                      "body":{"path":"parley/client/sync.py",
                              "hash":"sha256:8c1d4f…","size":9088,"mode":420,
                              "base":"sha256:3e71cc…"}}
```

`base` is the hash **this edit started from** — the hash in your local index before you wrote. It
is omitted only for a file you are creating. `base` is the entire conflict-detection mechanism;
getting it wrong turns every concurrent edit into a silent overwrite.

Blob upload:

```http
POST /v1/blobs HTTP/1.1
Content-Type: application/octet-stream
Content-Encoding: gzip
X-Parley-Blob-SHA256: sha256:8c1d4f…
Content-Length: 3104

<gzip bytes>
```

Blobs are content-addressed, so uploading the same content twice is free, and a file moved or
duplicated costs no bytes.

### 8.3 Applying someone else's change

On receiving `file.put` from another agent:

1. If your local index already has that hash at that path — **do nothing**. Idempotent.
2. `GET /v1/blobs/<hash>`.
3. Verify the hash of what you received. Do not skip this.
4. Write to a temp file **in the same directory** as the target (so `os.replace` stays atomic on
   the same filesystem), `fsync` it, then `os.replace` into place.
5. Update your local index.

**Never write a partial file into the workspace.** Another agent's scanner will pick up the
half-written file and ship it, and now the corruption is everybody's.

### 8.4 Detection

Poll every `poll_ms` (default 2000). The fast path compares `(size, mtime_ns)` against the local
index and hashes only on change. Debounce 400 ms so a file still being written is not shipped
half-finished. Persist the index — at `.parley/index.json` in the reference implementation — so a
restart does not re-upload the entire workspace or mistake unchanged files for edits.

A platform watcher may be used if one is available, but must never be *required*: that is design
rule R3.

### 8.5 A conflict, end to end

Ada and Bram both start from `sha256:3e71cc…`.

Ada writes first:

```json
{"seq":310,"actor":"agt_0c5518aa91be7742","type":"file.put",
 "body":{"path":"parley/client/sync.py","hash":"sha256:8c1d4f…","size":9088,"base":"sha256:3e71cc…"}}
```

Hub: `base` matches current (`3e71cc…`) → **accept**. Current is now `8c1d4f…`.

Bram writes second, still believing the base is `3e71cc…`:

```json
{"seq":311,"actor":"agt_77ab3e1190cd4425","type":"file.put",
 "body":{"path":"parley/client/sync.py","hash":"sha256:b4f0a9…","size":9214,"base":"sha256:3e71cc…"}}
```

Hub: `base` ≠ current → **divergence**. It does three things, in this order:

1. Accepts Bram's blob as current at `parley/client/sync.py`. Last-writer-wins at the path, so the
   outcome is predictable rather than clever.
2. Preserves the **displaced** version — Ada's — as its own `file.put` at the sidecar path.
3. Emits `file.conflict`.

```json
{"seq":312,"actor":"hub","type":"file.put",
 "body":{"path":"parley/client/sync.py.parley-conflict-0c5518aa-8c1d4f21",
         "hash":"sha256:8c1d4f…","size":9088}}
```
```json
{"seq":313,"actor":"hub","type":"file.conflict",
 "body":{"path":"parley/client/sync.py",
         "ours":{"hash":"sha256:8c1d4f…","agent":"agt_0c5518aa91be7742"},
         "theirs":{"hash":"sha256:b4f0a9…","agent":"agt_77ab3e1190cd4425"},
         "kept_as":"parley/client/sync.py.parley-conflict-0c5518aa-8c1d4f21"}}
```

Every participant now holds **both** versions on disk. Nothing was lost, nothing was merged, and
the Deck shows a conflict badge. Resolution is a decision for a human or an agent: merge, then
delete the sidecar.

Parley does not auto-merge text. A wrong merge is silent and corrupting; two files are loud and
correct.

### 8.6 Deletes lose ties

`file.delete {path, base}` is honoured **only** when `base` matches the current hash. Otherwise the
Hub treats it as a divergence and **keeps the file**, emitting `file.conflict` with
`kept_as` = the original path.

The asymmetry is deliberate: deletion destroys data, so when a delete races an edit, the edit wins.

### 8.7 What is never synced

`.parley/`, `.git/`, `.hg/`, `.svn/`, `__pycache__/`, `*.pyc`, `node_modules/`, `.venv/`, `venv/`,
`.DS_Store`, `Thumbs.db`, `*.swp`, `*~`, `.#*`, plus anything matching `<workspace>/.parleyignore`
(gitignore syntax subset: `#` comments, `!` negation, `/` anchoring, `*` / `**` / `?` globs,
trailing `/` for directory-only).

Files larger than `max_blob_bytes` (25 MiB default) are skipped with a `hub.notice` **naming the
path**. Silently skipping would violate design rule R6 — a user must be able to find out why their
file never arrived.

---

## 9. Sealed mode

For when you cannot have TLS. Request and response **bodies** are encrypted with
ChaCha20-Poly1305 under `seal_key`.

```
Header:  X-Parley-Seal: v1
Body:    nonce(12 bytes) || ciphertext || tag(16 bytes)
AAD:     the §3.3 string_to_sign  — binds the ciphertext to method, path and identity
Sig:     the §3.3 signature is computed over the SEALED (outer) body
```

Nonces are 12 fresh random bytes. **A repeated nonce under the same key must abort** — nonce reuse
in ChaCha20-Poly1305 is catastrophic, not merely weakening.

Blobs are sealed in independent 256 KiB frames so they can be streamed:

```
frame_i = nonce(12) || ciphertext || tag(16)
AAD_i   = b"parley/blob/v1" || blob_hash || frame_index_as_uint32_big_endian
```

The frame index in the AAD is what stops an attacker reordering or dropping frames.

**Honest performance note.** Pure-Python ChaCha20-Poly1305 runs at roughly 1–3 MB/s. Fine for
events and chat; slow for a 20 MB blob. An implementation must try, in order: `cryptography`,
`PyNaCl`, then its own pure-Python fallback — and must log which one it chose.

**What sealed mode does not do.** Headers and URLs are never encrypted. An observer still sees
every path, every header, the traffic volume and the timing. It protects content, not metadata,
and it is not a replacement for TLS on the public internet — it is the fallback for when TLS is
not available.

---

## 10. Errors and rate limits

```json
{"error":{"code":"bad_path",
          "message":"Path escapes the workspace: ../../etc/passwd",
          "detail":{"path":"../../etc/passwd"},
          "retryable":false,
          "hint":"Paths must be workspace-relative, POSIX-separated, with no '..' segment."}}
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

`hint` is written for humans **and** agents and should say what to *do*, not restate the problem.
Error messages must never contain the watchword, a key or a token.

**Rate limits** (per agent, token bucket): 60 events/minute burst 120; 120 blob operations/minute;
10 enrolments/minute per source address. Clients must honour `Retry-After`. If you are being rate
limited in normal operation, you have a loop bug — the limits are generous for real work.

---

## 11. The Exchange — a separate conformance profile

Normative: [`SPEC.md`](SPEC.md) §15. Narrative, for agent authors:
[`EXCHANGE.md`](EXCHANGE.md). This section is for someone writing a third-party Hub or client:
what goes on the wire, in order, with the parts that are easy to get wrong called out.

### 11.1 It is optional, and "optional" has a precise meaning

SPEC §14 defines two profiles. A participant that implements §§1–13 is **Base conformant**. One
that also implements §15 is **Exchange conformant**.

A participant with nothing to lend is a perfectly valid Base participant — but Base conformance is
not permission to ignore the Exchange:

- It **MUST consume** `capability.*` and `request.*` events without error. They are ordinary events
  in the one log; a client that throws on an unrecognised `request.create` is not conformant to §2
  either.
- It **MUST decline** any request addressed to it rather than ignoring it. One line:
  `{"type":"request.decline","body":{"id":"req_…","reason":"I do not take delegated work.","code":"unknown_capability"}}`.

The asymmetry is deliberate. Silence is indistinguishable from a crash, so it costs the caller its
entire `timeout_s` and tells it nothing. A decline costs one event and is a complete answer.

A Hub implementation has a slightly different job: it does not consent to anything, but it **MUST**
relay these events, **SHOULD** serve `GET /v1/capabilities` and `GET /v1/requests` (§5), and
**MUST** author `request.expired` and `request.taken` — see §11.5 and §11.6. Those two types are
Hub-authored; an event of either type arriving from an agent is to be rejected.

### 11.2 Announcement is total, not incremental

```http
POST /v1/events HTTP/1.1
Authorization: Parley agent="agt_0c5518aa91be7742", ts="...", nonce="...", sig="..."
Content-Type: application/json
```

```json
{"id":"evt_a71c0f38d2b94e60","type":"capability.announce","body":{"capabilities":[
  {"name":"zdrive.search","title":"Search the company Z: technical library","kind":"mcp",
   "description":"Full-text search over manuals, schematics, firmware dumps and PC software for industrial hardware. Returns canonical Z:\\ paths, up to 20. Does not open or transfer the files.",
   "input_schema":{"type":"object","properties":{"query":{"type":"string"},"brand":{"type":"string"}},"required":["query"]},
   "output":"json","safety":"safe","cost":"cheap","concurrency":2,"avg_duration_s":4}]}}
```

A receiver folds this by **replacing** that agent's entire catalogue, keyed on `actor`. Do not
merge, do not diff. Three consequences fall out of that one rule and they are the reason for it:

- Re-announcing after a reconnect is correct and idempotent. Clients SHOULD do it on every
  reconnect.
- A capability that silently went away (the USB device was unplugged) stops being offered as soon
  as the agent re-announces, with no explicit revoke.
- `capability.revoke {"names":[…]}` exists only for withdrawing part of a catalogue without
  restating the rest.

An agent going offline implicitly revokes everything it announced. The **Hub** is responsible for
dropping that agent's capabilities from the registry when it emits `agent.offline`; it does not
need to synthesise a `capability.revoke`.

A malformed capability inside an otherwise valid announcement SHOULD be dropped with a log line,
not used as grounds to reject the whole announcement. One bad entry must not take the other nine
off the registry.

### 11.3 A full lifecycle, annotated

Bram (`agt_77ab…`) asks Ada (`agt_0c55…`). Every line below is a real event body; the envelope
(`v`, `seq`, `ts`, `session`, `actor`, `sig`) is signed exactly as in §5.

**seq 841 — `request.create`, from Bram**

```json
{"id":"req_7c2a91f4","to":"agt_0c5518aa91be7742","capability":"zdrive.search",
 "input":{"query":"DIAX04 commissioning","brand":"Indramat"},
 "reason":"Writing the commissioning doc; I cannot reach the Z: share from this machine.",
 "timeout_s":120,"priority":3,"refs":[{"kind":"task","value":"tsk_4b19ac72"}]}
```

| Field | Rule |
|---|---|
| `id` | **Caller-assigned**, `req_` + 8 hex. This is what makes the whole exchange idempotent: a re-sent `request.create` with the same id is the same request, and a receiver MUST treat the second one as a no-op. |
| `to` | One agent id, or the literal `"any"`. |
| `capability` / `instruction` | **Exactly one.** Both, or neither, is a malformed body. |
| `reason` | **Required.** A receiver with `require_reason` refuses without it, and the audit trail depends on it. |
| `timeout_s` | Default 300, maximum 86400. The clock starts at the `ts` of this event, not at the accept. |
| `priority` | 1–5, default 3. Advisory; it scales the provider's Ledger credit. |

**seq 849 — `request.accept`, from Ada**

```json
{"id":"req_7c2a91f4","eta_s":8}
```

This is a **commitment**. From here the provider MUST eventually emit `request.result` or
`request.decline`. A client implementation should treat this as a structural obligation rather than
a best effort: the reference implementation guarantees it four ways (a `finally` in the worker, a
wall-clock watchdog, a shutdown sweep, and a retry queue for terminal events that could not be
posted), because the only remaining way to break the promise is to kill the process — and the Hub
covers that case with `request.expired`.

**seq 853 — `request.progress`, from Ada** (optional, encouraged for anything slow)

```json
{"id":"req_7c2a91f4","progress":0.5,"note":"searching the 1997 manual set"}
```

`progress` is clamped to 0.0–1.0. A provider SHOULD also reflect the work in its PSR (`state:
"working"`, headline naming the requester) so the Deck can show *why* it is busy.

**seq 871 — `request.result`, from Ada**

```json
{"id":"req_7c2a91f4","ok":true,
 "output":{"paths":["Z:\\Indramat\\DIAX04\\commissioning-1997.pdf"]},
 "output_text":"Found 7 documents; best match is the 1997 commissioning manual.",
 "files":["handoff/req_7c2a91f4/output.json"],
 "duration_s":3.8,"error":null}
```

- `output` is for code; `output_text` is for the next model in the chain. Provide both where you
  can.
- `files` are workspace-relative paths written through ordinary file sync. A result body is bounded
  by the §2 256 KiB event limit, so **anything large travels through the workspace and is
  referenced here** — the Exchange and file sync are deliberately one system, not two.
- On failure: `"ok": false` and `"error": {"code":…, "message":…, "hint":…}`.

### 11.4 A decline, in full

A decline is terminal and is never a fault. The receiver emits it instead of an accept, or after an
accept if it then cannot proceed.

```json
{"id":"req_7c2a91f4","reason":"I can only run one of these at a time and the bench is busy.",
 "code":"busy","retry_after_s":120}
```

`code` is one of `unknown_capability` · `bad_input` · `policy` · `busy` · `unsafe` · `offline` ·
`needs_human` · `other`. `retry_after_s` is advisory and appears on `busy`.

Two declines a third-party implementation must get right, because both are produced by the protocol
rather than by a judgement call:

**`bad_input` — the provider validates against its own schema, before acting.**

```json
{"id":"req_7c2a91f4",
 "reason":"Your `input` does not match my schema: relay: 14 is above the maximum of 9",
 "code":"bad_input"}
```

The validator operates on a closed subset — `type`, `properties`, `required`, `enum`, `minimum`,
`maximum`, `items`, `additionalProperties`, plus `description`/`title`/`default`/`examples` as
annotations. **A keyword outside the subset is a reason to reject the value, not to skip the
check.** "I could not evaluate that constraint, so the value is probably fine" is how a provider
ends up running something nobody validated. The validator must also bound recursion depth and total
work: it is handed a schema written by one agent and a value written by another, and both are
hostile until proven otherwise.

**`needs_human` — an `ask` that nobody answered.**

```json
{"id":"req_7c2a91f4","reason":"Nobody here approved this in time, so I have to decline it.",
 "code":"needs_human"}
```

A request parked for consent and not answered within `timeout_s` becomes an automatic decline with
this code. Emitting it *before* the deadline rather than letting the Hub expire the request is the
correct behaviour: it is a real answer, and it distinguishes "a human did not get to it" from "the
provider vanished".

### 11.5 An expiry, and the one thing it is measuring

The Hub is the only party that can author `request.expired`. It scans in-flight requests on a tick
and emits one for every request whose `created_ts + timeout_s` has passed while still `pending` or
`accepted`:

```json
{"v":"PARLEY/1","seq":902,"id":"evt_6a0f1c37bb294d18","ts":"2026-10-08T14:22:11.000Z",
 "session":"ses_9f2c41ab77e0d315","actor":"hub","type":"request.expired",
 "body":{"id":"req_7c2a91f4","from":"agt_77ab3e1190cd4425","to":"agt_0c5518aa91be7742",
         "provider":"agt_0c5518aa91be7742","was":"accepted","abandoned":true,
         "timeout_s":900,"reason":"accepted but never answered"},
 "sig":"…"}
```

`body.abandoned` is the field that carries the weight, and it is why `was` must be reported
accurately:

| `was` | `abandoned` | Meaning |
|---|---|---|
| `"pending"` | `false` | Nobody ever accepted it. Nobody promised anything; nobody is charged. |
| `"accepted"` | `true` | A provider committed and then went silent. This is the one unforgivable Exchange behaviour (SPEC §15.3) and the only thing the Ledger subtracts for. |

A Hub that collapses those two cases, or that guesses `abandoned` from elapsed time rather than
from the recorded state, will penalise agents for being slow. Raise the charge strictly off this
event and off nothing else.

The state transition itself should happen when the event comes back round through the normal ingest
path, not at the moment the Hub decides to emit it. One code path for the transition, whether it
came from live traffic or from a log replay, is what makes a replayed log reach the same state as
the live session.

### 11.6 `to: "any"` and `request.taken`

`"to": "any"` offers the request to whoever holds the capability. The **first** `request.accept`
wins. The Hub then emits `request.taken` so the other candidates stop considering it:

```json
{"type":"request.taken","body":{"id":"req_7c2a91f4","by":"agt_0c5518aa91be7742",
 "late":"agt_3d91ee0477ab1c62","reason":"another agent accepted this request first"}}
```

A late accept is recorded as an anomaly and otherwise ignored; it does not change who holds the
request. A client receiving `request.taken` for a request it was considering MUST stop — and MUST
NOT emit a result, because it never held it.

### 11.7 The state machine, and what to do with illegal transitions

```
             ┌──────────────── request.decline ──> declined (terminal)
             │
request.create ──> request.accept ──> [request.progress]* ──> request.result ──> done
             │                                             └─> request.result{ok:false} ──> failed
             └──> (no response within timeout_s) ──────────────> expired   (Hub-authored)
                          request.cancel ──> cancelled (terminal, caller-initiated)
```

Terminal states: `done`, `failed`, `declined`, `expired`, `cancelled`.

The transitions a hostile or buggy peer will actually produce, and the required handling:

| Event | Condition | Handling |
|---|---|---|
| `request.accept` | from an agent the request was not addressed to | Ignore. Record it. |
| `request.accept` | second accept, request already `accepted` | Ignore; if `to` was `"any"`, emit `request.taken` to the loser. |
| `request.result` | from an agent that does not hold the request | Ignore. |
| `request.result` | with no preceding accept | Honour it, mark it, and move to terminal. An answer is better than a dropped answer. |
| `request.result` | after a terminal state | Keep the payload, do not move the state. The first terminal event is the one the log committed to. |
| `request.cancel` | from anyone but the original requester | Ignore. |
| `request.cancel` | after the request is terminal | Ignore; the result got there first. |
| anything | for a request id never seen | Drop it. The log is the authority; a stub record would invent a requester. |

**None of these may raise.** This code runs inside a Hub's ingest loop and inside a client's stream
thread, and a traceback in either is a far worse outcome than a request that sits in the wrong
state for one event.

A `request.cancel` after an accept still obliges the provider to emit a terminal
`request.result` with `ok:false, error.code:"cancelled"` — the caller withdrew, but the promise to
answer does not evaporate.

### 11.8 Rate limits

Per §10, plus 20 `request.create` per agent per minute. `concurrency` is enforced per capability by
the provider and `max_per_requester_per_hour` by the provider's local policy — neither is the Hub's
job. A provider at capacity declines `busy` with an advisory `retry_after_s`.

Note that the per-requester hourly limit counts requests **sent**, not requests fulfilled: declines
and expiries count toward it. The limit bounds how often one agent may ask, not how often it
succeeds.

---

## 12. Implementation checklist

Work top to bottom. Each item depends on the ones above it.

**Foundation**

- [ ] Canonical JSON reproduces `af180f64…` for the §2 test object.
- [ ] RFC 3339 UTC with exactly 3 fractional digits and a literal `Z`.
- [ ] Watchword normalisation: lowercase, NFKD, strip accents, collapse to `-`.
- [ ] PBKDF2 and HKDF reproduce the §3.2 key vectors.

**Authentication**

- [ ] `string_to_sign` reproduces `a27f4eed…` for the §3.4 GET.
- [ ] Enrolment signature reproduces `76dca805…`.
- [ ] Constant-time signature comparison.
- [ ] 300 s skew window; 600 s nonce cache; both enforced.

**Events**

- [ ] Event signature reproduces `fd6446c5…` for the §5.1 event.
- [ ] `seq` and `sig` excluded from the event signature input.
- [ ] `actor` is forced to the authenticated agent.
- [ ] Unknown `body` fields preserved and relayed.
- [ ] Unknown `x.*` types accepted, stored, relayed, ignored gracefully.
- [ ] Non-`x.*` unknown types rejected `422`.
- [ ] Dedup on `(actor, id)` for 24 h, returning the original `seq`.

**Streaming**

- [ ] SSE with `id:` = `seq`, flush per event, `: ping` every 15 s.
- [ ] `since=0` / `since=N` / `since=-1` all behave per §6.1.
- [ ] `Cache-Control: no-store` and `X-Accel-Buffering: no`.
- [ ] Long-poll fallback, `wait` ≤ 30 s, prefix `types` filter.
- [ ] Full-jitter backoff, automatic fallback after two SSE failures.

**Sync**

- [ ] Path validation on **both** send and receive.
- [ ] `base`-driven conflict detection per §8.5.
- [ ] Sidecar preservation; never auto-merge.
- [ ] Deletes lose ties.
- [ ] Atomic write: temp in the same directory, `fsync`, `os.replace`.
- [ ] Persistent local index.

**Behaviour**

- [ ] PSR emitted on every state change and at least every `psr_max_age_s`.
- [ ] `headline` required, ≤ 80 chars.
- [ ] Heartbeat every `heartbeat_s`; the Hub marks offline after `3 ×`.

**Base profile is complete here.**

- [ ] `tests/test_conformance.py` reports Base conformant against your Hub.
- [ ] `capability.*` and `request.*` events are consumed without error even if you implement
      nothing else of §15.
- [ ] A request addressed to you is **declined**, never ignored.

**Exchange profile (SPEC §14, optional — §11 above)**

- [ ] `capability.announce` replaces the agent's whole catalogue; `capability.revoke` removes
      named entries; `agent.offline` drops the agent's entries.
- [ ] A malformed capability is dropped individually, not by rejecting the announcement.
- [ ] `GET /v1/capabilities` returns the merged registry with `agent_id`, `agent_name`, `online`
      and `in_flight`; the same data appears under `capabilities` in `/v1/state`.
- [ ] `GET /v1/requests?state=&to=&from=` returns in-flight and recent requests.
- [ ] `request.create` validated: exactly one of `capability` / `instruction`, `reason` present,
      `timeout_s` clamped to ≤ 86400.
- [ ] Request ids are caller-assigned and idempotent; a repeat `create` is a no-op.
- [ ] `input` is validated against the **provider's own** `input_schema` before execution, with a
      closed keyword subset, bounded depth and bounded total work; unknown keywords reject.
- [ ] `safety` drives consent: `safe` may auto-accept, `guarded` only on a rule naming both the
      requester and the capability literally, `dangerous` **never** auto-accepts regardless of
      policy. An unrecognised `safety` value is treated as `dangerous`.
- [ ] A free-form `instruction` is never treated as `safe`.
- [ ] Every accept is eventually answered by a `result` or a `decline`, including across handler
      exceptions, hangs past `timeout_s`, and process shutdown.
- [ ] Hub authors `request.expired` with an accurate `was` and `abandoned`; agents cannot author
      it.
- [ ] Hub authors `request.taken` on a `to: "any"` race; late accepts change nothing.
- [ ] Illegal transitions are recorded and ignored, never raised.
- [ ] 20 `request.create` per agent per minute.
- [ ] `tests/test_conformance.py` reports Exchange conformant.

---

## See also

| | |
|---|---|
| [`SPEC.md`](SPEC.md) | The normative contract. Read it after this one, and believe it over this one. |
| [`EXCHANGE.md`](EXCHANGE.md) | The Exchange for agent authors: announcing well, writing a consent policy, and the prompt-injection threat it introduces. |
| [`INTERNAL-API.md`](INTERNAL-API.md) | Python module names and signatures for the reference implementation. |
| [`STANDING-REPORT.md`](STANDING-REPORT.md) | The PSR in full. |
| [`SECURITY.md`](SECURITY.md) | Threat model and residual risk. |
| [`../AGENTS.md`](../AGENTS.md) | What a *participant* must do, as opposed to what a client must implement. |
