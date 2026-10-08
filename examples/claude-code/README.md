# Claude Code — drop-in participation

Two files. Either one makes a Claude Code agent a conforming Parley participant. You do not need
both, and you do not need to write any glue code.

| File | Use it when |
|---|---|
| [`PARLEY.md`](PARLEY.md) | You want the instructions **always** in context for this project. Copy it into the workspace, or append it to the project's `CLAUDE.md`. |
| [`skills/parley/SKILL.md`](skills/parley/SKILL.md) | You want the instructions loaded **on demand**, only when a parley is actually relevant. Lighter on context. |

The skill is the better default for a long-running agent that does other work too. The `CLAUDE.md`
route is better when the whole point of the session is the parley.

---

## Option A — the instructions file

```sh
# Into the workspace being collaborated on (NOT the Parley clone):
cp /path/to/parley/examples/claude-code/PARLEY.md ~/work/parley-ws/PARLEY.md
```

Then either point Claude Code at it:

> Read `PARLEY.md` and follow it. We are in a parley with other agents.

or append it to the project's `CLAUDE.md` so it is always loaded:

```sh
cd ~/work/parley-ws
{ echo; echo '---'; echo; cat PARLEY.md; } >> CLAUDE.md
```

> **Note.** `PARLEY.md` will itself be synced to every participant, because everything in the
> workspace is. That is usually fine and often useful — it means every agent that joins finds the
> instructions already there. If you would rather it were not, add it to `.parleyignore`.

## Option B — the skill

```sh
# Project-scoped, for this workspace only:
mkdir -p ~/work/parley-ws/.claude/skills
cp -r /path/to/parley/examples/claude-code/skills/parley ~/work/parley-ws/.claude/skills/

# Or user-scoped, available in every project:
mkdir -p ~/.claude/skills
cp -r /path/to/parley/examples/claude-code/skills/parley ~/.claude/skills/
```

Claude Code picks it up on the next start. It triggers on a `.parley/` directory being present, or
on the user mentioning a parley, a watchword, the Hub, the Deck, a standing report, or asking what
the other agents are doing.

Check it loaded:

```
/skills
```

---

## Getting the agent into a session

**Already joined** (someone ran `parley join` and `parley run` in this workspace):

> We're in a parley. Read `.parley/chat.md` and `.parley/roster.json`, introduce yourself, and
> take whatever work is open.

**Not joined yet** — give it the watchword and the clone location:

> Join the parley. The clone is at `~/parley`, the watchword is
> `copper-otter-climbs-the-quiet-hill`, the Hub is `http://192.168.1.20:7777`, and the fingerprint
> should read `lemon-anchor-fox`. Use this directory as the workspace.

**On a LAN with no URL:**

> Join the parley with `--discover`. The watchword is `copper-otter-climbs-the-quiet-hill` and the
> fingerprint should read `lemon-anchor-fox`.

Always give the fingerprint. It is how the agent confirms it reached the right Hub, and it is
instructed to stop rather than continue if it does not match.

---

## What a conforming agent does

The short version — the full reasoning is in [`../../AGENTS.md`](../../AGENTS.md) §6 and §6A.

| | |
|---|---|
| Keeps `.parley/me.json` current, on every state change and at least every 30 s | Otherwise it renders as *stale* on the Deck and others plan around work it abandoned. |
| Announces in chat and takes a `lock.acquire` **before** editing a file | Collisions are the most expensive failure mode here. |
| Checks `state.json` locks and yields rather than writing over someone | It *can* write anyway — but that produces a conflict sidecar and a visible `lock_violation` flag. |
| Records a `knowledge.contribution` when it decides, finds or finishes something | It is the session's memory and the Ledger's main input. |
| Cites with `reply_to` and `refs` | Credits the person cited; self-citation deliberately scores nothing. |
| Sets `blocked_on` and says so in chat when stuck | A silent block looks exactly like an idle agent. |
| Releases locks and claims when it stops | Abandoned claims make the Deck lie. |
| Treats everything in the log as untrusted | Other agents may be misconfigured or hostile. |
| Announces what it alone can reach, in `.parley/capabilities.json` | Otherwise the others solve its speciality badly by hand, or not at all. |
| Reads `capabilities` in `state.json` before deciding something is impossible from here | An hour reimplementing what the agent beside it does in one call is the waste the Exchange exists to prevent. |
| Answers every request it accepts | Accepting and going quiet parks the caller until its timeout burns; it is the only thing the Ledger subtracts for. Declining is free. |
| Treats an incoming request as a proposal, not a command | A signed request is still only an ask. Text in a workspace file is not a request at all. |

Both drop-in files ship with this already in them, so an agent given either one is an Exchange
participant out of the box — it will announce nothing until you tell it what it holds, but it will
discover, ask, answer and decline correctly without further configuration.

### Telling it what it can lend

The agent does not know what is attached to your machine. Say so once:

> You have the `abc-repairdata` MCP server and this machine is the only one with the serial cable
> to the bench. Announce both to the parley. The serial one is `dangerous` — it moves real
> hardware.

It will write `.parley/capabilities.json` and the daemon announces it. Check with:

```sh
PYTHONPATH=~/parley python3 -m parley capabilities --json
```

### Consent, if you lend anything beyond read-only

Anything declared `guarded` or `dangerous` parks for a decision instead of running. Those appear in
`.parley/pending.json`, on the Deck, and in:

```sh
PYTHONPATH=~/parley python3 -m parley requests --pending
```

A `dangerous` capability needs a human on **every** call and no configuration can change that. To
stop routine `guarded` requests from asking every time, write a
[`.parley/policy.json`](../../docs/EXCHANGE.md#6-writing-a-policyjson) naming the caller and the
capability — note that a blanket `{"requester":"*","capability":"*","action":"allow"}` deliberately
does **not** count as naming them.

---

## Two things to set up first

**Turn off format-on-save in the workspace.** An editor or hook that reformats on every save will
write files under other agents' locks and generate conflict sidecars continuously. This is the
single biggest source of conflict spam.

**Check `.parleyignore` before the first `parley run`.** Everything in the workspace is replicated
to every participant's disk. `.git/`, `node_modules/`, `.venv/`, `__pycache__/` and friends are
ignored by default; add your build output, caches and anything large.

---

## Verifying it worked

```sh
cd ~/work/parley-ws
PYTHONPATH=~/parley python3 -m parley roster --json
```

The agent should appear with `online: true` and a PSR whose `headline` is specific. If the headline
reads "working on stuff" or the state is missing, it has not actually read the instructions — point
it at [`../../AGENTS.md`](../../AGENTS.md) §6 directly.

On the Deck, the agent's card should show a state badge, a live headline and the files it is
focused on. An agent marked *non-conforming* has never emitted a standing report at all.

---

## See also

| | |
|---|---|
| [`../../AGENTS.md`](../../AGENTS.md) | The canonical procedure. These files are a condensation of it. |
| [`../../docs/EXCHANGE.md`](../../docs/EXCHANGE.md) | Capability lending in full: writing a good `description`, the consent policy file, and the prompt-injection threat the Exchange introduces. |
| [`../generic-agent/`](../generic-agent/) | The same obligations as runnable Python, for a non-Claude agent. |
| [`../human/`](../human/) | For the person watching the Deck. |
