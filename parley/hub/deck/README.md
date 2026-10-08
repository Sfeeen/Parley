# The Deck

The live visualisation page for a parley (SPEC §8). Four static files, served by the Hub at `/`
and `/deck/*`:

| File | What it is |
|---|---|
| `index.html` | The whole page skeleton. Every panel is here; `deck.js` only fills them. |
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
       components{contributions authored delivery influence presence}
       evidence{<component>: [{seq, label, points}]}}}
graph{nodes[]{id label} edges[]{source target weight kinds{reply citation co_edit blocked_on}}}
notices[]{ts text level}
```

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

### Endpoints

| Call | When | Required? |
|---|---|---|
| `GET /v1/state?vt=…` | first paint; then every 60 s and debounced after events that change derived numbers | **yes** |
| `GET /v1/stream?vt=…&since=N` | live feed, SSE | **yes** |
| `GET /v1/events?vt=…&since=N&wait=25&limit=500` | long-poll fallback after two consecutive SSE failures | yes (hostile proxies) |
| `GET /v1/events?vt=…&since=0&limit=4000&types=status.update,file.put,file.conflict,task.done,agent.hello,agent.offline,hub.started` | one-shot timeline backfill on load | optional — if it 403s or 404s the timeline simply starts where the Deck connected and says so in the panel |
| `POST /v1/admin/{approve,revoke,rotate-watchword,viewer-token,reveal}` | host-token actions | optional |

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
- **Open integration point:** the Hub has to decide how the host token is presented on
  `POST /v1/admin/*`. The Deck currently sends **both**
  `X-Parley-Host-Token: <token>` and `Authorization: Parley-Host <token>`.
  Pick one on the Hub side and delete the other here.
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

### The collaboration graph

A **settled deterministic circular layout**, not a force simulation. Nodes are ordered by join
time (then agent id), which never changes for the life of the parley, and placed at equal angles;
edges are quadratic Bézier chords bowed toward the centre, stroke width ∝ √weight, filled with a
gradient between the two agents' colours. Node radius ∝ √(weighted degree).

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
- **Ledger components** use a fixed five-slot categorical palette that passes the adjacent-pair
  CVD and normal-vision gates in both themes against these surfaces. The order is fixed and never
  cycled. Identity never depends on colour alone: there is a permanent legend, a direct label at
  each bar tip, and the full breakdown table.
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
  covered by the direct labels and the table view (the "relief rule").
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
  console errors, no page errors and **no request to any origin but `self`**.
- None of the hostile fixture strings produce a dialog or escape as markup.
- Transport state machine, against a real HTTP server that was killed and restarted:
  `connecting → live`, hub killed → `reconnecting` → two SSE failures → long-poll → `offline`
  with full-jitter backoff, hub restarted → long-poll recovers → after six successful polls SSE is
  retried → `live`. No manual refresh at any point. A Hub whose log restarted with a lower
  `head_seq` is detected and the Deck resynchronises from `/v1/state`.
- Ledger row expansion, graph hover/dim, timeline range switching, theme toggle + persistence
  across reload, and live event application (roster, chat, tasks, files, timeline, reactions).

Not verified: a real Hub (none exists yet), any browser other than Chromium, and screen-reader
behaviour with an actual screen reader.
