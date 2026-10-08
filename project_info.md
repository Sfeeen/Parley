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
**Wave 1 complete. PAUSED at Sven's instruction — do not start wave 2 without his word.**

- `docs/SPEC.md` (PARLEY/1) is the frozen build contract; `docs/INTERNAL-API.md` pins module signatures.
- All seven wave-1 components built and integrated: foundations/crypto, hub, client/sync, the Deck,
  ledger, CLI/doctor, docs/scripts. 115 files, ~34k lines.
- **Test suite: 527 tests, 6 failures** (`python3 -m unittest discover -s tests`, ~76 s).
  All 6 failures are one defect — see Next steps #1.
- Verified working end-to-end for real: `parley init` → `parley join` ×2 → two `parley run`
  daemons → two-way file sync (exec bit + byte-identical binaries), pigeonhole chat, clean
  SIGTERM with `agent.bye`. `parley doctor` 18/18 from a guest. `--discover` finds the Hub over UDP.
- The Deck was verified in headless Chromium: renders 1440/1280/390 px in both themes, zero
  off-origin requests, XSS attacks in the fixture all neutralised, reconnect state machine walks
  live → reconnecting → long-poll → recovery against a killed Hub.
- **The Exchange (SPEC §15) is specced but NOT implemented.** That is wave 2.
- Nothing pushed to GitHub — repo is local only, `gh` is not installed on this box.

## Open defects (wave 1)
1. **`protocol.normalise_path` is non-conformant** — silently rewrites `.` segments and
   backslashes instead of rejecting them (SPEC §7.1 says MUST reject). Causes all 6 test
   failures. Not a containment hole (`safe_join` still resolves and verifies), but two agents
   can address one file by two spellings. One-line-ish fix in `parley/protocol.py`.
2. **Sealed mode is broken** — hub and client disagree on the enrolment body AAD, so
   `init --seal` + `join --seal` fails with `400 could not decrypt`. Root cause is a spec bug:
   §3.6's AAD is circular (it embeds `sha256(body)`, which the Hub cannot compute before
   decrypting). Both sides invented different non-circular readings. Fix the spec first, then
   make both sides agree. This is the no-TLS internet path, so it matters.
3. **Restart trap** — `parley init` mints a NEW session every run, so a systemd
   `Restart=on-failure` silently starts a *different* parley and every client fails with
   `fingerprint_mismatch`. Needs `parley resume` or `init --reuse`. Flagged in DEPLOY.md §4.5.
4. **Host token is sent two ways** by both the CLI and the Deck (`X-Parley-Host-Token` and
   `Authorization: Parley-Host`). Pick one server-side, delete the other.
5. `Store.seen_nonce` ignores its injected `ts` and uses `time.time()` — not exploitable,
   but makes TTL expiry untestable except by back-dating stored rows.
6. `invite --reveal` cannot work as specced (Hub stores only the root key + a hash). It exits
   honestly pointing at `--rotate`. Decide whether to drop it from SPEC §11 or retain plaintext.
7. `init --json` puts `host_token` in the same envelope as the watchword — review.
8. Unverified: `tunnel.sh` provider branches, both `.ps1` scripts (no pwsh here), any browser
   but Chromium, the public-exposure warning branch (no public address on this box).

## Spec defects found by implementing it (fix in SPEC.md)
- §3.6 sealed AAD is circular — unimplementable as written (see defect 2).
- §2 clock-skew `ts` rewrite invalidates the author's `sig`. Hub's resolution: verify over bytes
  as received, rewrite, record `body._original_ts`, re-sign with the Hub-minted key. Spec this.
- §3.5 vs §3.8 contradict: rotating the watchword changes the fingerprint, but §3.5 calls a
  changed fingerprint a hard error. Hub announces old→new in a signed `hub.notice` and keeps the
  last 3 root keys. Spec this.
- §3.5 never said how 6 fingerprint bytes become 3 words. Implemented as three 2-byte big-endian
  chunks mod 2048 (unbiased, since 65536 % 2048 == 0). Pin it.
- §7.1 PBKDF2 salt encoding was implicit — it is UTF-8 of the full `ses_`-prefixed id.
- §11 omits `--discover`, which is implemented. `POST /v1/admin/reveal` is called by the Deck but
  is not in §5.

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

## Next steps (all gated on Sven's go-ahead — he asked to pause after wave 1)
1. Fix `protocol.normalise_path` → 527/527 green.
2. Fix the §3.6 sealed-AAD spec bug, then make hub and client agree; sealed mode end-to-end.
3. Close the remaining wave-1 defects above (restart/resume, host-token duplication, seen_nonce).
4. Wave 2: implement the Exchange (SPEC §15) across hub, client, Deck, CLI, ledger, docs.
5. Decide the GitHub org, find-and-replace the `<org>/parley` placeholder, push.
   Needs Sven — `gh` is not installed and there are no credentials here.

## Notes
- Build contract is `docs/SPEC.md`. If code and spec disagree, the spec wins — fix the code.
- Local Python is 3.14.4 but the target floor is 3.9: do not let 3.10+ syntax creep in.
  `from __future__ import annotations` at the top of every module.
- `gh`, `cloudflared` and `ngrok` are all absent on this machine; tunnel scripts are written
  against them but cannot be smoke-tested here.
- Repo: /home/sven/Desktop/Projects/parley (git initialised, no remote yet).
