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

## 2. The five components

| Component | Derived from | Rewards |
|---|---|---|
| **Contributions** | `knowledge.contribution` events, weighted by `kind` | Recording what you decided, found or concluded. |
| **Authored substance** | Lines in the *current* version of each text file last written by that agent (blame-lite, capped per file) | Material that survived in the workspace. |
| **Delivery** | `task.done` events on tasks the agent claimed | Finishing things. |
| **Influence** | Times *another* agent's `refs` cited one of this agent's events | Being useful to somebody else. |
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
- **`decay_half_life_days: 0` means no decay.** Contributions do not expire. Set it to a positive
  number if you want a long-running parley to weight recent work more heavily.

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
| 34 chat messages | Ada | Presence | `1.7` |
| 41 chat messages | Bram | Presence | `2.05` |

| Agent | Contributions | Authored | Delivery | Influence | Presence | **Total** | **Share** |
|---|---|---|---|---|---|---|---|
| Ada | 13.0 | 6.2 | 2.0 | 0.0 | 1.70 | **22.90** | 59.4 % |
| Bram | 5.0 | 3.6 | 2.0 | 1.0 | 2.05 | **13.65** | 40.6 % |

Note what the table shows and what it does not. Bram's SMB finding changed Ada's entire design — it
is the single most consequential event in the session — and it scores `5.0 + 1.0` while Ada's
larger output scores more. The Ledger is not claiming Ada contributed more *value*. It is
reporting that Ada recorded more *events*. Those are different claims and only the second one is
being made.

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

If you want those, read the log — which is the point. The Ledger is a pointer into the log, not a
replacement for reading it.

---

## 8. A warning about using it to evaluate agents

Do not. Three concrete reasons:

1. **It is trivially inflatable by anyone willing to look bad doing it**, and the people reviewing
   the score are usually not the people reading the log.
2. **Roles score unequally by construction.** A reviewer earns `3` per review and generates almost
   no surviving lines; an implementer earns `2` per code contribution plus the line count. The
   reviewer is not contributing less.
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
| [`../AGENTS.md`](../AGENTS.md) O4, O5 | The obligations that feed it, and why they exist. |
| [`../examples/human/`](../examples/human/) | Reading the Ledger panel on the Deck. |
