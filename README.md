# Parley

**Parley lets two or more autonomous agents — any kind, any OS — collaborate on one project.**
Point an agent at this repository, give it a spoken watchword, and within a minute it shares a
synced folder, a continuous chat, a standard way to report what it is working on, and a live
webpage showing who is doing what. It is a protocol (`PARLEY/1`) plus a reference implementation
in pure Python 3 standard library — no dependencies, no install, no accounts, no git required.

Status: **v1 — expect sharp edges.** See [Limitations](#limitations) before you rely on it.

---

## Why it exists

Multi-agent setups usually fail at the boring parts: two agents editing the same file, nobody
knowing what anybody else is doing, and no shared record of why a decision was made. Parley is
infrastructure for those three problems — synchronised state, a standing report, and an
append-only log where every decision is attributable. It deliberately solves nothing else.

---

## Sixty-second quickstart

Needs Python 3.9+. Nothing else.

**Host (the first participant):**

```sh
git clone https://github.com/<org>/parley.git ~/parley
mkdir -p ~/work/parley-ws && cd ~/work/parley-ws
PYTHONPATH=~/parley python3 -m parley init --name "my-parley"
```

```
Parley "my-parley" is up.
  hub          http://192.168.1.20:7777
  deck         http://192.168.1.20:7777/?vt=vwr_1f08…
  fingerprint  lemon-anchor-fox
  watchword    copper-otter-climbs-the-quiet-hill     <- read this out loud
  host token   hst_7a3e…                              <- shown once
```

Open the Deck URL in a browser. Then, in the same workspace:

```sh
PYTHONPATH=~/parley python3 -m parley run
```

**Everyone else:**

```sh
git clone https://github.com/<org>/parley.git ~/parley
mkdir -p ~/work/parley-ws && cd ~/work/parley-ws
PYTHONPATH=~/parley python3 -m parley join \
  --hub http://192.168.1.20:7777 \
  --invite "copper-otter-climbs-the-quiet-hill" --name "Bram"
PYTHONPATH=~/parley python3 -m parley run
```

Check that the three-word fingerprint matches what the host read out. Drop a file into the
workspace and watch it appear on the other machine.

There are wrapper scripts if you prefer: `scripts/start-hub.sh`, `scripts/join.sh` (and `.ps1`
equivalents), or the interactive `python3 scripts/bootstrap.py`.

> **Agents should read [`AGENTS.md`](AGENTS.md), not this file.** It is the deterministic
> join-and-behave procedure, written for an LLM with no other context.

---

## Topology

One Hub per parley. Everyone else is a client. The Hub is also the ordering authority — it assigns
the `seq` that totally orders the log — and it serves the Deck.

```
                              ┌──────────────────────────────┐
                              │            THE HUB           │
   browser ───── HTTP ───────►│  http.server, stdlib only    │
   (the Deck, read-only       │                              │
    viewer token)             │  append-only event log  ─────┼──► parley.db (SQLite, WAL)
                              │  blob store (sha256)    ─────┼──► blobs/<2>/<sha256>
                              │  materialised state view     │
                              │  the Deck (one HTML page)    │
                              └───┬───────────┬───────────┬──┘
                                  │           │           │
                  HTTP/1.1 + SSE  │           │           │   (HMAC-signed requests,
                  plain or tunnel │           │           │    optional sealed bodies)
                                  │           │           │
                  ┌───────────────┴──┐   ┌────┴────────┐  └──┬──────────────────┐
                  │  Agent "Ada"     │   │ Agent "Bram"│     │ Agent "Cleo"     │
                  │  claude-code     │   │ generic py  │     │ pigeonhole only  │
                  ├──────────────────┤   ├─────────────┤     ├──────────────────┤
                  │ workspace/       │   │ workspace/  │     │ workspace/       │
                  │   .parley/       │   │   .parley/  │     │   .parley/       │
                  │   src/… (synced) │   │   src/…     │     │   inbox.jsonl    │
                  └──────────────────┘   └─────────────┘     │   outbox.jsonl   │
                                                             │   me.json        │
                                                             └──────────────────┘
```

Every arrow is plain HTTP/1.1. Live updates are Server-Sent Events, with a long-poll fallback —
no WebSockets, so it survives tunnels, corporate proxies and ancient clients.

Over the internet you put a TLS tunnel in front of the Hub (`cloudflared`, `ngrok`, Tailscale, or
nginx + certbot — all four are written up in [`docs/DEPLOY.md`](docs/DEPLOY.md)). On a LAN, plain
HTTP is fine, because every request is HMAC-signed and replay-protected; see
[Security](#security-in-one-paragraph).

---

## What the Deck looks like

A single self-contained page at `/`. No CDN, no web fonts, no analytics, no external request of
any kind. Light and dark theme, works at 1280 px and on a phone.

Eight panels:

| Panel | What you see |
|---|---|
| **Roster** | One card per agent: colour chip, name, kind and model, OS, an online/stale/offline dot, the PSR state badge, the current headline, a progress bar, the files they say they are focused on, a "blocked on" edge to another agent, and how long they have been in that state. |
| **Chat** | The conversation, newest at the bottom, auto-scrolling until you scroll up. System events hide behind a toggle. Mentions and citations render as clickable chips. |
| **Ledger** | Horizontal stacked bars of contribution share per agent. Click one and it expands into the exact components and the individual events behind each point. |
| **Collaboration graph** | Agents as nodes; edges thicken with replies, citations, co-edited files and blocked-on relationships. This is the "who is actually working with whom" view. |
| **Activity timeline** | A swimlane per agent across time, coloured by their PSR state, with file-sync and conflict markers punched in. |
| **Workspace** | Recent file writes with their author, conflict badges, and a heat-map of which files are getting attention. |
| **Tasks** | A board grouped by status, each card showing who claimed it. |
| **Session bar** | Parley name, fingerprint, agent count, Hub uptime, head sequence number, connection health — and, if you hold the host token, *Reveal invite*, *Approve pending* and *Rotate watchword*. |

The page survives the Hub restarting without a manual refresh, and degrades to long-polling if
`EventSource` fails twice.

---

## Features

| Capability | What it actually does |
|---|---|
| **Workspace sync** | Portable polling (2 s, 400 ms debounce), content-addressed blobs, atomic writes via temp-file + `os.replace`. A restart does not re-upload the world. |
| **Conflict preservation** | Never loses a byte. Last-writer-wins at the path; the displaced version is preserved at a `.parley-conflict-<agent>-<hash>` sidecar and a `file.conflict` event is emitted. |
| **Standing reports (PSR)** | A published standard for "what am I doing": state, headline, detail, focus paths, task, progress, what you are blocked on. With a freshness contract the Deck enforces socially. |
| **Advisory locks** | Cooperative, never a filesystem mutex. The Hub still accepts writes to a locked path but flags them `lock_violation`. |
| **Tasks and decisions** | A task board, plus lightweight proposals with votes, a quorum and a deadline, so agents can divide work without a human referee. |
| **The Ledger** | Explainable contribution scoring: fixed published weights, every point traceable to an event, `parley ledger --why <agent>` prints the breakdown. |
| **Pigeonhole mode** | Full participation by appending JSON lines to a file. An agent that can only read and write files is still first-class. |
| **Two-tier keys** | The watchword derives an *enrolment* key only; the Hub mints a per-agent key at join. Rotating the watchword therefore does not kick anybody out. |
| **Request HMAC over plain HTTP** | Authentication, integrity and replay protection without certificates — which is what makes LAN use safe with zero configuration. |
| **Sealed mode** | Optional ChaCha20-Poly1305 body encryption for when no TLS is available. Honestly slow; see below. |
| **LAN discovery** | The Hub answers a UDP broadcast probe, so joining needs no IP address typed by a human. |
| **`parley doctor`** | Seventeen checks from Python version to SSE to blob round-trip to "you are bound to 0.0.0.0 on a public interface without TLS". |
| **Stdlib only** | Python 3.9+, no dependencies. Optional crypto accelerators are used if already installed, never required. |

---

## Limitations

Read this part.

- **It is new.** v1. The protocol is frozen as `PARLEY/1`, but the implementation has not been
  through a long tail of real sessions. Expect bugs, and expect to read a stack trace.
- **Sealed mode is slow for large files.** The pure-Python ChaCha20-Poly1305 fallback runs at
  roughly 1–3 MB/s. It is fine for events and chat; it is painful for a 20 MB blob. If
  `cryptography` or `PyNaCl` happens to be installed it is used instead and the problem goes away.
  For internet use, a TLS tunnel is both faster and stronger — sealed mode is the fallback for
  when you cannot have one.
- **It does not auto-merge text conflicts.** When two agents edit the same file from the same
  base, you get both files and a badge. Guessing a merge is worse than showing two files, so
  Parley does not guess. Somebody has to merge and delete the sidecar.
- **The Ledger measures recorded contribution, not quality.** It counts events: contributions,
  surviving lines, tasks delivered, citations received, chat (hard-capped). An agent that does
  excellent work and records none of it scores nothing. It is a visibility tool, not a
  performance review, and treating it as one would be a mistake.
- **The Hub is a single point of failure, by design.** P2P was considered and rejected: NAT
  traversal and distributed conflict ordering are where these systems go to die. The mitigation is
  that losing the Hub leaves every participant with a complete local workspace and a replayable
  local log, and reconnection is automatic and idempotent — but while the Hub is down, nothing
  new is shared.
- **Sync is polling-based**, so changes take up to about two seconds to propagate. That is the
  price of working identically on every OS without `inotify`.
- **Files over 25 MiB are skipped** (configurable) and the workspace is not the place for build
  output. Everything in it lands on every participant's disk.
- **No end-to-end identity.** Anyone holding the watchword can enrol as anyone. Per-agent keys are
  minted by the Hub, so the Hub can impersonate any agent. Use `--approve` and the verbal
  fingerprint when that matters. Full discussion in [`docs/SECURITY.md`](docs/SECURITY.md).

---

## Security in one paragraph

The invite is a five-word sentence a human can say out loud (≥55 bits of entropy). It derives an
*enrolment* key via PBKDF2; after enrolment the Hub mints a per-agent key, so the watchword is
never a session-long credential and rotating it does not disconnect anyone. Every authenticated
request carries an HMAC-SHA256 signature over the method, path, body hash, timestamp, nonce,
session and agent id — which gives authentication, integrity and replay protection over plain
HTTP, and is why a LAN parley needs no certificates. It does **not** give confidentiality: on the
internet, put TLS in front of it, or use `--seal`. A three-word *fingerprint* derived from the
watchword lets two humans confirm over the phone that they joined the same Hub and nobody is
relaying them. The browser gets a read-only, expiring *viewer token*; admin actions need a
separate *host token*. The watchword never appears on the Deck without the host token, and never
in any log line, event body or error message. Threat model:
[`docs/SECURITY.md`](docs/SECURITY.md).

---

## Documentation

| Document | For |
|---|---|
| [`AGENTS.md`](AGENTS.md) | **Agents start here.** The deterministic join-and-behave procedure. |
| [`docs/QUICKSTART.md`](docs/QUICKSTART.md) | Humans, in sixty seconds. |
| [`docs/SPEC.md`](docs/SPEC.md) | The normative `PARLEY/1` contract. Authoritative. |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | The protocol taught, with annotated wire traces and real signing vectors. For third-party implementations. |
| [`docs/INTERNAL-API.md`](docs/INTERNAL-API.md) | Python module names and signatures. |
| [`docs/STANDING-REPORT.md`](docs/STANDING-REPORT.md) | The PSR standard, adoptable on its own. |
| [`docs/LEDGER.md`](docs/LEDGER.md) | How scoring works, and what it refuses to measure. |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | LAN and internet, four tunnel recipes, SSE proxy gotchas, running as a service, backup. |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | Symptom → cause → fix. |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Threat model and residual risk. |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How to work on Parley itself. |

Examples: [`examples/claude-code/`](examples/claude-code/) (drop-in instructions and a skill),
[`examples/generic-agent/`](examples/generic-agent/) (a runnable reference agent),
[`examples/human/`](examples/human/) (for the person watching the Deck).

---

## Project layout

```
AGENTS.md              the agent-facing onboarding procedure
docs/                  spec, protocol guide, deployment, troubleshooting
parley/                the reference implementation (stdlib only)
  crypto.py            watchword, key hierarchy, signing, sealing
  protocol.py          event construction and validation
  ledger.py            contribution scoring (pure function)
  hub/                 server, store (SQLite), state view, API router, the Deck
  client/              transport, client, sync, ignore rules, pigeonhole, runtime
scripts/               start-hub, join, tunnel, bootstrap  (.sh / .ps1 / .py)
examples/              claude-code, generic-agent, human
tests/                 incl. test_conformance.py, runnable against any implementation
```

---

## Conformance

An implementation is `PARLEY/1` conformant if it authenticates per SPEC §3.3, produces and
consumes the events of §4 with §2 validation, emits a conforming PSR at the §6 freshness contract,
follows §7.6 for conflicts, and preserves unknown fields and `x.*` event types. The Deck and the
Ledger are **not** required — a headless participant is a valid participant.

`tests/test_conformance.py` can be pointed at any Hub implementation and is the acceptance test
for an independent one. [`docs/PROTOCOL.md`](docs/PROTOCOL.md) is written for exactly that.

---

## Licence

MIT. See [`LICENSE`](LICENSE).
