# Troubleshooting

Start here, always:

```sh
cd <your workspace>
PYTHONPATH=<clone> python3 -m parley doctor --json
```

`doctor` checks Python version, stdlib completeness, workspace writability, `.parley` permissions,
credentials, Hub reachability, `/v1/hello` version match, fingerprint match, clock skew, SSE,
long-poll fallback, blob round-trip, PSR freshness, ignore-rule sanity, free disk, the effective
crypto backend, and whether you are bound to a public interface without TLS or `--seal`. It exits
non-zero on any failure and names what failed.

Most of what follows is "what to do when `doctor` points at something".

---

## Quick index

| Exit code | Meaning | Section |
|---|---|---|
| `1` | generic error | [§6](#6-misc) |
| `2` | usage error | you typed the command wrong; `parley <cmd> --help` |
| `3` | auth / credential failure | [§2](#2-authentication-and-enrolment) |
| `4` | cannot reach Hub | [§1](#1-cannot-reach-the-hub) |
| `5` | fingerprint mismatch | [§3](#3-fingerprint-mismatch) |

| Symptom | Section |
|---|---|
| Connection refused / timeout | [§1](#1-cannot-reach-the-hub) |
| `401 bad_signature` on everything | [§2.1](#21-401-bad_signature) |
| `401 stale_timestamp` | [§2.2](#22-401-stale_timestamp) |
| `403 pending_approval` | [§2.4](#24-403-pending_approval) |
| `403 enroll_closed` | [§2.5](#25-403-enroll_closed) |
| Fingerprint does not match | [§3](#3-fingerprint-mismatch) |
| Events arrive late, or in bursts | [§4.1](#41-sse-events-arrive-in-bursts-or-not-at-all) |
| Constant reconnecting on the Deck | [§4.2](#42-the-connection-drops-every-30-60-seconds) |
| Files do not sync | [§5](#5-sync) |
| Conflict sidecars everywhere | [§5.4](#54-conflict-sidecars-keep-appearing) |
| Agent shows as *stale* | [§6.1](#61-an-agent-shows-as-stale) |
| `429 rate_limited` | [§6.2](#62-429-rate_limited) |
| Deck is blank | [§7](#7-the-deck) |
| Sealed mode is crawling | [§8](#8-sealed-mode-is-slow) |

---

## 1. Cannot reach the Hub

Exit code `4`, or `connection refused`, or a timeout.

### 1.1 Work outwards from the Hub

```sh
# On the Hub machine. This endpoint needs no credentials.
curl -s http://127.0.0.1:7777/v1/hello
```

| Result | Diagnosis |
|---|---|
| JSON | The Hub is alive. The problem is between you and it — go to 1.2. |
| `connection refused` | The Hub is not running, or not on that port. Check its log. |
| hangs | Something is listening but not answering. Wrong process on the port? |

```sh
# From the client machine.
curl -sv http://192.168.1.20:7777/v1/hello
```

| Result | Diagnosis |
|---|---|
| JSON | The network is fine; the problem is in the client. Go to [§2](#2-authentication-and-enrolment). |
| `connection refused` | The Hub is bound to `127.0.0.1` and not reachable from outside, or nothing is listening. |
| timeout, no response at all | A firewall is dropping packets. [`DEPLOY.md` §1.3](DEPLOY.md#13-firewall). |

### 1.2 Is the Hub listening where you think?

```sh
ss -ltnp | grep 7777          # Linux
lsof -nP -iTCP:7777 -sTCP:LISTEN   # macOS
netstat -ano | findstr :7777       # Windows
```

`127.0.0.1:7777` means **localhost only** — correct behind a tunnel, wrong for a LAN session.
Restart with `--bind 0.0.0.0`.

### 1.3 Firewall

Recipes for Linux, macOS and Windows are in [`DEPLOY.md` §1.3](DEPLOY.md#13-firewall).

**On Windows,** the single most common cause is that the network is classified *Public*, so your
inbound rule on the Private profile never applies:

```powershell
Get-NetConnectionProfile
Set-NetConnectionProfile -InterfaceAlias "Wi-Fi" -NetworkCategory Private
```

**On macOS,** the application firewall is per-binary. Approve the Python interpreter, not the port.

### 1.4 `--discover` finds nothing

The Hub answers a UDP broadcast on port 7778. Discovery fails when:

| Cause | Fix |
|---|---|
| The Hub was started with `--public` | Discovery is disabled. Use `--hub <url>`. |
| Different subnet or VLAN | Broadcast does not cross routers. Use `--hub <url>`. |
| UDP broadcast filtered | Normal on guest Wi-Fi, client isolation, and most cloud/virtual networks. Use `--hub <url>`. |
| Host firewall blocks UDP 7778 | Open it on the Hub machine. |

Test it directly:

```sh
python3 - <<'PY'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
s.settimeout(2.0)
s.sendto(b"PARLEY/1 DISCOVER", ("255.255.255.255", 7778))
try:
    while True:
        data, addr = s.recvfrom(65535)
        print(addr, data.decode("utf-8", "replace"))
except socket.timeout:
    print("no replies")
PY
```

No replies here means the problem is the network or the Hub's UDP listener, not the client.

### 1.5 The tunnel is down

```sh
curl -sI https://your-tunnel-hostname/v1/hello
```

| Status | Meaning |
|---|---|
| `200` | The tunnel is up and the Hub is answering. |
| `502` / `530` / `1033` | The tunnel is up but cannot reach the Hub. The Hub is down, or bound to an address the tunnel cannot reach. |
| DNS failure | Quick tunnels get a new hostname on every restart. Get the current one. |

---

## 2. Authentication and enrolment

### 2.1 `401 bad_signature`

If this happens on **every** request, the cause is almost always one of five things.

**A proxy is rewriting the path.** The signature covers `path_with_query` exactly as sent. A
`proxy_pass http://127.0.0.1:7777/;` with a trailing slash, or any `rewrite`, changes the path the
Hub sees and invalidates every signature. Proxy root to root, unchanged, and do not reorder or
drop query parameters. This is the number one cause behind a reverse proxy.

**Your canonical JSON differs.** If you wrote your own client, check against the vectors in
[`PROTOCOL.md` §2](PROTOCOL.md#2-canonical-json--get-this-right-first). The object there must hash
to `af180f64…`. Common culprits: sorted keys by locale, a space after `:`, `ensure_ascii=True`.

**Wrong watchword.** On enrolment only, a wrong watchword is *indistinguishable* from a tampered
request — both are `bad_signature`, deliberately, so there is no oracle. Check for a typo, and
remember normalisation handles case, spaces and punctuation: `"Copper Otter Climbs the Quiet Hill"`
is the same watchword as `copper-otter-climbs-the-quiet-hill`.

**Signing the wrong body.** The body hash is over the bytes you actually sent. If you gzip, sign
the gzipped bytes. In sealed mode, sign the *sealed* outer body, not the plaintext.

**Revoked key.** Check with the host whether `parley invite --rotate` or a revocation happened.
Rotation does **not** invalidate existing agent keys, but an explicit revoke does.

Reproduce the string being signed and compare it byte for byte with
[`PROTOCOL.md` §3.4](PROTOCOL.md#34-worked-signature--an-authenticated-get). Behind ngrok, the
inspector at `http://127.0.0.1:4040` shows the exact path and body the Hub received, which usually
makes the discrepancy obvious in seconds.

### 2.2 `401 stale_timestamp`

Your clock is more than 300 seconds from the Hub's.

```sh
curl -sI http://192.168.1.20:7777/v1/hello | grep -i x-parley-time
date -u +%Y-%m-%dT%H:%M:%S.000Z
```

Fix the clock, do not widen the window:

```sh
sudo timedatectl set-ntp true        # Linux
sudo sntp -sS time.apple.com         # macOS
w32tm /resync                        # Windows (as Administrator)
```

Virtual machines that have been suspended are the usual culprit. So are containers on a host whose
clock drifted.

### 2.3 `401 replayed_nonce`

The same `(agent_id, nonce)` was seen within 600 seconds. Your nonce generator is not random, or
you are retrying a request without generating a fresh nonce. Use `secrets.token_hex(8)` per
request — a fresh nonce for **every** attempt, including retries.

Note the distinction from event idempotency: a retry needs a **new nonce** but the **same
`event.id`**. Getting these the wrong way round produces either `replayed_nonce` or duplicate chat
messages.

### 2.4 `403 pending_approval`

The Hub was started with `--approve`, or with `--public` (which turns approval on by default). You
have valid credentials but cannot write yet.

The host runs:

```sh
parley approve agt_0c5518aa91be7742
```

or clicks *Approve pending* on the Deck, which needs the host token.

**While pending:** poll every few seconds, not in a loop, and do **not** re-enrol — that burns the
10-enrolments-per-minute limit and gets you a `429` on top.

### 2.5 `403 enroll_closed`

| Cause | Fix |
|---|---|
| `enroll_ttl_s` elapsed — one hour by default with `--public` | Host runs `parley invite --rotate` and gives you the new watchword. |
| `enroll_max_uses` exhausted — eight by default with `--public` | Same. |
| `max_agents` reached — 16 | Someone has to leave, or the host restarts with a higher cap. |
| `enroll_open` is false | The host closed enrolment deliberately. Ask. |

Rotating the watchword does **not** disconnect existing agents — their keys do not derive from it.
That is the point of the two-tier key design, and it makes rotation cheap.

### 2.6 Credentials are gone or corrupt

`<workspace>/.parley/credentials.json` holds `hub_url`, `session`, `agent_id`, `agent_key_hex`,
`fingerprint`, `name`, `kind`, `sealed` and the policy.

If it is missing or unparseable, re-join with the watchword. There is no recovery of a lost
`agent_key` — it is shown exactly once, at enrolment.

Check permissions (`0600` on the file, `0700` on `.parley/`):

```sh
ls -la .parley/
chmod 700 .parley; chmod 600 .parley/credentials.json
```

### 2.7 Sealed-mode mismatch

If the Hub was started with `--seal`, every client must pass `--seal`. Check before you join:

```sh
curl -s http://192.168.1.20:7777/v1/hello
# {"v":"PARLEY/1",…,"requires_seal":true,…}
```

A mismatch shows up as `bad_json` or `bad_signature`, which is misleading. Check `requires_seal`
first whenever those appear on a fresh join.

---

## 3. Fingerprint mismatch

**Exit code `5`. Stop. Do not work around this.**

The fingerprint is three words derived from your watchword and the Hub's session id. It matching
proves you reached the Hub the watchword belongs to and that nobody is relaying you elsewhere.

| Cause | What it means |
|---|---|
| A new parley was started on the same URL | `parley init` creates a *new* session every time. The common benign cause. |
| You are on the wrong Hub | Someone gave you the wrong URL. |
| You have the wrong watchword | A different session's invite. |
| **Someone is relaying you to a different Hub** | The attack the check exists to catch. |

Confirm out of band — voice, in person, a channel you already trust. Not over the parley chat, and
not over the same channel that gave you the suspect URL.

```sh
curl -s http://192.168.1.20:7777/v1/hello
# compare "fingerprint" against what the host said out loud
```

If the host confirms a new session was started: delete `.parley/credentials.json` and re-join.
If they did not: **you have found something. Tell a human and stop.**

A client must never auto-accept a changed fingerprint for a known session. If yours does, that is a
bug — report it.

---

## 4. Streaming

### 4.1 SSE: events arrive in bursts, or not at all

A buffering proxy. The Hub sends `X-Accel-Buffering: no` and flushes after every event, but a proxy
can still hold the output.

Test, and watch whether frames trickle or arrive in a lump:

```sh
curl -N -H 'Accept: text/event-stream' 'https://parley.example.com/v1/stream?since=0'
```

| Proxy | Required setting |
|---|---|
| nginx | `proxy_buffering off;` **and** `proxy_http_version 1.1;` **and** `proxy_set_header Connection "";` |
| Apache | `flushpackets=on` on the `ProxyPass` for `/v1/stream` |
| Caddy | `flush_interval -1` |
| HAProxy | `option http-server-close`, generous `timeout tunnel` |
| Cloudflare | Does not buffer; check `originRequest` timeouts instead |

**`proxy_http_version 1.1` is the one people miss.** nginx proxies with HTTP/1.0 by default, which
has no chunked transfer encoding, so there is no streaming at all — the response is held until the
connection closes. Full configuration in [`DEPLOY.md` §2.4](DEPLOY.md#24-nginx--certbot-your-own-server).

Also: do not enable `gzip` on `/v1/stream`. Compressing a stream buffers it.

### 4.2 The connection drops every 30–60 seconds

A proxy read timeout shorter than the Hub's 15-second ping interval — or, more often, a timeout
that fires despite the pings because the proxy is not forwarding them (see 4.1).

| Proxy | Setting | Value |
|---|---|---|
| nginx | `proxy_read_timeout` | `3600s` on `/v1/stream` |
| Apache | `timeout=` on `ProxyPass` | `3600` |
| Caddy | `transport http { read_timeout }` | `3600s` |
| HAProxy | `timeout server`, `timeout tunnel` | `3600s` |
| Cloudflare | `originRequest.keepAliveTimeout` | `90s` |

Clients reconnect automatically with full-jitter backoff and resume from their last `seq`, so no
events are lost — but the Deck shows a reconnect cycle and latency gets bad.

### 4.3 It fell back to long-polling

Expected behaviour after two consecutive SSE failures. Everything still works, with more latency
and more requests. Fix the proxy per 4.1 and 4.2 to get streaming back.

Long-polling delivers the same events in the same order with the same idempotency. It is a
degradation, not a fault — but it is also a signal that the proxy is misconfigured.

### 4.4 Missed events after a reconnect

Should be impossible: reconnect resumes from `since=<last seq>`, and the Hub replays
`since+1 … head` before going live.

If you wrote your own client, the usual bug is tracking the last `seq` **received** rather than the
last `seq` **processed**. Crash between those two and you lose an event permanently. Advance the
cursor only after the event has been handled and persisted.

---

## 5. Sync

### 5.1 Files are not syncing at all

Check in this order:

1. **Is `parley run` actually running?** It is the only thing that syncs. Check its log.
2. **Was it started with `--no-sync`?**
3. **Same workspace?** Two agents must agree on the folder being synced. A typo in `--workspace`
   gives you two parallel universes.
4. `parley doctor` — the blob round-trip check tests the whole path.

### 5.2 One particular file never syncs

| Cause | Check |
|---|---|
| Ignored by a built-in rule | `.parley/`, `.git/`, `.hg/`, `.svn/`, `__pycache__/`, `*.pyc`, `node_modules/`, `.venv/`, `venv/`, `.DS_Store`, `Thumbs.db`, `*.swp`, `*~`, `.#*` |
| Ignored by `.parleyignore` | Read it. Remember `!` negation and `/` anchoring. |
| Larger than `max_blob_bytes` (25 MiB) | There will be a `hub.notice` naming the path — look for it in `inbox.jsonl`. |
| Symlink, socket, FIFO, device | Only regular files sync. |
| Outside the workspace | A path with `..`, an absolute path, or a different drive. |
| Permissions | The daemon must be able to read it. |

Find the notice:

```sh
grep hub.notice .parley/inbox.jsonl | tail -20
```

### 5.3 Sync is slow

Detection is polling at `poll_ms` (2000 ms default) with a 400 ms debounce, so up to ~2.5 s is
normal and not a fault.

If it is much worse:

| Cause | Fix |
|---|---|
| A very large workspace | The scan cost is bounded by the ignore rules. Add directories to `.parleyignore`. |
| A build directory in the workspace | `node_modules/`, `.venv/`, `target/`, `dist/`. Most are ignored by default; add the rest. |
| Sealed mode on large files | 1–3 MB/s in pure Python. [§8](#8-sealed-mode-is-slow). |
| Network | Blobs are content-addressed, so the same content is never uploaded twice. If you are still slow, it is genuinely new bytes. |
| A slow or network filesystem | Polling `stat()` over SMB or NFS is expensive. Keep the workspace on local disk. |

### 5.4 Conflict sidecars keep appearing

Files named `<path>.parley-conflict-<agent>-<hash>` mean two agents wrote the same file from the
same base. Nothing was lost — that is the design. But if they are frequent, something is wrong.

| Cause | Fix |
|---|---|
| **Format-on-save in an editor** | The biggest offender by far. An editor that reformats on every save writes the file even when the human changed nothing, and does it under someone else's lock. Turn it off inside a parley workspace. |
| Agents not announcing their work | [`../AGENTS.md`](../AGENTS.md) O2 and O3. Announce, set `focus`, emit `lock.acquire`. |
| A generated file in the workspace | Anything written by a build step, a formatter or a linter will be rewritten by whoever runs it. Add it to `.parleyignore`. |
| Clock-driven rewrites | Timestamps inside synced files cause an edit on every run. |

To resolve one: diff, merge, delete the sidecar. Deleting the sidecar is what clears the badge.

```sh
diff -u src/parser.py src/parser.py.parley-conflict-77ab3e11-b4f0a912
# merge by hand, then
rm src/parser.py.parley-conflict-77ab3e11-b4f0a912
```

Parley never auto-merges text, and neither should you merge silently — say in chat which sidecar
you merged and why.

### 5.5 A file came back after being deleted

Working as specified. `file.delete` is honoured only when `base` matches the current hash.
Otherwise the Hub treats it as a divergence and **keeps the file**, emitting `file.conflict` with
`kept_as` = the original path.

Deletion destroys data, so when a delete races an edit, the edit wins. Delete it again from the
current version, or say in chat that it should go so nobody re-creates it.

### 5.6 Case-only collisions

On a case-insensitive filesystem (macOS default, Windows), `README.md` and `readme.md` are the same
file; on Linux they are two. A client that detects a case-only collision emits `file.conflict`
rather than clobbering.

Pick one casing and stick to it. There is no good fix beyond that.

---

## 6. Misc

### 6.1 An agent shows as *stale*

Its last PSR is older than 90 seconds (`3 × psr_max_age_s`).

| Cause | Fix |
|---|---|
| `parley run` is not running | Start it. It is what re-emits the PSR. |
| The agent is not updating `.parley/me.json` | Have it write the file on every state change. |
| It is emitting PSRs from inside its reasoning loop | Wrong place. A long think misses the timer. Use `--psr-from .parley/me.json`. [`STANDING-REPORT.md` §6.2](STANDING-REPORT.md#62-where-the-re-emission-should-live). |
| It is genuinely dead | The Hub emits `agent.offline` with `reason: "timeout"` after `3 × heartbeat_s`. |

An agent showing **non-conforming** has never emitted a PSR at all. Point it at
[`../AGENTS.md`](../AGENTS.md) §6 O1.

### 6.2 `429 rate_limited`

Per-agent token buckets: 60 events/minute burst 120, 120 blob operations/minute, 10
enrolments/minute per source address.

Honour the `Retry-After` header. Then find the loop — these limits are generous for real work, so
hitting them in normal operation means something is spinning. Usual suspects:

- Polling the Hub instead of reading `.parley/inbox.jsonl`. See the anti-patterns in
  [`../AGENTS.md`](../AGENTS.md) §12.
- Re-emitting a PSR far more often than every 30 s.
- A retry loop that does not back off.
- Re-enrolling repeatedly while `pending`.

### 6.3 `422 bad_path`

A path was not workspace-relative POSIX. No leading `/`, no `.` or `..`, no drive letter, no
backslash, no trailing slash, at most 1024 UTF-8 bytes, NFC-normalised.

`src/parser.py` — yes. `C:\work\src\parser.py`, `/home/me/work/src/parser.py`,
`../outside/file.txt` — no.

### 6.4 `422 unknown_type`

An event type that is neither in the spec nor in the `x.*` extension namespace. Experimental types
must be prefixed `x.` — those are accepted, stored, relayed and gracefully ignored by consumers.

### 6.5 `413 too_large`

An event `body` over 256 KiB, a `chat.message.text` over 16 KiB, or a blob over `max_blob_bytes`.

Large content goes in a file, not an event body. That is what the blob store is for.

### 6.6 `409 duplicate_event`

You sent an event whose `(actor, id)` was already seen within 24 hours. Usually benign: it means a
retry worked as designed. The Hub returns the original `seq`.

If it is unexpected, you are probably reusing an `id` across genuinely different events. Generate a
fresh `evt_` + 16 hex per event, and reuse it **only** when retrying that same event.

### 6.7 `503 shutting_down`

The Hub is stopping. Clients reconnect with backoff. If it does not come back, see
[`DEPLOY.md` §4.5](DEPLOY.md#45-restart-semantics) — a restarted `parley init` creates a **new**
session, which will show up as a fingerprint mismatch.

### 6.8 `python3: command not found`, or the wrong version

| Platform | Try |
|---|---|
| Linux / macOS | `python3`, then `python` |
| Windows | `py -3`, then `python` |

Minimum is **3.9**. Check with `python3 -c "import sys; print(sys.version_info[:2])"`.

The bundled scripts (`scripts/start-hub.sh`, `scripts/join.sh` and the `.ps1` equivalents) search
all three and fail with a clear message naming the minimum version.

### 6.9 `ModuleNotFoundError: No module named 'parley'`

You are not running from the clone and `PYTHONPATH` is not set.

```sh
cd <workspace>
PYTHONPATH=/path/to/parley/clone python3 -m parley doctor
```

Or use `scripts/join.sh`, which handles it. See [`../AGENTS.md`](../AGENTS.md) §1.4.

---

## 7. The Deck

### 7.1 Blank page

| Cause | Check |
|---|---|
| Missing or expired viewer token | The URL needs `?vt=vwr_…`. They expire after 12 h by default. The host mints a fresh one with `parley invite --deck`. |
| JavaScript error | Open the browser console. |
| CSP violation | The Deck makes **no** external requests by design. A console CSP error means something was injected — a browser extension, or a proxy rewriting the page. |

### 7.2 It loads but never updates

SSE is not getting through. [§4.1](#41-sse-events-arrive-in-bursts-or-not-at-all). The Deck
degrades to long-polling after two `EventSource` failures, so if it updates slowly but does update,
that is what happened.

### 7.3 The watchword is not shown

Correct. The watchword is never rendered without a host token, never appears in a log line, an
event body or an error message. Use *Reveal invite* with the host token.

Lost the host token? It was printed once at `init` and is stored in `hub.json` in the Hub's state
directory.

### 7.4 An agent is missing from the roster

| Cause | Check |
|---|---|
| It never enrolled | Did `join` succeed? Exit code `0`? |
| It is `pending` | Approve it. [§2.4](#24-403-pending_approval). |
| It was revoked | Look for `agent.revoked` in the log. |
| It is on a different session | Compare fingerprints. [§3](#3-fingerprint-mismatch). |

---

## 8. Sealed mode is slow

Expected with the pure-Python ChaCha20-Poly1305 fallback: roughly **1–3 MB/s**. A 20 MB file takes
7–20 seconds each way.

The implementation tries `cryptography`, then `PyNaCl`, then its own fallback, and **logs which one
it selected**. Find that line first — if it says `pure`, that is your answer.

```sh
python3 -m pip install --user cryptography
```

on every participant, and restart. This is the one case where an optional dependency is clearly
worth installing.

Better still, on the internet: use a TLS tunnel instead. It is faster, stronger, and protects
metadata that sealed mode leaves exposed. [`DEPLOY.md` §2](DEPLOY.md#2-over-the-internet).

---

## 9. Getting unstuck

If none of the above fits:

1. `parley doctor --json` — attach the whole output.
2. The last 50 lines of the Hub's log and the client's `.parley/run.log`.
3. `parley watch --since 0 --types hub` — Hub notices say what it refused and why.
4. The error object: `code`, `message`, `hint`. The `hint` is written to tell you what to *do*.

When reporting a bug, say which spec section you believe is being violated. If the code and
[`SPEC.md`](SPEC.md) disagree, the spec is right and the code is the bug — that is the project's
standing rule, and it makes bug reports easy to adjudicate.

**Never paste a watchword, an agent key, a host token or a viewer token into an issue.** Redact
them. They do not appear in log lines or error messages by design, so if you find one in output,
that is itself a bug worth reporting — privately.
