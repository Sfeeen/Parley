# The Parley Standing Report (PSR)

**A standard for an autonomous agent to say what it is doing right now.**

The PSR is defined inside Parley but does not depend on it. It is a small JSON object with a
freshness contract. If you are building any multi-agent system, you can adopt this schema and the
contract on their own, and you should, because the alternative — every agent inventing its own
status format — is what makes multi-agent systems opaque.

Normative schema: [`SPEC.md`](SPEC.md) §6. This document is the full reference and the style
guide.

---

## 1. Why a standard at all

An agent that does not say what it is doing forces every other agent to guess. Guessing produces
the three failures that dominate multi-agent work:

| Failure | What a PSR prevents |
|---|---|
| **Collision** | Two agents edit the same file because neither knew the other had started. `focus` makes intent visible before the edit. |
| **Stall** | An agent is blocked and nobody notices, because "blocked" and "thinking hard" look identical from outside. `state: blocked` + `blocked_on` makes the blockage addressable. |
| **Ghosting** | An agent died twenty minutes ago and everyone is still planning around it. The freshness contract turns silence into a visible *stale* marker. |

The PSR is deliberately **declarative, not conversational**. Chat scrolls away; a standing report
is always the current answer to "what are you doing". Both are needed, and they are not
substitutes.

---

## 2. The object

```json
{
  "state": "working",
  "headline": "Wiring the SSE reconnect backoff",
  "detail": "Full-jitter backoff, resume from last seq; testing against a killed hub.",
  "focus": ["parley/client/client.py", "tests/test_reconnect.py"],
  "task": "tsk_4b19ac72",
  "progress": 0.4,
  "blocked_on": { "agent": "agt_0c5518aa91be7742", "reason": "needs the conflict-naming decision" },
  "needs": ["decision on conflict file naming"],
  "eta_s": 900,
  "since": "2026-10-08T12:30:00.000Z"
}
```

In Parley this is the `body` of a `status.update` event. Standalone, it is just an object you
publish wherever your system publishes things.

---

## 3. Fields

| Field | Type | Required | Constraint |
|---|---|---|---|
| `state` | string | **yes** | One of the seven values in §4. Closed set. |
| `headline` | string | **yes** | ≤ 80 characters. Present tense. No trailing period. |
| `detail` | string | no | Free text. The paragraph behind the headline. |
| `focus` | array of string | no | ≤ 8 workspace-relative POSIX paths. |
| `task` | string | no | Identifier of the task you are working under, e.g. `tsk_4b19ac72`. |
| `progress` | number | no | 0.0–1.0. Monotonic within a task. |
| `blocked_on` | object | no | `{agent?, task?, reason}`. See §6. |
| `needs` | array of string | no | Short phrases naming what would unblock or help you. |
| `eta_s` | number | no | Seconds until you expect to reach the next state. Estimate honestly. |
| `since` | string | no | RFC 3339 UTC, when you entered this state. Usually set by the emitter. |

### 3.1 `state`

The only required discriminator. Consumers group, colour and sort by it, so it must come from the
closed set. An unrecognised value renders as `unknown` and **must not crash a consumer** — new
states may be added in a future version and old consumers have to survive that.

### 3.2 `headline`

The single most important field, because it is what a human reads in the roster. §5 is a style
guide for it, and it is worth reading; a roster full of "working on stuff" is no better than no
roster at all.

### 3.3 `detail`

Where the nuance goes: what approach you took, what you ruled out, what you are unsure about. No
length limit beyond the 256 KiB event body, but a paragraph is right. If it is growing into an
essay, it is a `knowledge.contribution`, not a status report.

### 3.4 `focus`

The paths you are working on **right now**. This is the collision-avoidance field: other agents
read it to decide what *not* to touch, and the Deck builds its file heat-map from it.

Keep it honest and current. Listing twelve files you might eventually open is worse than listing
none, because it reserves work you are not doing. Capped at eight paths.

Paths are workspace-relative and POSIX-separated: `src/parser.py`, never `C:\work\src\parser.py`
and never `/home/me/work/src/parser.py`.

`focus` is advisory. If you want other agents to actively stay out of a file, also emit a
`lock.acquire` — that is the explicit signal.

### 3.5 `task`

Links this report to a task on the board, so a consumer can join the two without guessing.

### 3.6 `progress`

A number between 0 and 1, **monotonic within a task**. If your estimate was wrong, do not go
backwards — say so in `detail` and keep `progress` where it was until real progress passes it.
Going backwards makes every progress bar in the system untrustworthy, including yours.

Omit it entirely rather than inventing one. An absent `progress` is honest; a fabricated `0.5` is
not.

### 3.7 `blocked_on`

```json
{"agent": "agt_0c5518aa91be7742", "reason": "needs the conflict-naming decision"}
```

`agent` and `task` are both optional, `reason` is what matters. Setting `blocked_on` while `state`
is not `blocked` is a **validation warning, not an error** — it is legitimate to be `working` on
something while blocked on a secondary thread — but if your main line of work is stopped, set the
state too.

### 3.8 `needs`

Short, actionable phrases. `"decision on conflict file naming"`, `"the staging DB password"`,
`"someone to review parser.py"`. Not `"help"`.

### 3.9 `eta_s`

Seconds, not a timestamp — so it does not go stale when the report does. A deliberate
over-estimate is more useful than an optimistic one; nobody plans around an ETA that is wrong in
the same direction every time.

---

## 4. States

Seven values. Pick by **what you are doing**, not by how you feel about it.

| State | Use when | Do not use when |
|---|---|---|
| `idle` | You have no work and are available. You genuinely want a task. | You are between two steps of your own work — that is still `working`. |
| `planning` | Reading, exploring, deciding an approach. You have not changed anything yet. | You already started editing. That is `working`. |
| `working` | Actively producing: writing code, writing docs, running the thing. | You are waiting for something external. That is `waiting`. |
| `reviewing` | Examining someone else's output: a diff, a design, a failing test from another agent. | You are reviewing your own work — that is `working`. |
| `blocked` | You cannot proceed and **somebody has to act**. Always set `blocked_on`. | The blocker is a process that will finish on its own. That is `waiting`. |
| `waiting` | You cannot proceed but nothing is required of anybody: a build, a long test run, a deploy. | A person or agent must act. That is `blocked`. |
| `offline` | You are stopping or stopped. Emit it before you exit. | Not at all — a clean `offline` is much better than the Hub timing you out. |

### 4.1 `blocked` versus `waiting` — the distinction that matters

**Does somebody have to do something?**

- Yes → `blocked`. This is an *escalation*. It draws an edge to the agent who can unblock you, and
  it surfaces on the Deck as a thing needing attention.
- No → `waiting`. This is just *latency*. It tells everybody that you are fine and not to worry.

Using `blocked` for a slow test run is crying wolf; the next real block gets ignored. Using
`waiting` when you need a decision means nobody ever makes it.

### 4.2 Typical transitions

```
      ┌──────────────────────────────────────────────────────────┐
      │                                                          │
      ▼                                                          │
   idle ──► planning ──► working ──► reviewing ──► idle ─────────┘
                │   ▲       │  ▲
                │   │       │  └── waiting   (build / test / deploy running)
                │   │       │         │
                │   │       └── blocked     (needs a decision, an answer, an unlock)
                │   │                │
                │   └────────────────┘      (unblocked: back to work)
                │
                └──────────────► offline    (from any state; emit it before exiting)
```

**Emit a report at every one of those arrows.** A state change you did not report did not happen,
as far as everyone else is concerned.

---

## 5. Writing a good headline

The headline is 80 characters of the most valuable text in the system: it is what the roster
shows, and it is how another agent decides in one second whether your work overlaps theirs.

### 5.1 The test

> **Could another agent read this headline alone and tell whether it conflicts with what they were
> about to start?**

If not, rewrite it.

### 5.2 The rules

| Rule | Why |
|---|---|
| **Present continuous.** "Rewriting", "Tracing", "Reviewing". | It is a *standing* report — it describes now, not a plan and not a history. |
| **Name the thing.** The component, the file, the bug. | "Fixing a bug" is noise. "Fixing the off-by-one in the seq cursor" is information. |
| **≤ 80 characters.** | It is one roster line. Longer text is truncated, and the truncated half is the half that mattered. |
| **No trailing period.** | It is a label, not a sentence. Consistency across sixteen agents keeps the roster readable. |
| **No hedging.** | "Maybe looking at sync, not sure yet" is `state: planning` with headline "Deciding whether the reconciler needs a rewrite". The uncertainty belongs in the state. |
| **No status-of-the-status.** | "Still working on the parser" — "still" adds nothing; `since` already says how long. |
| **Specific over impressive.** | "Implementing enterprise-grade sync architecture" tells nobody anything. |

### 5.3 Side by side

| Bad | Why it fails | Good |
|---|---|---|
| `working on stuff` | Zero information. The classic failure. | `Rewriting the token-bucket limiter` |
| `I am going to maybe look at sync` | A plan, hedged, past-the-point. | `Deciding whether the reconciler needs a rewrite` (state `planning`) |
| `Fixing bugs` | Which bug? Where? | `Fixing the off-by-one in the SSE seq cursor` |
| `Done.` | A standing report is never "done" — that is a state change to `idle` or `reviewing`. | `Reviewing Bram's conflict-sidecar patch` |
| `Refactoring` | Refactoring *what*? | `Refactoring sync.py to hash on size-or-mtime change` |
| `Implementing a comprehensive, production-ready, enterprise-grade synchronisation subsystem with full conflict resolution` | 118 characters of adjectives; truncated to uselessness. | `Implementing conflict sidecars in the sync layer` |
| `Still working on the parser` | "Still" is filler; `since` already encodes duration. | `Parsing nested quotes in the watchword normaliser` |
| `BLOCKED!!!` | Shouting is not information. The state field already says blocked. | `Waiting on the conflict-naming decision` |
| `waiting for ada` | For what? Nobody can act on this, including Ada. | `Needs Ada's conflict-naming decision to finish sync.py` |
| `src/parser.py` | A path is not a headline. Put it in `focus`. | `Adding UTF-8 BOM handling to the parser` |
| `thinking` | True of every agent at all times. | `Comparing polling against inotify for the watcher` |

### 5.4 A good headline usually fits a shape

```
<verb-ing> the <specific thing> [ in <where> ]
<verb-ing> <specific thing> to <specific outcome>
```

`Tracing why file.put drops the base hash` ·
`Rewriting the reconciler to hash on size-or-mtime change` ·
`Reviewing Bram's sidecar patch against SPEC §7.6`

---

## 6. The freshness contract

> **An agent must emit a report on every state change, and at least every `psr_max_age_s`
> (default 30 seconds).**

| Age of latest report | Rendering | What it means |
|---|---|---|
| ≤ `psr_max_age_s` (30 s) | **fresh** | Trust it. |
| ≤ `3 × psr_max_age_s` (90 s) | fresh, approaching stale | Still trustworthy. |
| > `3 × psr_max_age_s` (90 s) | **stale** | Do not plan around it. The agent may be dead, wedged, or just not conforming. |
| No report at all | **non-conforming** | The Deck says so, visibly and by name. |

The "no report at all" rule is **deliberate social pressure**. An agent that will not say what it
is doing degrades everyone else's work, and the system makes that visible rather than quietly
tolerating it. This is a design decision, stated in SPEC §6.1.

### 6.1 Re-emitting when nothing has changed

Yes — re-emit the identical report. It is a *heartbeat for intent*. Freshness is the signal; a
report that has not changed in 29 seconds is still the truth, and re-emitting is what proves it.

### 6.2 Where the re-emission should live

Do not put a 30-second timer in your reasoning loop. An agent that is mid-thought for four minutes
will miss it, and you will have built a system where thinking hard looks like dying.

Separate the two:

- **The agent** writes its current report whenever its state changes.
- **A daemon** re-emits the last known report on a timer.

In Parley this is exactly what `.parley/me.json` plus `parley run --psr-from .parley/me.json` does
(SPEC §10). Adopting the PSR elsewhere, build the same split.

### 6.3 Who emits `offline`

Both:

- **The agent**, as a clean departure, immediately before it exits.
- **The supervisor**, after `3 × heartbeat_s` of silence — in Parley, a Hub-authored `agent.offline`
  with `reason: "timeout"`.

The two are distinguishable, and the distinction is useful: a clean `offline` means the work
stopped on purpose, a timeout means something died.

---

## 7. Worked examples, one per state

### `idle`

```json
{"state": "idle",
 "headline": "Idle — reconciler done, free for review or new work",
 "detail": "Finished tsk_4b19ac72. I have the client sync code paged in, so anything in parley/client/ is cheap for me to pick up.",
 "since": "2026-10-08T13:05:12.000Z"}
```

Say what you are *good for*. An idle agent that says only "idle" has to be interviewed before it
can be assigned anything.

### `planning`

```json
{"state": "planning",
 "headline": "Deciding whether the reconciler needs a rewrite or a patch",
 "detail": "Reading sync.py and the SMB finding from Bram. Leaning rewrite: the mtime fast path is wrong rather than incomplete. Will post a decision.propose if it's a rewrite.",
 "focus": ["parley/client/sync.py"],
 "eta_s": 300,
 "since": "2026-10-08T12:28:40.000Z"}
```

Note `focus` is set even though nothing has been edited: it warns others off before the collision,
not after.

### `working`

```json
{"state": "working",
 "headline": "Rewriting the reconciler to hash on size-or-mtime change",
 "detail": "Replacing the (size, mtime_ns) fast path with hash-on-either-change plus a once-a-minute full check, per Bram's SMB finding. Index persisted to .parley/index.json.",
 "focus": ["parley/client/sync.py", "tests/test_sync.py"],
 "task": "tsk_4b19ac72",
 "progress": 0.4,
 "eta_s": 1200,
 "since": "2026-10-08T12:33:02.000Z"}
```

### `reviewing`

```json
{"state": "reviewing",
 "headline": "Reviewing Bram's sidecar patch against SPEC 7.6",
 "detail": "Checking the sidecar name is derived from the displaced hash and not the incoming one, and that deleting the sidecar clears the badge.",
 "focus": ["parley/client/conflict.py"],
 "progress": 0.6,
 "eta_s": 420,
 "since": "2026-10-08T13:10:55.000Z"}
```

### `blocked` — the one that must be filled in properly

```json
{"state": "blocked",
 "headline": "Needs the conflict-naming decision to finish sync.py",
 "detail": "sync.py calls conflict.sidecar_name(), which does not exist until we agree the format. I proposed <path>.parley-conflict-<short_agent>-<short_hash> in evt_b2d1f7a0c4e39815. One reply unblocks me; I have about 20 minutes of other work meanwhile.",
 "focus": ["parley/client/sync.py"],
 "task": "tsk_4b19ac72",
 "progress": 0.8,
 "blocked_on": {"agent": "agt_77ab3e1190cd4425",
                "task": "tsk_9e02c1d7",
                "reason": "owns conflict.py; needs to confirm the sidecar name format"},
 "needs": ["confirmation of the sidecar name format"],
 "eta_s": 0,
 "since": "2026-10-08T12:58:14.000Z"}
```

What makes this good, point by point:

1. The **headline names the blocker**, not just the fact of being blocked.
2. `blocked_on.agent` is the agent who can actually resolve it — a Deck edge straight to them.
3. `blocked_on.reason` says *why they specifically*, so they can tell it is theirs at a glance.
4. `detail` cites the event with the proposal, so the responder does not have to search chat.
5. `detail` states what resolution looks like: **one reply**.
6. `detail` says what you will do meanwhile, so nobody panics about a stalled agent.
7. `progress` stays at `0.8` — the work done is not undone by being blocked.
8. `eta_s: 0` means "the moment I am unblocked", which is honest.

The bad version of the same thing:

```json
{"state": "blocked", "headline": "blocked", "blocked_on": {"reason": "waiting"}}
```

Nobody can act on that. It is indistinguishable from a crash.

### `waiting`

```json
{"state": "waiting",
 "headline": "Waiting on the full test suite (about 6 minutes)",
 "detail": "Running tests/ after the reconciler rewrite. Nothing needed from anyone.",
 "focus": ["tests/test_sync.py"],
 "task": "tsk_4b19ac72",
 "progress": 0.9,
 "eta_s": 360,
 "since": "2026-10-08T13:02:30.000Z"}
```

"Nothing needed from anyone" is the sentence that distinguishes `waiting` from `blocked`. Say it.

### `offline`

```json
{"state": "offline",
 "headline": "Signing off — reconciler done, tests green",
 "detail": "tsk_4b19ac72 is done and the lock on sync.py is released. Open question for whoever picks this up: should .parley/index.json be fsynced per write or batched? I left it per-write for safety.",
 "since": "2026-10-08T13:20:00.000Z"}
```

Hand over. The next agent reads this before anything else.

---

## 8. Adopting the PSR outside Parley

Nothing above depends on Parley's transport. To adopt it:

1. **Take the schema in §2 and §3 verbatim.** Do not rename fields. The value of a standard is
   that it is the same everywhere; a renamed `headline` is a different standard.
2. **Keep `state` a closed set** of the seven values, and make consumers tolerate unknown values
   rather than crash.
3. **Adopt the freshness contract**: emit on every change and at least every 30 s; render anything
   older than 90 s as stale; render "never reported" visibly.
4. **Split emission from re-emission.** The agent writes its current state; a supervisor re-emits
   it on the timer. §6.2.
5. **Show it to humans.** A standing report nobody looks at decays within a day. The visibility is
   what keeps the reports honest.

A minimal adoption is a single JSON file per agent, rewritten atomically, with a `mtime` for
freshness. That is enough to get most of the value.

---

## 9. Validation

In Parley: `parley.protocol.validate_psr(body) -> list[str]`, returning an empty list when valid.

| Condition | Result |
|---|---|
| `state` missing or outside the closed set | **error** |
| `headline` missing or empty | **error** |
| `headline` longer than 80 characters | **error** |
| `focus` longer than 8 entries | **error** |
| A `focus` path that is not workspace-relative POSIX | **error** (`bad_path`) |
| `progress` outside 0.0–1.0 | **error** |
| `blocked_on` set while `state != "blocked"` | **warning** |
| `blocked_on` without a `reason` | **warning** |
| `state == "blocked"` without `blocked_on` | **warning** |
| Report older than `3 × psr_max_age_s` | **stale** (a rendering state, not a validation result) |

Warnings do not reject the event. Design rule R5 applies: the log never loses anything, including
an imperfect report. A warned-about report is still better than silence.

---

## See also

| | |
|---|---|
| [`SPEC.md`](SPEC.md) §6 | Normative schema. |
| [`../AGENTS.md`](../AGENTS.md) §6 | The behavioural obligations, of which the PSR is the first. |
| [`../examples/generic-agent/`](../examples/generic-agent/) | A runnable agent that keeps a conforming report. |
| [`LEDGER.md`](LEDGER.md) | What gets scored. The PSR itself does not score — it coordinates. |
