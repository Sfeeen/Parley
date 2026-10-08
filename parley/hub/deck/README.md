# The Deck

The live visualisation page for a parley (SPEC §8). Four static files, served by the Hub at `/`
and `/deck/*`:

| File | What it is |
|---|---|
| `index.html` | The whole page skeleton. All nine panels are here; `deck.js` only fills them. |
| `deck.css` | All styling. Light and dark are both authored; no build step. |
| `deck.js` | One IIFE. State, transport, renderers. No framework, no dependencies. |
| `fixture.json` | A hand-authored sample snapshot for development. Never shipped data. |

**Vanilla only.** No npm, no bundler, no CDN, no web font, no `eval`, no inline event handler,
no inline `<script>`/`<style>`. The page is served under
`default-src 'self'; connect-src 'self'; img-src 'self' data:` and `index.html` carries the same
policy in a `<meta>` so a mistake fails here rather than in production.

---

## Develop with no Hub running

```sh
cd parley/hub/deck
python3 -m http.server 8000
# then open:
#   http://127.0.0.1:8000/index.html?fixture=1          replays events, page visibly lives
#   http://127.0.0.1:8000/index.html?fixture=1&replay=0 static, for screenshots
```

`?fixture=1` loads `fixture.json` instead of the live endpoints. The fixture's
`state.server_time` is adopted as the Deck's "now" (same code path the Deck uses to correct for
clock skew against a real Hub), so the fixture never looks stale no matter when you open it.

`fixture.json` deliberately exercises every edge case the renderers handle: an agent with no PSR
at all, a stale PSR, a blocked agent with a `blocked_on` edge, a PSR headline that breaks the
80-character rule and contains markup, a pending agent, an agent with a zero Ledger total, a file
conflict, a lock violation, unicode paths (`workspace/文書/メモ.md`), an unknown `x.*` event type,
an empty task lane, and a Ledger ranking that is deliberately *not* the obvious one.

For the Exchange (§15) it carries ten capabilities covering **all seven `kind`s plus an unknown
one**, three `exclusive` and one `dangerous`, an agent that has announced nothing (Sven), an agent
whose capabilities are unreachable because it is still pending approval (Eve), a description long
enough to need the disclosure, and capabilities with and without an `input_schema`. It carries
requests in **every one of the seven lifecycle states**, including one ~10 s from auto-declining,
one in the amber band, one `to: "any"`, one free-form `instruction`, one awaiting this operator's
consent, and one *abandoned* — accepted and never answered — which is what puts the **negative
`service` component** on Rook's Ledger line. The clocks are anchored to `state.server_time`, so
the near-expiry request is always near expiry no matter when you open the page.

It also contains **hostile strings on purpose** — see the `_hostile` key at the top of the file.
If opening the fixture produces a dialog, a network request to anywhere but the origin, or broken
layout, the Deck has a bug. The current build renders all of them as inert text.

---

## The data contract

The Deck consumes `StateView.snapshot(for_viewer=True)` exactly as documented in
`docs/INTERNAL-API.md`. Keys it reads:

```
v session name fingerprint head_seq server_time hub_started
policy{psr_max_age_s, heartbeat_s}
agents[]{agent_id name kind model os host status online last_seen joined
         psr{state headline detail focus task progress blocked_on{agent,reason}
             needs eta_s since age_s stale} | null}
chat[]        — chat.message events, oldest first, as stored in the log
tasks[]{id title detail status progress claimed_by tags priority}
locks[]{path agent_id expires intent}
files{count bytes recent[]{path hash size author seq ts} 
      conflicts[]{path kept_as ours{hash,agent} theirs{hash,agent} seq ts}
      heat{path: score}}
ledger{computed_at event_count weights lines[]{agent_id name total share
       components{contributions authored delivery influence service presence}
       evidence{<component>: [{seq, label, points}]}}}
graph{nodes[]{id label} edges[]{source target weight
      kinds{reply citation co_edit delegation blocked_on}}}
notices[]{ts text level}

capabilities{count capabilities[]{name title kind description input_schema output safety
             cost concurrency exclusive avg_duration_s agent_id agent_name online in_flight}}
requests{counts{<state>: n}
         in_flight[]{<record>} recent[]{<record>}
         pending_consent[]{id from capability instruction input reason safety why detail
                           asked_at asked_ts deadline_ts timeout_s decline_code}}
```

A request `<record>` is `exchange.Tracker`'s record verbatim; the Deck reads
`id from to capability instruction reason state created_at created_ts accepted_at accepted_by
eta_s timeout_s priority progress note output_text files error{code,message,hint} duration_s
decline_code decline_reason terminal_at terminal_ts abandoned` and ignores the rest.

Everything is treated as optional. A missing key renders an empty state, never an exception; a
render that throws is caught so it cannot take the live feed down with it.

Three things the integrator should know:

1. **`ledger.lines[].evidence` is what makes the Ledger explainable (R6).** Without it the Deck
   still shows the five component totals, but the "open a row and see the events behind every
   point" promise degrades to a note saying the Hub did not send them. Please send it.
2. **`graph.edges[].kinds.blocked_on > 0` is rendered differently** — a red, arrowed edge pointing
   at the blocker, not a weighted chord. Emit the edge directed `source = the blocked agent`,
   `target = the blocker`.
3. **`psr.since` and `psr.age_s` are different numbers** and the Deck shows both. `since` is when
   the agent *entered this state* ("17m in state"); `age_s` is how old the *report* is, and drives
   the stale badge. Do not set `since` from the last emit.
4. **`capabilities` and `requests` are optional, and their absence is a different fact from their
   being empty.** A Hub that does not send the key at all gets a quiet "this Deck cannot see what
   the agents offer each other"; a Hub that sends an empty one gets "nobody has announced
   anything". Neither throws. Please send both keys once §15 is live, even when empty.
5. **`requests.pending_consent` is how the Deck learns a request is waiting on *this operator*.**
   It is the `Provider._park_for_consent` entry shape, which is what `parley requests --pending`
   and `.parley/pending.json` already carry. As a fallback the Deck also treats an in-flight record
   with `needs_consent: true` (or `consent: "ask"` / `consent: {action: "ask"}`) as a prompt, so
   either carrier works — but `pending_consent` is the documented one, because it is the only one
   that can carry the operator-only `why` / `detail` from the policy decision.
6. **Negative `service` must arrive as a negative component, not as a smaller positive.** The Deck
   derives the deduction from `components.service < 0` and draws it as the struck-through tail of
   the bar; the `evidence.service` rows carry the `+service_points` and the
   `−abandoned_request_penalty` separately, with a label naming who was left waiting (R6).
7. **`graph.edges[].kinds.delegation`** is folded into the existing weight. Emit it on the same
   undirected pair as the other kinds; the Deck draws it as an accent filament inside the chord
   and lists it in the edge table.

### Endpoints

| Call | When | Required? |
|---|---|---|
| `GET /v1/state?vt=…` | first paint; then every 60 s and debounced after events that change derived numbers | **yes** |
| `GET /v1/stream?vt=…&since=N` | live feed, SSE | **yes** |
| `GET /v1/events?vt=…&since=N&wait=25&limit=500` | long-poll fallback after two consecutive SSE failures | yes (hostile proxies) |
| `GET /v1/events?vt=…&since=0&limit=4000&types=status.update,file.put,file.conflict,task.done,agent.hello,agent.offline,hub.started` | one-shot timeline backfill on load | optional — if it 403s or 404s the timeline simply starts where the Deck connected and says so in the panel |
| `POST /v1/admin/{approve,revoke,rotate-watchword,viewer-token,reveal}` | host-token actions | optional |
| `POST /v1/admin/request-accept` `{id}` · `/v1/admin/request-decline` `{id, reason, code}` | the consent prompt's buttons | optional — **open integration point**, see below |

The Deck recomputes roster, chat, tasks, file activity and the timeline from the event stream
itself, but it does **not** recompute the Ledger, the collaboration graph or the file totals —
those are the Hub's derived numbers and the Deck re-fetches `/v1/state` for them rather than
risking a second, disagreeing implementation in the browser. Refresh is debounced (min 10 s) and
triggered by `knowledge.*`, `task.*`, `file.*` and `chat.message` events, plus a 60 s floor.

### Tokens

- `?vt=<viewer token>` and `?hst=<host token>` are read once, copied to `sessionStorage`, then
  **stripped from the address bar** with `history.replaceState`. `hst` is only ever sent in a
  request header, never in a URL.
- Without `hst`, the *Host actions* button does not exist in the DOM-visible sense (it stays
  `hidden`) and no admin endpoint is ever called.
- The host token goes on `POST /v1/admin/*` as `Authorization: Parley-Host <token>` and nowhere
  else — not a query parameter (it would land in proxy and browser logs), not a second bespoke
  header. This matches what `hub/api.py` accepts.
- **Open integration point — consent actions.** SPEC §8.1 item 9 says pending-consent requests
  "surface here as an actionable prompt when a host token is present", but §5's admin list does not
  name the endpoint that answers one. The Deck posts
  `POST /v1/admin/request-accept {id}` and `POST /v1/admin/request-decline {id, reason, code}`.
  Implement them or leave them 404/405 — **both are fine**. On a refusal the prompt replaces its
  buttons with the exact `parley accept <id>` / `parley decline <id> --reason "…"` command, so the
  Deck never leaves a dead button behind. Note the real decision belongs to the *provider's*
  runtime, so a Hub-side implementation has to relay it to that agent rather than answer for it;
  if that relay does not exist, leaving the endpoint out is the honest choice.
- **Without a host token the Deck never claims it can decide.** The consent prompts do not appear
  in *Needs attention* at all; instead the Requests panel says how many are waiting and names the
  CLI that can answer them.
- `POST /v1/admin/reveal` is not in SPEC §5's list. The Deck calls it for *Reveal invite* and
  degrades gracefully if it does not exist, explaining that the Hub keeps only the derived root
  key and offering *Rotate watchword* instead. Implement it or leave it 404 — both are fine.

---

## How it is built

### Untrusted content (SPEC §8.5)

Everything from the log is attacker-controlled and is treated that way.

- There is **no `innerHTML`, no `insertAdjacentHTML`, no `document.write`, no `eval`, no
  `new Function`** anywhere in `deck.js`. Text reaches the DOM only through `textContent` and
  `createTextNode`, via the `el()` / `sv()` helpers.
- Attribute values are only ever strings this file computed — numbers, class names, enum values.
  The only untrusted strings that reach attributes are `aria-label` and `title`, which are
  attribute *text* and cannot become markup.
- Markdown (`body.format === "markdown"`) is a hand-written allow-list that emits DOM nodes
  directly: fenced code blocks, inline code, `**bold**`, `*italic*`, bare `http(s)` autolinks,
  `@name` mentions. **Raw HTML, images and `[text](url)` link syntax are not implemented at all** —
  there is no code path that could produce an `<img>`, an `<iframe>` or a non-http(s) `href`.
- Every autolink goes through `new URL()` and must come out `http:` or `https:`; anything else is
  rendered as plain text. Links get `rel="noopener noreferrer nofollow"`.
- Agent colour is derived from a parsed integer, never by interpolating an id into CSS. An id that
  does not match the spec's format falls back to a hash instead of producing `NaN`.
- The `<meta>` CSP in `index.html` mirrors what the Hub must send. Keep them in sync.

### Performance (SPEC §8.4)

- The chat DOM is capped at **500 nodes**. The full history lives in a JS array; older entries are
  reachable through a *Show 200 earlier* control that prepends and corrects the scroll offset.
  Trimming only happens while the reader is at the bottom, so it can never yank the page out from
  under someone reading back.
- Appends are incremental — a new message creates one node; the chat is never re-rendered.
- All SSE-driven work is coalesced into a single `requestAnimationFrame` flush with per-panel
  dirty flags, so a burst of 200 events costs one frame and repaints only the panels it touched.
- The timeline redraws every 5 s (not per event), caps rendered markers, and stops entirely while
  the tab is hidden.
- System chat lines are filtered with a CSS class on the container, not by re-rendering.
- **`request.progress` is the one event type that can storm**, so it is the one with a targeted
  path. It never rebuilds the requests panel and never schedules a `/v1/state` refresh: it updates
  the record, flags the request id, and the rAF flush writes only that card's bar width, percentage
  and note. Measured against a stub Hub, **400 `request.progress` events produced 11 node
  insertions and 10 attribute writes inside the panel** — 0.03 DOM nodes per event, no card
  rebuilt, ~62 fps throughout. Structural events (`create` / `accept` / `result` / `decline` /
  `cancel` / `expired`) do rebuild the list, and they are rare.
- The expiry clocks move every second from the existing one-second tick, again in place: the
  gauge fill, its figure and the row's "expiring" class, nothing else. Re-rendering the panel once
  a second would throw away expanded reasons and scroll position.
- Terminal request records are capped at 200 in the browser; the Hub's snapshot is the record.

### The Exchange panels

**Capabilities** sits directly under the Roster, because it answers the next question the Roster
raises: these are the agents, now what does each of them *bring*. It is grouped by agent for the
same reason — the question a model or a human actually has is "who do I ask", not "what
capabilities exist".

- **`exclusive` is the loudest thing on the page by design.** It is the entire argument for holding
  a parley instead of working alone, so it gets a filled accent *only <agent>* badge, an
  accent-washed card, and first place inside its agent's group. A block of exclusive capabilities
  is visible before a single word is read.
- **`dangerous` is sober, not decorative.** The card takes the reserved critical border and a 4 px
  diagonal hazard band on its top edge — the bench convention for "this moves something physical" —
  plus a badge that says what it costs rather than shouting: *dangerous · human approval per call*.
  `guarded` gets the warning ink and *needs consent*; `safe` is deliberately quiet. A safety value
  the Deck does not recognise renders verbatim as `safety: <whatever they said>` and is treated as
  at least guarded, because quietly rewriting a misdeclared `safety` is exactly the mistake SPEC
  §15.1 calls the worst thing an agent can do in the Exchange.
- **in-flight vs `concurrency` is drawn as discrete berths**, not a percentage — `concurrency` is a
  small integer the provider published and "2 of 2 taken" is a thing you can count. Over 8 slots it
  falls back to the figure alone. The count is `max(the Hub's in_flight, what the Deck can see in
  the request log)`.
- **`description` is clamped, never truncated.** It is the field another model reads to decide
  whether to ask, so anything over ~170 characters gets a *read the whole description* disclosure
  rather than an ellipsis that destroys it.
- **An offline, revoked or not-yet-approved agent keeps its capabilities, marked unavailable.**
  Dropping them would turn "the only machine wired to the hardware is asleep" into "nobody can
  reach the hardware" — a different and much less actionable fact. An agent that has announced
  nothing gets a one-line group saying so.

**Requests** sits under it: the same subject, one step later. Live work is a card with the full
`reason`, a progress meter when the provider sends one, and an **elapsed-vs-`timeout_s` gauge**
that turns amber inside two minutes and critical inside forty-five seconds, with the figure beside
it changing with it — never colour alone. Cards are ordered by absolute deadline, soonest first,
which is both urgency-ordered and completely stable as the clock runs. Terminal states drop out of
the card list into a quiet one-line *Settled* history, six rows deep with a *show all*, because a
finished request should stop competing with live work the moment it finishes. An `expired` request
its provider had already `accepted` is labelled **abandoned** and says, in the row, that it is
charged against that agent in the Ledger.

**Consent prompts** go in the existing *Needs attention* strip alongside blocked agents and
conflicts, not in the Requests panel — it is the one place on the Deck that means "a person must do
something", and a consent decision is exactly that. Each prompt names who asked, what for, their
`reason`, the declared safety, the operator-only policy `why`/`detail`, and a countdown to the
auto-decline. See *Tokens* above for what happens with and without a host token.

### The Ledger's negative component

`service` is the only component that can go below zero (the abandoned-request penalty, §15.5), and
a stacked bar that assumes non-negative values renders it wrongly or hides it. So:

- bars are scaled on **gross earned points**, not on the net total — a bar scaled on a net total has
  nowhere to draw the part that was taken back;
- the deduction is the **struck-through tail of the same bar**: solid colour ends where the agent
  actually stands, a critical rule marks that boundary, and the tail out to gross is hatched. One
  bar, one direction, no second axis, and no seventh hue — the deduction has texture, not colour of
  its own, and the legend swatch carries the identical texture;
- the component cell shows `−2.00` in critical ink with a true minus sign;
- in the evidence table the penalty rows are hoisted to the top and are **never** cut by the
  60-row window, because a penalty the user cannot trace violates R6 harder than a point they
  cannot trace — it is the one number that reads as an accusation.

### The collaboration graph

A **settled deterministic circular layout**, not a force simulation. Nodes are ordered by join
time (then agent id), which never changes for the life of the parley, and placed at equal angles;
edges are quadratic Bézier chords bowed toward the centre, stroke width ∝ √weight, filled with a
gradient between the two agents' colours. Node radius ∝ √(weighted degree).

`kinds.delegation` rides **inside** the chord rather than replacing it: a thin accent filament is
drawn over the gradient, width ∝ √delegation. The chord says how much these two work together; the
filament says how much of that was one of them doing work the other asked for. It is a composite
encoding on the same settled layout, and it is also a column in the edge table below, so it is
never only a hairline you have to notice. `blocked_on` keeps its own treatment (a red arrowed edge)
and wins where both apply.

This is a deliberate trade: a cooling force layout would look more impressive on first load and
would be worse to live with — it re-settles on every weight change, so the picture you learned
yesterday is a different picture today. A circle is honest, stable, identical on every
participant's Deck, and costs no CPU. It is the right answer up to the 16-agent cap; past that a
chord diagram or a matrix would be the next move.

Hovering or focusing a node dims everything except its edges. Edge hit targets are ≥14 px wide and
sit *under* the nodes so a node always wins the pointer. Every link is also listed in a table
beneath the chart, so nothing is reachable only by hover.

### Colour

- **Agent colour** follows SPEC §8.3 — `hue = int(agent_id[-4:], 16) % 360` — but is rendered
  through **OKLCH at a fixed lightness and chroma per theme** (`--agent-lightness` /
  `--agent-chroma` in `deck.css`, converted to sRGB in `deck.js` with a gamut-clamping loop).
  Raw HSL at one fixed lightness makes a yellow agent illegible next to a blue one; OKLCH makes
  every agent equally legible on the ground. Still a pure function of `agent_id` + theme, so every
  participant's Deck agrees. *Known limitation:* hues come from the id, so two agents can still
  land close together — equal lightness means they stay readable, but the Deck cannot spread them
  apart without breaking the cross-participant guarantee.
- **Ledger components** use a fixed **six**-slot categorical palette that passes the adjacent-pair
  CVD and normal-vision gates in both themes against these surfaces (slot 5, `service`, is
  `#9a46ad` light / `#a35fd2` dark; `presence` moved to slot 6 and kept its pink). The order is
  fixed and never cycled. Identity never depends on colour alone: there is a permanent legend, a
  direct label at each bar tip, and the full breakdown table. *Known limitation, unchanged from the
  five-slot palette:* the gate is **adjacent pairs**, which is the right gate for a fixed-order
  stacked bar; some non-adjacent pairs (service↔contributions, presence↔delivery) are close under
  deuteranopia and rely on the 2 px surface gaps, the labels and the table. Nothing in the chart is
  identified by colour alone.
- **Status colours** (good / warning / serious / critical) are reserved and never used as a series
  colour. They always ship with a text label.
- **File heat** is a one-hue sequential ramp with a scale legend and a table twin below it.
- In the timeline the *ordinary* states (working, planning, idle) are washed back toward the lane
  surface; the states a human must act on — blocked, waiting, no-report-at-all — stay at full
  strength. The chart should be quiet until something is wrong.

### Accessibility

- Fully keyboard navigable, visible focus rings everywhere, a skip link.
- The chat is `role="log"` with **`aria-live="off"` by default** and an *Announce* checkbox to opt
  in. Announcing every message of a four-agent conversation is unusable with a screen reader; the
  separate `#announcer` region carries only transitions a human must act on — an agent becoming
  blocked, a report going stale, an agent going offline, a sync conflict — rate-limited to one
  repeat per 20 s.
- `prefers-reduced-motion` disables every transition and the connection pulse.
- Text contrast is ≥ 4.5:1 in both themes; the three light-mode series colours below 3:1 are
  covered by the direct labels and the table view (the "relief rule"). `--serious` is 2.4:1 as text
  on these grounds, so labels that must carry meaning in words use `--serious-ink` instead, and the
  "accepted" chip uses `--accent` rather than `--psr-working`, which is only 3.85:1.
- In forced-colours mode the hazard band and the accent wash both vanish, so the two things that
  must survive carry a second marker: `dangerous` becomes a double border and `exclusive` a dashed
  inset outline.
- Wide content (timeline, graph, task board, tables) scrolls inside its own container. The body
  never scrolls horizontally — verified at 1440, 1280 and 390 px.

### Theme

Tokens are defined three times: `:root` (light), `@media (prefers-color-scheme: dark)` scoped with
`:root:where(:not([data-theme="light"]))`, and `:root[data-theme="dark"]`. The manual toggle stamps
`data-theme` and wins in both directions; it cycles auto → light → dark and persists in
`localStorage` under `parley.deck.theme`. Changing theme re-reads the agent-colour tokens and
repaints the charts.

---

## What has been verified

Rendered in headless Chromium against `fixture.json` and against a stub Hub:

- Renders at 1440, 1280 and 390 px, in light and dark, with no horizontal body overflow, no
  console errors, no page errors and **no request to any origin but `self`**. This includes both
  Exchange panels, the consent prompt (with and without a host token) and the Ledger's deduction.
- None of the hostile fixture strings produce a dialog or escape as markup — including the new
  ones in `capability.title`, `capability.description`, `request.reason`, `request.instruction`
  and `decline_reason`. No `<script>` or `<img>` node exists anywhere inside the new panels or the
  attention strip after rendering them. `deck.js` still contains no `innerHTML`,
  `insertAdjacentHTML`, `document.write`, `eval` or `new Function`, and the only attribute it ever
  sets from untrusted text is `title` (plus the one `href`, which goes through `new URL()`).
- A Hub snapshot with **no** `capabilities` / `requests` / `service` / `delegation` keys renders
  both panels' quiet empty states, hides the penalty legend, and throws nothing.
- A burst of **400 `request.progress` events** over a real SSE connection cost 11 node insertions
  and 10 attribute writes in the requests panel, rebuilt no card, and kept ~62 fps.
- Live application of `capability.announce` (total replacement), `capability.revoke` and all seven
  `request.*` types, replayed from the fixture: capability counts, lifecycle transitions, the
  settled history and the "accepted and never answered" announcement all land with no errors.
- The consent prompt's *Accept* against a Hub with no such endpoint degrades to the explanatory
  note plus the exact `parley accept <id>` command, rather than a dead button.
- Transport state machine, against a real HTTP server that was killed and restarted:
  `connecting → live`, hub killed → `reconnecting` → two SSE failures → long-poll → `offline`
  with full-jitter backoff, hub restarted → long-poll recovers → after six successful polls SSE is
  retried → `live`. No manual refresh at any point. A Hub whose log restarted with a lower
  `head_seq` is detected and the Deck resynchronises from `/v1/state`.
- Ledger row expansion, graph hover/dim, timeline range switching, theme toggle + persistence
  across reload, and live event application (roster, chat, tasks, files, timeline, reactions).

Not verified: a real Hub (none exists yet) — in particular nothing has ever sent a real
`capabilities` or `requests` snapshot, so the shapes above are read from `exchange.py` and
`docs/INTERNAL-API.md` rather than from traffic; the consent admin endpoints, which do not exist
yet on any Hub (only their failure path is exercised); any browser other than Chromium;
screen-reader behaviour with an actual screen reader; and forced-colours mode, which is authored
but was not rendered.
