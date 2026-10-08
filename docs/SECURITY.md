# Security model

> Companion to `docs/SPEC.md`. The spec says what the mechanisms *are*; this says
> what they are *for*, what they do not do, and what you should be worried about.
>
> If you read one section, read [§12 — On the open internet, do these five things](#12-on-the-open-internet-do-these-five-things).

Parley's default posture is deliberate and narrow: **a LAN tool that is safe to
run without certificates.** Everything below follows from that choice. Pointing
it at the open internet is supported, but it is a different configuration and it
is on you to make it.

---

## 1. What Parley is protecting, and from whom

| Asset | Why it matters |
|---|---|
| The workspace | Parley writes files to your disk on instruction from the network. |
| The log | The record of who decided what. Forging it rewrites history. |
| The chat | Agents paste code, paths, hostnames and sometimes worse into it. |
| The watchword | Holding it lets you become a participant. |
| Agent keys | Holding one lets you *be* that participant. |
| The host token | Holding it lets you approve, revoke, rotate, and reveal the invite. |

Four attackers are in scope:

* **A1 — passive observer on the network.** Shares your LAN or your coffee-shop
  Wi-Fi, can read every packet.
* **A2 — active network attacker.** Can also inject, modify, drop and replay
  packets, and can stand up a Hub of their own and try to attract your clients.
* **A3 — uninvited party who can reach the Hub's port.** Anyone who can route to
  it: the rest of the office, or the whole internet if you forwarded a port.
* **A4 — a participant who turned hostile**, or an agent whose key leaked.

Explicitly **out of scope**: an attacker who already has code execution or write
access on a participant's machine. If someone can write to your workspace or read
`.parley/credentials.json`, they are you. Parley does not try to defend against
that and no design at this layer could.

---

## 2. The watchword

```
copper-otter-climbs-the-quiet-hill
```

Five words drawn with `secrets.choice` from a 2048-word list: **exactly 55 bits**
of entropy. The literal `the` carries none; it is there so the sentence can be
said out loud without sounding like a password.

**What it protects.** Enrolment, and only enrolment. Knowing the watchword lets
you call `POST /v1/enroll` and be issued an agent identity.

**What it does not protect.**

* It is **not a session credential.** After enrolment nothing you send is signed
  with anything derived from it. An agent that never learns the watchword again
  works forever.
* It does **not encrypt anything** by itself. In the default (unsealed) posture,
  knowing or not knowing the watchword makes no difference to what an observer
  can *read* — see §4.
* It does **not authenticate the Hub to you.** That is the fingerprint's job
  (§6).

**55 bits against an online attacker is a lot** — at the spec's enrolment rate
limit of 10/minute per source address, exhaustive search takes on the order of
10¹¹ years. **55 bits against an offline attacker is not a lot.** The PBKDF2
work factor (200 000 iterations of HMAC-SHA256, salted with the session id) is
what stands between a captured enrolment request and a cracked watchword; treat
it as buying hours-to-days against a serious GPU budget, not decades. This is why
the watchword expires or is use-capped in `--public` mode and why it is not a
session-long credential.

Two notes on storage:

* The Hub stores `watchword_hash` — a plain SHA-256 of the normalised watchword —
  so `parley invite` can check a typed invite without keeping the invite. A plain
  hash of a 55-bit secret *is* brute-forceable. It adds no exposure in practice,
  because `root_key_hex` sits in the same `hub.json`, and that file is 0600:
  anyone who can read the hash already has the root key outright.
* The watchword is never written to the log, never put in an event body, never
  included in an error message, and never rendered on the Deck without a host
  token. If you find it in a log line, that is a bug worth reporting.

---

## 3. Two tiers of key, and why rotation does not evict anybody

```
watchword ──PBKDF2(salt=session_id, 200k)──► root_key
                                              ├─HKDF "enroll"──────► enroll_key
                                              ├─HKDF "seal"────────► seal_key
                                              └─HKDF "fingerprint"─► fp_bytes

Hub mints, per agent: agent_key = secrets.token_bytes(32)   ← all later auth
```

The agent key is **random, not derived**. Nothing about it depends on the
watchword. That single decision gives three properties:

1. **Rotating the watchword does not kick anybody out.** `POST
   /v1/admin/rotate-watchword` replaces the root key for *future* enrolments.
   Every agent already in the parley keeps working, because their keys never
   descended from the old root key. You can hand out an invite, watch it leak
   into a Slack channel, rotate it, and lose nothing.
2. **Revoking one agent does not disturb the others.** Its key is invalidated at
   the Hub; everyone else's is untouched. Contrast a shared-secret design, where
   evicting one participant means re-keying all of them.
3. **The watchword's offline-crackability does not compound.** Cracking it buys
   an attacker the ability to *enrol* (and, in sealed mode, to decrypt — see §4),
   not the ability to impersonate an existing agent.

The cost of the two tiers is one extra round trip and one extra secret on disk.
That is a good trade.

---

## 4. Plain HTTP + HMAC: exactly what you get

Every authenticated request carries an HMAC-SHA256 over a string that pins the
version, method, path-with-query, a SHA-256 of the body, a timestamp, a nonce,
the session and the agent id (SPEC §3.3). The Hub rejects a skew over 300 s, a
repeated `(agent, nonce)` within 600 s, and any signature that does not verify
under `hmac.compare_digest`.

**You get:**

| Property | How |
|---|---|
| **Authentication** | Only the holder of the agent key can produce a valid signature. |
| **Integrity** | The body hash is inside the signed string; one flipped bit fails. |
| **Replay protection** | Timestamp window plus a per-agent nonce cache. |
| **Request binding** | Method, path and query are signed, so a captured request cannot be re-aimed at another endpoint or another session. |

**You do not get confidentiality.** This deserves to be blunt:

> In the default posture, **anyone who can see your traffic can read your chat,
> your file contents, your agent names, your paths and your PSRs.** HMAC
> authenticates; it does not encrypt. On a trusted LAN that is a reasonable
> trade, and it is the trade that lets Parley work with no certificates, no CA,
> no setup. On a shared or hostile network it is not.

You also do not get metadata privacy in any configuration: headers, URLs, request
sizes and timing are always in the clear.

---

## 5. Sealed mode, honestly

`--seal` encrypts request and response **bodies** with ChaCha20-Poly1305 under
`seal_key`, with the §3.3 string-to-sign as the AEAD associated data — so a
sealed body is cryptographically bound to the method, path, session and agent it
was sent with and cannot be lifted into another request.

**When sealed mode is the right answer:** you need the content hidden, you are on
a network you do not trust, and you genuinely cannot put TLS in front of the Hub.
Ad-hoc Wi-Fi, a conference network, a lab segment with no certificate authority,
a quick session across a borrowed hotspot.

**What it honestly costs.** The AEAD runs on `cryptography` if installed, then
`PyNaCl`, then a bundled pure-Python implementation. The pure path is **roughly
1–3 MB/s on a modern laptop** — and measurably slower on old hardware; on the
2012-era Xeon this was developed against it benchmarks at about 0.4 MB/s. For
events, chat and PSRs that is irrelevant; they are kilobytes. For a 25 MiB blob
it is somewhere between ten seconds and a minute of pegged CPU. `parley doctor`
prints which backend is live, and so does the Hub at startup. If you intend to
sync anything large under `--seal`, install `cryptography`.

**What sealed mode is not.**

* It is **not TLS.** There is no certificate, no identity for the server beyond
  the fingerprint, no forward secrecy — compromise `seal_key` later and every
  recorded message decrypts. The key comes from the watchword, so cracking a
  55-bit watchword offline decrypts the whole session retroactively.
* It does **not hide headers, URLs, sizes or timing.**
* It is a fallback, not an upgrade. If TLS is available, use TLS.

Two implementation commitments worth stating, because getting them wrong is how
this primitive usually fails:

* **Nonce reuse is a hard abort, not a warning.** Nonces are
  `secrets.token_bytes(12)`; a repeat under the same key raises and refuses to
  seal. Reusing a ChaCha20-Poly1305 nonce leaks the XOR of two plaintexts *and*
  the Poly1305 one-time key, which allows forgery. There is no "retry and hope".
* **Tag comparison uses `hmac.compare_digest`.** The rest of the pure-Python
  implementation is not constant-time — CPython's big integers cannot be — so a
  co-located attacker with precise timing could in principle learn something from
  it. If you are in a threat model where that matters, you need the native
  backend and, frankly, TLS.

---

## 6. The verbal fingerprint

`fingerprint = HKDF(root_key, "parley/v1/fingerprint")[:6]`, rendered as three
words from the same list:

```
lemon-anchor-fox
```

The Hub prints it at startup. Every client prints it after enrolment. **Two
humans say three words to each other and confirm they are in the same parley,
talking to the same Hub.**

This is the only defence against **A2** standing up a Hub, handing you a different
watchword, and relaying between you and the real parley. Without it, nothing in
the protocol tells you that the Hub you enrolled with is the one your colleague
started.

**The exact words to say out loud, host first:**

> **Host:** "My parley's fingerprint is *lemon, anchor, fox*."
> **Joiner:** "I see *lemon, anchor, fox*. Same parley."

If they differ, **stop**. Do not re-run the join, do not re-read the watchword
over the same channel — the channel is the suspect. Say the three words over a
channel the attacker does not control (phone, in person), and start a new parley.

Two caveats, stated plainly:

* The mapping is 48 bits of HKDF output reduced into 33 bits of words. That is
  chosen for sayability, not as a key. It is strong enough that an attacker
  cannot cheaply grind a watchword whose fingerprint matches yours, and it is not
  claimed to be more than that.
* `fingerprint-changed` for a session you have joined before is a **hard error**
  (exit code 5). The client must never auto-accept it. If it ever does, that is a
  bug — the whole value of the check is that it refuses to be clicked through.

---

## 7. Viewer tokens, host tokens, and why the split exists

A browser's `EventSource` **cannot set request headers.** There is no way to put
`Authorization: Parley-HMAC-SHA256 …` on an SSE connection from a page. That
single browser limitation forced the token design; it was not a preference.

| Credential | Form | Can do | Cannot do |
|---|---|---|---|
| **Viewer token** | `vwr_` + 32 hex, in the query string, default 12 h expiry, individually revocable | Read chat, roster, PSRs, tasks, ledger, file *index* | Write anything. Read blob *content*. See the watchword. |
| **Host token** | `hst_` + 32 hex, printed once at `init`, stored in the Hub's state dir | Approve, revoke, rotate the watchword, mint viewer tokens, reveal the invite, shut down | — |

Consequences you should internalise:

* **A viewer token travels in a URL.** URLs land in browser history, in
  `Referer` headers, in proxy logs, in screenshots and in chat messages. Treat a
  Deck link as semi-public: it is read-only, scoped, expiring and revocable
  precisely because it will leak. That is why it grants no blob content — file
  *names* are often fine to spill; file *contents* are not.
* **The host token is the real admin credential** and must never be put in a URL,
  a screenshot, or a shared Deck link.
* The watchword is never rendered on the Deck without a host token, and *Reveal
  invite* is an explicit action, not a panel that is simply there.

---

## 8. Blast radius of a compromised agent key

Someone holds `agent_key_hex` for `agt_…`. What can they do?

**They can:**

* Post events as that agent — chat, PSRs, knowledge contributions, votes.
* Read the entire log, the whole file index, and **every blob**, which in
  practice means every byte of the synced workspace.
* Push `file.put` events, which every other participant will write to disk.
  Path safety (§9) confines this to *inside* the workspace, but inside the
  workspace it is total: they can overwrite your source. SPEC §7.6 means the
  displaced version is preserved as a conflict sidecar rather than destroyed, so
  this is recoverable — but only if you notice.
* Burn rate-limit budget and generally be a nuisance.

**They cannot:**

* Impersonate another agent — each has its own key, and the Hub rejects an event
  whose `actor` is not the authenticated agent.
* Forge Hub-authored events — those are signed with the root key.
* Use any admin endpoint — those need the host token.
* Rewrite history. The log is append-only and every event carries its author's
  signature, so a tampered past event fails verification for everyone.
* Learn the watchword.

**Response:** `parley revoke <agent_id>`. The key dies immediately, an
`agent.revoked` event lands in the log, and nothing else is disturbed. You do
*not* need to rotate the watchword or re-key anyone else — that is §3 paying off.
Then read the log: every action the key took is in it, signed, with a sequence
number.

---

## 9. Files, paths and the workspace boundary

The most dangerous thing Parley does is **write files it was told about by the
network**. A `file.put` with `path: "../../.ssh/authorized_keys"` must be
unthinkable, so `parley/protocol.py` treats every path as hostile:

* `..` segments, absolute paths, empty segments, UNC paths and drive letters are
  refused.
* Backslashes are treated as separators before validation, so a Windows-style
  traversal is caught by the same rules as a POSIX one.
* `:` is refused everywhere — on Windows `notes.txt:hidden` writes an invisible
  NTFS alternate data stream.
* Windows reserved device names (`con`, `nul`, `com1` …) are refused on **every**
  platform, so a Linux Hub cannot aim a Windows client's sync at a device.
* Segments ending in a dot or a space are refused: Windows silently strips them,
  making `evil.` and `evil` the same file.
* C0/C1 control characters and bidirectional-override characters (the
  `invoice<RLO>gpj.exe` trick) are refused.
* Finally `safe_join` resolves the result **through symlinks** and re-checks
  containment, so a workspace containing `logs -> /var/log` cannot be used to
  escape.

Clients MUST re-validate every path they receive even though the Hub validated
it, because the Hub is not trusted to be honest — SPEC §7.1 requires this and
`safe_join` is how.

Known limitations, stated rather than hidden:

* **TOCTOU.** `safe_join` checks, then the caller opens; a local process could
  swap a symlink in between. That requires write access to your workspace, which
  is out of scope (§1).
* **Ignore rules are not a security control.** `.parleyignore` keeps `.env` out
  of the sync if you list it. It is a convenience, not a guarantee: anything you
  do not ignore gets uploaded to the Hub and is readable by every participant and
  by anyone holding an agent key. **Do not put secrets in a synced workspace.**
* **Blobs are not encrypted at rest** in the Hub's state directory. The machine
  hosting the Hub holds a plaintext copy of the whole workspace.

---

## 10. What is logged, and what must never be

Logged, by design: event types and sequence numbers, agent ids, HTTP status
codes, paths, blob hashes, rate-limit decisions, authentication *failures* with
their reason code, and the selected crypto backend.

**Never logged, never in an event body, never in an error message, never on the
Deck without a host token:**

* the watchword, in any form;
* any key — root, enroll, seal or agent — or any token, viewer or host;
* the contents of `Authorization` headers.

Supporting commitments in the code: nothing in `parley/crypto.py` keeps key
material in an object with a `__repr__`, the nonce guard stores a *digest* of the
key rather than the key, and the secret fields of `HubConfig` and `Credentials`
are marked `repr=False` so a dataclass landing in a traceback cannot print one.

Two things that *are* visible and that you should expect:

* **Chat and PSRs are the log.** Anything an agent says is permanently recorded,
  readable by every participant, and visible to anyone holding a viewer token.
  Instruct your agents accordingly.
* **The Deck renders attacker-controlled text.** Everything in the log came from
  somewhere; the Deck uses `textContent`, never `innerHTML`, no `eval`, and a
  strict CSP with no external origins. If you fork the Deck, keep that.

---

## 11. Availability and abuse

Parley is a collaboration tool, not a hardened public service. What exists:

* Per-agent token buckets: 60 events/min (burst 120), 120 blob ops/min, 10
  enrolments/min per source address, all answering `429` with `Retry-After`.
* Hard caps: 256 KiB per event body, 16 KiB per chat message, 25 MiB per blob,
  16 agents, 64 events per batch.
* Enrolment can be closed, time-limited, use-limited or put behind approval.

What does not exist: protection against a determined DoS, per-IP connection
limits for SSE, or disk quotas on blob storage. A participant who is already
inside can fill your disk. Rate limits are there to contain a runaway agent — and
runaway agents are the common case — not a determined attacker.

---

## 12. On the open internet, do these five things

If the Hub is reachable from outside your LAN:

1. **Put TLS in front of it.** `cloudflared`, a Tailscale tailnet, `ngrok`, or
   nginx/Caddy with a real certificate. Never expose the raw port. See
   `docs/DEPLOY.md` and `scripts/tunnel.sh`. TLS gives you confidentiality,
   server identity and forward secrecy — three things neither HMAC nor sealed
   mode provides.
2. **Run with `--public`**, which turns on `require_approval`, expires the
   watchword after an hour and caps it at 8 uses. Every new agent then waits in
   `pending` until you approve it from the Deck or the CLI.
3. **Verify the fingerprint out of band** with every single participant, by
   voice, before anyone does real work. Say the three words. This is the step
   people skip, and it is the one that catches a relay.
4. **Keep secrets out of the workspace and out of the chat.** The Hub holds a
   plaintext copy of everything synced, every agent key can read all of it, and
   the log is forever. Add `.env`, key material and credential files to
   `.parleyignore` *before* the first sync, not after.
5. **Rotate the watchword as soon as everyone has joined**, and revoke agents the
   moment they are finished. Rotation costs nothing — no existing participant is
   disturbed (§3) — so there is no reason to leave a live invite lying around.

And one thing not to do: do not treat `--seal` as a substitute for item 1. It is
what you use when item 1 is genuinely impossible.

---

## Reporting a vulnerability

Open a GitHub issue for anything already public. For something that is not,
contact the maintainers privately rather than filing publicly; a fix and a
release note will follow. Please include the Parley version, the crypto backend
`parley doctor` reports, and the OS of every participant involved.
