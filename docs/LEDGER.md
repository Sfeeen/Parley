# The Ledger

**What it is:** a count of recorded contribution per agent, computed from the event log by a fixed,
published, user-overridable set of weights.

**What it is not:** a measure of quality, of effort, or of anybody's worth. It counts events. An
agent that does excellent work and records none of it scores nothing, and that is a known and
accepted property, not a bug.

Normative weights: [`SPEC.md`](SPEC.md) §9. Implementation: `parley/ledger.py`.

---

## 1. Why it exists

In a session with six agents, "who contributed what" is otherwise unanswerable without reading
several thousand events. The Ledger answers it in a bar chart, and — this is the part that matters
— lets you click any bar and see the exact events behind every point.

Design rule **R6** governs it: *explainable, not magic*. No learned weights, no model, no hidden
heuristics. Every point traces to an event you can be shown.

---

## 2. The six components

| Component | Derived from | Rewards |
|---|---|---|
| **Contributions** | `knowledge.contribution` events, weighted by `kind` | Recording what you decided, found or concluded. |
| **Authored substance** | Lines in the *current* version of each text file last written by that agent (blame-lite, capped per file) | Material that survived in the workspace. |
| **Delivery** | `task.done` events on tasks the agent claimed | Finishing things. |
| **Influence** | Times *another* agent's `refs` cited one of this agent's events | Being useful to somebody else. |
| **Service** | Successful `request.result`s the agent **provided** to others through the Exchange (SPEC §15), minus a penalty for requests it accepted and never answered | Doing work for another agent, and being reliable about it. |
| **Presence** | Chat messages, hard-capped | Showing up and talking — a little. |

### 2.1 Default weights

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
  "service_points": 3.0,
  "service_priority_bonus": 0.5,
  "service_cap_per_requester": 20.0,
  "abandoned_request_penalty": -5.0,
  "decay_half_life_days": 0
}
```

Override them per workspace in `<workspace>/.parley/ledger.json`. Anything you omit keeps its
default.

### 2.2 What the numbers imply

The weights encode a position, and it is worth being explicit about it:

- **A decision is worth 160 chat messages** (`8` versus `0.05`). Deciding something that constrains
  other people's work is the highest-value act in a collaboration.
- **A finding is worth 2.5 code contributions** (`5` versus `2`). Discovering *why* something
  behaves as it does — including a dead end that saves somebody a day — is weighted above
  producing more of it.
- **400 surviving lines in one file is worth one decision** (`400 × 0.02 = 8`). Volume counts, but
  it is capped so that generating a large file cannot outweigh thinking.
- **Chat caps out at 10 points**, which is reached after 200 messages. After that, chat is worth
  exactly zero.
- **A fulfilled request is worth one and a half code contributions** (`3.0` versus `2.0`). Doing a
  piece of work for another agent is weighted above producing more of your own.
- **`decay_half_life_days: 0` means no decay.** Contributions do not expire. Set it to a positive
  number if you want a long-running parley to weight recent work more heavily.

### 2.3 Service, and the one negative term

The **service** component is the Exchange's half of the Ledger (SPEC §15.5). It exists because
lending a capability is real contribution: an agent that spends its afternoon running searches,
flashing boards or answering database questions for the others has done work, and without this
component none of it would show up anywhere.

#### What earns

`service_points` (default `3.0`) per successful `request.result` the agent **provided**, scaled by
the requester's declared `priority`:

```
award = service_points + service_priority_bonus × (priority − 3)
```

Additive, not multiplicative, and floored at zero — a priority-1 errand is worth less than a
priority-5 one (`2.5` against `4.0`) but never worth *nothing*, which a multiplier would make it at
the bottom of the scale.

Four conditions, all of which must hold:

| Condition | Why |
|---|---|
| The agent is the one the request was addressed to, or the one that accepted an open `to: "any"` offer | Emitting a result for somebody else's request buys nothing. |
| The requester is not the provider | Serving yourself scores nothing, exactly as self-citation does. |
| The request id has not already been paid | A provider that re-sends its result is idempotent, not twice as useful. |
| `ok: true` | See below. |

**A failed result earns nothing and costs nothing.** `{"ok": false}` is still an answer. This is
deliberate and it is the important half: a provider must never be better off staying silent than
admitting a failure. Declining costs nothing either.

#### The per-requester-pair cap — the anti-farming measure

Service credit is capped at `service_cap_per_requester` (default `20.0`) **per (provider,
requester) pair**. Past that point, further work for that same caller earns exactly zero.

That is what makes mutual farming pointless. Two agents trading trivial requests back and forth hit
the cap after roughly seven exchanges each and then earn nothing more from each other, no matter
how many requests they send. To keep earning service points an agent has to be useful to *different*
agents — which is the behaviour the component is there to encourage.

The capped result still gets an evidence line saying it was capped. A point that was *not* awarded
is as much part of the explanation as one that was, and R6 applies to both.

#### The penalty — the only negative term in the Ledger

`abandoned_request_penalty` (default `−5.0`) is charged for every request an agent **accepted and
then never answered**. Nothing else in the Ledger subtracts.

It exists because reliability is the thing the Exchange depends on. Every other failure mode in a
parley is recoverable by someone noticing: a stale PSR is visible, an unreleased lock expires, an
unrecorded finding is merely invisible. An accepted request that is never answered is different —
the caller is parked in `state: "waiting"` with `blocked_on` pointing at the provider, doing
nothing, until its timeout burns. It cannot tell silence from a crash, so it cannot even go
elsewhere. One agent's silence stops another agent's work.

Note the shape of the incentive, which is the whole point:

| The provider does | It costs |
|---|---|
| Declines immediately | nothing |
| Accepts, tries, fails, says so (`ok: false`) | nothing |
| Accepts, succeeds | nothing — earns `+3.0`-ish |
| Accepts and goes silent | **−5.0** |

There is no situation in which staying silent is the cheapest option. That is by design.

The charge is raised strictly off the Hub's `request.expired` event with `abandoned: true` — never
from elapsed time guessed at scoring time. A request still legitimately in flight when `compute`
runs has not been abandoned, and penalising an agent for being slow would be a different and much
worse rule. Abandoning your own request harms nobody but yourself and is not charged.

Like every other line, the penalty appears in `evidence` with its `seq` and a label naming the
request and who was left waiting:

```
service  seq 902   −5.0   accepted request req_7c2a91f4 from 77ab3e (bench power-cycle) and never answered it
```

A penalty the user cannot trace would violate R6 just as much as a point they cannot trace.

---

## 3. Influence, and why self-citation does not count

Every `chat.message` and `knowledge.contribution` may carry `refs`:

```json
{"refs": [{"kind": "event", "value": "evt_b2d1f7a0c4e39815"},
          {"kind": "file",  "value": "parley/client/sync.py"}]}
```

When agent A cites an event authored by agent B, **B** earns `citation_received_points`. A earns
nothing for making the citation.

**Citing your own event scores nothing.** This is the central anti-gaming property. The only way
to earn influence is for somebody else to find your work worth pointing at, and you cannot
manufacture that yourself.

It also happens to be the right incentive: it rewards being *useful to others*, which is the thing
a collaboration actually needs, rather than being prolific.

---

## 4. Worked example

A short session. Ada hosts, Bram joins.

| Event | Agent | Component | Points |
|---|---|---|---|
| `knowledge.contribution` kind `decision` — "SSE beats WebSockets here" | Ada | Contributions | `8.0` |
| `knowledge.contribution` kind `finding` — "mtime_ns unreliable on SMB" | Bram | Contributions | `5.0` |
| `knowledge.contribution` kind `code` — "Reconciler hashes on either change" | Ada | Contributions | `2.0` |
| `knowledge.contribution` kind `fix` — "Merged sidecar call into sync.py" | Ada | Contributions | `3.0` |
| Ada cites Bram's finding in a `chat.message` | Bram | Influence | `0.5` |
| Ada cites Bram's finding again in her `code` contribution | Bram | Influence | `0.5` |
| `task.done` tsk_4b19ac72 (claimed by Ada) | Ada | Delivery | `2.0` |
| `task.done` tsk_9e02c1d7 (claimed by Bram) | Bram | Delivery | `2.0` |
| `sync.py` — 310 surviving lines last written by Ada | Ada | Authored | `6.2` |
| `conflict.py` — 180 surviving lines last written by Bram | Bram | Authored | `3.6` |
| `request.result` — Ada ran `pytest.run` for Bram (priority 3) | Ada | Service | `3.0` |
| `request.result` — Ada ran it again for Bram after the fix (priority 4) | Ada | Service | `3.5` |
| `request.result{ok:false}` — Ada could not reach the Z: share for Bram | Ada | Service | `0.0` |
| 34 chat messages | Ada | Presence | `1.7` |
| 41 chat messages | Bram | Presence | `2.05` |

| Agent | Contributions | Authored | Delivery | Influence | Service | Presence | **Total** | **Share** |
|---|---|---|---|---|---|---|---|---|
| Ada | 13.0 | 6.2 | 2.0 | 0.0 | 6.5 | 1.70 | **29.40** | 68.3 % |
| Bram | 5.0 | 3.6 | 2.0 | 1.0 | 0.0 | 2.05 | **13.65** | 31.7 % |

Three things in that table are worth reading carefully.

The failed result scores `0.0` and is still listed. It cost Ada nothing and earned nothing, which
is exactly the intended price of admitting a failure.

Ada's two successful fulfilments total `6.5` against the `20.0` cap for the Ada→Bram pair. Thirteen
and a half points of further errands for Bram would still score; the fourteenth would not.

And Bram's SMB finding changed Ada's entire design — it is the single most consequential event in
the session — and it scores `5.0 + 1.0` while Ada's larger output and willingness to run errands
score more. The Ledger is not claiming Ada contributed more *value*. It is reporting that Ada
recorded more *events*. Those are different claims and only the second one is being made.

---

## 5. Seeing the breakdown

```sh
parley ledger --why agt_77ab3e1190cd4425
```

prints the per-component breakdown with the contributing event ids. On the Deck, click any bar in
the Ledger panel for the same thing.

```sh
parley ledger --json
```

returns `LedgerResult.to_dict()`: one line per agent with `total`, `share`, `components` and
`evidence`, where `evidence` maps each component to the list of `{seq, label, points}` behind it.

If a number cannot be explained by pointing at events, that is a bug in the implementation, not a
feature of the scoring. Report it.

---

## 6. Why it cannot usefully be gamed

| Attempt | Why it fails |
|---|---|
| Flood chat | `0.05` each, capped at `10.0` total. The cap arrives after 200 messages, then chat is worth zero. Meanwhile the spam is in the log with your name on it. |
| Cite yourself constantly | Self-citation scores nothing, by rule. |
| Generate enormous files | `surviving_lines_cap_per_file` is 400 lines (8 points). Beyond that, length is free of charge. |
| Record trivia as `decision` | It scores — and it is visible. Every contribution is in the log with its title and detail, in front of the other agents and the human watching the Deck. A roster of eight-point "decisions" reading "renamed a variable" is self-documenting. |
| Churn files to re-author lines | Only the *current* version counts, and only the last writer. Rewriting someone's file transfers the lines; rewriting your own gains nothing. |
| Claim and complete trivial tasks | `2.0` each. A decision is worth four of them. |
| Farm service points with a partner | Capped at `20.0` per (provider, requester) pair — about seven exchanges — after which that caller is worth zero no matter how many requests arrive. Serving yourself is worth nothing at all. The whole exchange is in the log with both names on it. |
| Accept everything to look useful | Accepting is a promise. Each one you do not answer is `−5.0`, the only negative term in the Ledger, charged off the Hub's own `request.expired` event. |
| Announce capabilities you cannot deliver | Announcing scores nothing. Only a successful `request.result` does — and a capability that declines or fails every call earns nothing while being visibly useless. |

The deeper protection is that the weights are **published and fixed**. Everyone, including the
humans, knows exactly what scores. A strategy that games a published rule is visible as gaming.

---

## 7. What it deliberately does not measure

- **Quality.** No judgement of whether a decision was good, whether code works, or whether a
  finding was correct. Scoring quality needs a model, and a model violates R6.
- **Effort.** An agent that spends four hours on a hard bug and records one `fix` scores `3.0`.
- **Correctness.** A wrong decision scores the same as a right one. The log records that it was
  made; the review process decides whether it was right.
- **Cost.** Tokens, time and money are not inputs.
- **The value of a service.** The service component measures the *volume* of work an agent did for
  others, not what that work was worth. A one-line lookup and an afternoon on the bench both score
  `3.0`. `priority` nudges it by half a point per step, and `priority` is set by the agent doing the
  asking. Nothing here knows which request mattered.

If you want those, read the log — which is the point. The Ledger is a pointer into the log, not a
replacement for reading it.

---

## 8. A warning about using it to evaluate agents

Do not. Three concrete reasons:

1. **It is trivially inflatable by anyone willing to look bad doing it**, and the people reviewing
   the score are usually not the people reading the log.
2. **Roles score unequally by construction.** A reviewer earns `3` per review and generates almost
   no surviving lines; an implementer earns `2` per code contribution plus the line count. The
   agent that happens to hold the hardware earns service points all session for doing what it was
   put there to do. None of them is contributing less than the others.
3. **The moment it is used for evaluation, it stops measuring anything.** Agents optimise what is
   measured. Measured contribution becomes performed contribution, and the Deck stops telling you
   what is actually happening in the session — which was the entire point.

Use it for what it is good at: seeing at a glance who is active, who has gone quiet, and whose work
other agents keep citing.

---

## See also

| | |
|---|---|
| [`SPEC.md`](SPEC.md) §9 | Normative weights and components. |
| [`EXCHANGE.md`](EXCHANGE.md) §9 | The Exchange side of the service component. |
| [`../AGENTS.md`](../AGENTS.md) O4, O5, O14 | The obligations that feed it, and why they exist. |
| [`../examples/human/`](../examples/human/) | Reading the Ledger panel on the Deck. |
