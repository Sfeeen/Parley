# Contributing to Parley

Thanks for looking. This document is short because most of the rules are already written down
somewhere binding.

---

## The one rule

**[`docs/SPEC.md`](docs/SPEC.md) is the contract.** If the code and the spec disagree, the spec is
right and the code is a bug.

Changing behaviour therefore means changing the spec *first*, in its own commit, with a rationale.
A pull request that quietly makes the implementation diverge from the spec will be asked to pick
one side.

[`docs/INTERNAL-API.md`](docs/INTERNAL-API.md) binds the Python module names and signatures so
that independently-built modules link up. You may *add* to it. Do not rename or re-shape what is
already there without a corresponding discussion.

---

## Design rules you cannot break

These are SPEC §0.2. They are non-negotiable because the whole value proposition rests on them.

| | Rule | What it means in a diff |
|---|---|---|
| **R1** | Stdlib only | No new runtime dependency. Ever. Optional accelerators are used only if already installed and must have a pure-stdlib fallback. |
| **R2** | Python 3.9 floor | No `match`, no PEP-604 `X \| Y` at runtime, no `tomllib`, no `str.removeprefix` carelessness. Every module starts with `from __future__ import annotations`. |
| **R3** | Any OS | No POSIX-only syscall on the hot path. No `inotify`, no required `fcntl`, no shelling out to `git` or `curl`. `pathlib` internally, POSIX-style paths on the wire. |
| **R4** | Plain HTTP/1.1 | No WebSockets. SSE with a long-poll fallback. |
| **R5** | Never lose a byte | A conflict preserves both sides. The log is append-only; nothing is rewritten or deleted in place. |
| **R6** | Explainable, not magic | Every number the Deck shows must be traceable to events in the log, and the UI must be able to show that breakdown. No learned weights. |
| **R7** | Degrade, don't die | Losing the Hub must leave every participant with a complete local workspace and a replayable local log. Reconnect is automatic and idempotent. |

If your change needs an exception to one of these, it needs a spec change and a very good reason.

---

## Code conventions

From [`docs/INTERNAL-API.md`](docs/INTERNAL-API.md):

- **Logging** via `logging.getLogger("parley.<module>")`. Never `print()` outside `cli.py`.
  **Never log a key, a token or the watchword** — not at debug level, not in a traceback, not in
  an error message. `crypto.py` deliberately exposes no `__repr__` that leaks key material.
- **Threads, not asyncio.** The stdlib `http.server` is thread-based and it keeps the 3.9 floor
  simple. Anything shared is guarded by an explicit `threading.Lock`, and the lock order is
  documented at the point of acquisition.
- **No global mutable singletons.** `Store`, `HubConfig` and `ParleyClient` are passed explicitly.
- **Type hints everywhere**, under `from __future__ import annotations`.
- **Docstrings explain *why*, not *what*.** The spec covers *what*.
- `parley.ledger.compute` must stay **pure** — no I/O, no clock beyond an injected `computed_at`.

---

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The standing target: **every normative MUST in the spec has at least one test.** If you add a MUST
to the spec, add the test in the same pull request.

`tests/test_conformance.py` is special: it can be pointed at *any* Hub implementation, not just
this one. Keep it free of references to our internals — it talks HTTP and nothing else. It is the
acceptance test an independent implementation runs, which makes it the most important file in the
suite.

Before opening a pull request:

```sh
python3 -m unittest discover -s tests          # green
python3 -m parley doctor                       # green in a real workspace
bash -n scripts/*.sh                           # if you touched the shell scripts
python3 -m py_compile scripts/bootstrap.py examples/generic-agent/agent.py
```

If your change touches the wire format, the Deck or the client/Hub boundary, also run a real
two-participant session on one machine (two workspaces, two terminals) and say in the PR that you
did.

---

## Documentation

Documentation is part of the product, not an afterthought — the whole project rests on an agent
being able to read [`AGENTS.md`](AGENTS.md) and participate correctly with no other context.

- Changing a command, a flag, an endpoint or an event body means updating `AGENTS.md`,
  `docs/PROTOCOL.md` and the relevant reference doc in the same pull request.
- **Every command written in the docs must be one a reader can actually paste.** Not pseudo-code,
  not a sketch. If you cannot run it, do not write it.
- Do not document behaviour that does not exist yet. A documented flag that nobody implemented is
  worse than an undocumented one that works.
- House style: tables and short paragraphs over walls of text. No marketing voice. No emoji in
  headings. Honest limitations stated plainly — a credible document with known limits is worth
  more than a breathless one.

---

## Security issues

Do not open a public issue for a vulnerability. See [`docs/SECURITY.md`](docs/SECURITY.md) for the
threat model and the reporting route.

Two things that are *not* vulnerabilities, because they are documented design decisions:

- The Hub can impersonate any agent. It mints the per-agent keys. This is stated in the threat
  model.
- Anyone holding the watchword can enrol. That is what the watchword is for. `--approve`,
  `enroll_ttl_s` and `enroll_max_uses` are the controls.

---

## Pull requests

- One concern per pull request.
- Say which spec sections your change touches.
- If it changes the wire format, it changes the version string, and that is a large conversation —
  raise it as an issue first.
- New event types belong in the `x.*` extension namespace until they are specified. Implementations
  must accept, store, relay and gracefully ignore unknown `x.*` types already, so this costs
  nothing.
