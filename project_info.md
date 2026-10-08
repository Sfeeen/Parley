# Parley

Created: 2026-10-08

## Goal
A protocol + reference implementation that lets 2 or more agents — any kind, any OS — collaborate
on one project. Point an agent at the public GitHub repo and it must be able to join a running
session in under a minute. Each participant gets an auto-synced project folder, a continuous
shared chat, a standard way to report what it is working on, and at least one participant hosts a
live webpage ("the Deck") visualising who is collaborating, what they are doing, how much
knowledge each has contributed, and the agent chat. Works on a LAN and over the internet. Secured
by a readable-sentence watchword the first participant reads out to the others.

Success = a stranger's agent, given only the repo URL and a spoken watchword, is participating
correctly without a human writing any glue code.

## Status
- Spec written and frozen as the build contract: `docs/SPEC.md` (PARLEY/1).
- Project scaffolding + git repo created.
- Builders fanned out across the six components (crypto/protocol, hub, client/sync, Deck,
  ledger/tests, docs/scripts).
- Nothing pushed to GitHub yet — repo exists locally only, `gh` is not installed on this box.

## Key decisions
- **Name:** Parley. Vocabulary: a *parley* (session), the *Hub* (server), the *watchword*
  (invite), the *Deck* (webpage), *PSR* (Parley Standing Report = the activity standard), the
  *Ledger* (contribution scoring), *Pigeonhole mode* (file-only participation).
- **Python 3, stdlib only, 3.9 floor.** Chosen so any agent on any machine can run it with zero
  install step. Optional crypto accelerators used only if already present.
- **One Hub, anyone can host.** Not P2P — NAT traversal and conflict ordering make P2P fragile.
  The Hub is also the one ordering authority (`seq`) and serves the Deck.
- **Plain HTTP + Server-Sent Events, not WebSockets.** Implementable in the stdlib, survives
  corporate proxies and tunnels; long-poll fallback for hostile networks.
- **Sync is built into the protocol, not git.** Participants need no GitHub account, no git, no
  push rights. The public repo is bootstrap instructions only, never the data path.
- **Two-tier keys.** The watchword derives an *enrolment* key only; the Hub mints a per-agent key
  at join. This is why rotating the watchword does not kick existing agents out.
- **Request HMAC over plain HTTP** gives auth + integrity + replay protection without TLS, making
  LAN use safe certificate-free. Confidentiality is TLS-tunnel-first, with optional pure-Python
  ChaCha20-Poly1305 "sealed mode" as the no-TLS fallback (honestly documented as slow, ~1-3 MB/s).
- **Viewer tokens for the browser** because `EventSource` cannot set auth headers. Read-only,
  expiring, never reveal the watchword. Separate host token for admin actions.
- **Conflicts never lose a byte.** Last-writer-wins at the path, displaced version preserved at a
  `.parley-conflict-*` sidecar. No auto-merge — guessing a merge is worse than showing two files.
- **The Ledger is explainable by rule.** Fixed, published, user-overridable weights; every point
  traceable to an event. It measures recorded contribution, not quality, and says so.

## Next steps
1. Integrate the six builders' output; resolve interface mismatches against `docs/SPEC.md`.
2. Run the full test suite + a real two-participant loopback session end-to-end.
3. Visual pass on the Deck with live data.
4. Decide the GitHub org/account and push (needs Sven — `gh` not installed, no credentials here).

## Notes
- Build contract is `docs/SPEC.md`. If code and spec disagree, the spec wins — fix the code.
- Local Python is 3.14.4 but the target floor is 3.9: do not let 3.10+ syntax creep in.
  `from __future__ import annotations` at the top of every module.
- `gh`, `cloudflared` and `ngrok` are all absent on this machine; tunnel scripts are written
  against them but cannot be smoke-tested here.
- Repo: /home/sven/Desktop/Projects/parley (git initialised, no remote yet).
