/* ============================================================================
 * The Deck — PARLEY/1 live visualisation
 * ----------------------------------------------------------------------------
 * Vanilla ES2017. No framework, no build step, no network except same-origin.
 *
 * Hard rules kept throughout this file:
 *   · Every string that came from the log is written with textContent or
 *     createTextNode. There is no innerHTML, no insertAdjacentHTML, no eval,
 *     no new Function, no inline event handler, anywhere.
 *   · Attributes are only ever set from values this file computed (numbers,
 *     class names, enum strings). The two exceptions are aria-label/title,
 *     which are attribute text and cannot become markup.
 *   · Anything that looks like a URL is parsed with the URL constructor and
 *     must come out http: or https: or it is rendered as plain text.
 *   · Markdown is a hand-rolled allow-list that builds DOM nodes directly:
 *     fenced code, inline code, bold, italic, bare http(s) autolinks, @names.
 *     Raw HTML, images, and [text](url) link syntax are deliberately absent.
 * ========================================================================== */

(function () {
  "use strict";

  var SVGNS = "http://www.w3.org/2000/svg";
  var MAX_CHAT_DOM = 500;      // SPEC §8.4
  var CHAT_PAGE = 200;         // how many older entries "show earlier" reveals
  var PSR_STATES = ["idle", "planning", "working", "reviewing", "blocked", "waiting", "offline"];
  var TASK_LANES = [
    ["todo", "To do"], ["doing", "Doing"], ["blocked", "Blocked"],
    ["review", "Review"], ["done", "Done"]
  ];
  var LEDGER_COMPONENTS = [
    ["contributions", "Contributions", "knowledge.contribution events, weighted by kind"],
    ["authored", "Authored substance", "surviving lines in files this agent last wrote"],
    ["delivery", "Delivery", "tasks this agent claimed and finished"],
    ["influence", "Influence", "times another agent cited this agent's events"],
    ["service", "Service", "work done for other agents through the Exchange, minus abandoned requests"],
    ["presence", "Presence", "chat messages, hard-capped so chattiness cannot win"]
  ];

  /* ---- the Exchange (SPEC §15) ---- */
  var REQ_STATES = ["pending", "accepted", "done", "failed", "declined", "expired", "cancelled"];
  var REQ_TERMINAL = ["done", "failed", "declined", "expired", "cancelled"];
  /* The Deck does not police `kind` or `safety` — it renders what the provider
     announced. An unknown value is shown as itself, because silently rewriting
     a bad `safety` is exactly the mistake SPEC §15.1 calls the worst thing an
     agent can do in the Exchange. */
  var SAFETY_LABEL = {
    safe: "safe",
    guarded: "guarded · needs consent",
    dangerous: "dangerous · human approval per call"
  };
  var DEFAULT_TIMEOUT_S = 300;
  var REQ_HISTORY_PAGE = 6;    // settled rows shown before "show all"
  var MAX_LOCAL_TERMINAL = 200; // terminal records the browser keeps between snapshots

  /* ======================================================== 0. boot params */

  var qs = new URLSearchParams(location.search);
  var FIXTURE = qs.get("fixture") === "1";
  var REPLAY = qs.get("replay") !== "0";

  /* Tokens are kept in memory + sessionStorage and stripped from the visible
     URL, so a screenshot or a shoulder-surfed address bar does not leak them. */
  var VT = qs.get("vt") || sessionStorage.getItem("parley.vt") || "";
  var HST = qs.get("hst") || sessionStorage.getItem("parley.hst") || "";
  if (qs.get("vt")) sessionStorage.setItem("parley.vt", qs.get("vt"));
  if (qs.get("hst")) sessionStorage.setItem("parley.hst", qs.get("hst"));
  if (qs.get("vt") || qs.get("hst")) {
    var clean = new URLSearchParams(location.search);
    clean.delete("vt"); clean.delete("hst");
    var qstr = clean.toString();
    history.replaceState(null, "", location.pathname + (qstr ? "?" + qstr : "") + location.hash);
  }

  /* ========================================================= 1. DOM helpers */

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function sv(tag, attrs) {
    var n = document.createElementNS(SVGNS, tag), k;
    if (attrs) for (k in attrs) if (Object.prototype.hasOwnProperty.call(attrs, k)) {
      n.setAttribute(k, String(attrs[k]));
    }
    return n;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function show(node, on) { if (on) node.removeAttribute("hidden"); else node.setAttribute("hidden", ""); }

  /* ==================================================== 2. deterministic colour
   * SPEC §8.3: hue = int(agent_id[-4:], 16) % 360, fixed S/L per theme.
   * Raw HSL at one fixed lightness is not perceptually even — a yellow and a
   * blue at hsl(…, 50%) differ by miles of apparent brightness, and one of them
   * will always be illegible. So the hue is taken exactly as the spec says and
   * then rendered through OKLCH at a fixed perceptual lightness and chroma per
   * theme. Still a pure function of agent_id + theme, so every participant's
   * Deck agrees, and every agent colour is equally legible on the ground.
   * ====================================================================== */

  var colorCache = Object.create(null);
  var themeTokens = { L: 0.515, C: 0.135 };

  function readThemeTokens() {
    var cs = getComputedStyle(document.documentElement);
    var L = parseFloat(cs.getPropertyValue("--agent-lightness"));
    var C = parseFloat(cs.getPropertyValue("--agent-chroma"));
    themeTokens.L = isFinite(L) ? L : 0.515;
    themeTokens.C = isFinite(C) ? C : 0.135;
    colorCache = Object.create(null);
  }

  function hueOf(agentId) {
    var id = String(agentId || "");
    var tail = id.slice(-4);
    var h = /^[0-9a-f]{4}$/i.test(tail) ? parseInt(tail, 16) : null;
    if (h === null || !isFinite(h)) {           // defensive: non-conforming id
      h = 0;
      for (var i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
    }
    return h % 360;
  }

  function oklchToRgb(L, C, hDeg) {
    var h = hDeg * Math.PI / 180;
    var a = C * Math.cos(h), b = C * Math.sin(h);
    var l_ = L + 0.3963377774 * a + 0.2158037573 * b;
    var m_ = L - 0.1055613458 * a - 0.0638541728 * b;
    var s_ = L - 0.0894841775 * a - 1.2914855480 * b;
    var l = l_ * l_ * l_, m = m_ * m_ * m_, s = s_ * s_ * s_;
    return [
       4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
      -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
      -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    ];
  }
  function gamma(u) {
    return u <= 0.0031308 ? 12.92 * u : 1.055 * Math.pow(u, 1 / 2.4) - 0.055;
  }
  function hex2(v) {
    var n = Math.max(0, Math.min(255, Math.round(v * 255)));
    return (n < 16 ? "0" : "") + n.toString(16);
  }
  function oklchHex(L, C, hDeg) {
    var rgb, i;
    for (i = 0; i < 40; i++) {                 // shrink chroma until in gamut
      rgb = oklchToRgb(L, C, hDeg);
      if (rgb[0] >= -0.001 && rgb[0] <= 1.001 &&
          rgb[1] >= -0.001 && rgb[1] <= 1.001 &&
          rgb[2] >= -0.001 && rgb[2] <= 1.001) break;
      C -= 0.006;
      if (C <= 0) { C = 0; rgb = oklchToRgb(L, 0, hDeg); break; }
    }
    return "#" + hex2(gamma(rgb[0])) + hex2(gamma(rgb[1])) + hex2(gamma(rgb[2]));
  }

  function agentColor(agentId) {
    var key = agentId + "|" + themeTokens.L + "|" + themeTokens.C;
    if (!colorCache[key]) colorCache[key] = oklchHex(themeTokens.L, themeTokens.C, hueOf(agentId));
    return colorCache[key];
  }

  function initials(name) {
    var s = String(name || "?").trim();
    if (!s) return "?";
    var parts = s.split(/[\s_\-./]+/).filter(Boolean);
    var out = parts.length > 1
      ? (parts[0][0] + parts[1][0])
      : s.slice(0, 2);
    return out.toUpperCase().slice(0, 2);
  }

  /* Identity token used in every panel. */
  function flagFor(agent, size) {
    var n = el("span", "flag" + (size ? " flag--" + size : ""));
    if (!agent) { n.classList.add("flag--off"); n.textContent = "??"; return n; }
    n.style.setProperty("--a", agentColor(agent.agent_id));
    if (size !== "xs") n.textContent = initials(agent.name);
    n.setAttribute("aria-hidden", "true");
    return n;
  }
  function whoChip(agent, size) {
    var w = el("span", "whochip");
    w.appendChild(flagFor(agent, size || "sm"));
    w.appendChild(el("span", "whochip__name", agent ? agent.name : "unknown agent"));
    return w;
  }

  /* ========================================================= 3. formatting */

  function parseTs(s) {
    if (typeof s === "number") return s;
    var t = Date.parse(String(s || ""));
    return isFinite(t) ? t : NaN;
  }
  function fmtAge(sec) {
    if (!isFinite(sec) || sec < 0) return "—";
    if (sec < 60) return Math.round(sec) + "s";
    if (sec < 3600) return Math.round(sec / 60) + "m";
    if (sec < 86400) return (sec / 3600).toFixed(sec < 36000 ? 1 : 0) + "h";
    return Math.round(sec / 86400) + "d";
  }
  function fmtDur(sec) {
    if (!isFinite(sec) || sec < 0) return "—";
    var d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
    if (d) return d + "d " + h + "h";
    if (h) return h + "h " + m + "m";
    return m + "m " + Math.floor(sec % 60) + "s";
  }
  function fmtBytes(b) {
    if (!isFinite(b)) return "—";
    if (b < 1024) return b + " B";
    if (b < 1048576) return (b / 1024).toFixed(b < 10240 ? 1 : 0) + " KiB";
    if (b < 1073741824) return (b / 1048576).toFixed(1) + " MiB";
    return (b / 1073741824).toFixed(2) + " GiB";
  }
  function fmtNum(n, dp) {
    if (!isFinite(n)) return "—";
    return n.toFixed(dp === undefined ? 1 : dp);
  }
  function clockOf(ms) {
    var d = new Date(ms);
    if (!isFinite(d.getTime())) return "--:--";
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }
  function dayOf(ms) {
    var d = new Date(ms);
    if (!isFinite(d.getTime())) return "unknown day";
    return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  }
  function shortPath(p) {
    var s = String(p || "");
    if (s.length <= 46) return s;
    return s.slice(0, 14) + "…" + s.slice(-30);
  }

  /* ================================================ 4. untrusted text → DOM */

  var URL_RE = /^https?:\/\//i;

  function safeHref(raw) {
    try {
      var u = new URL(String(raw));
      if (u.protocol === "http:" || u.protocol === "https:") return u.href;
    } catch (e) { /* not a URL */ }
    return null;
  }

  /* Matches, in priority order: fenced code is handled before this runs.
     1 inline code · 2 bold · 3 italic · 4 bare http(s) URL · 5 @mention */
  var INLINE_RE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|(https?:\/\/[^\s<>"'`)\]]+)|(@[A-Za-z0-9][A-Za-z0-9_.\-]{0,31})/g;

  function appendInline(parent, text, markdown) {
    var src = String(text);
    var last = 0, m;
    INLINE_RE.lastIndex = 0;
    while ((m = INLINE_RE.exec(src)) !== null) {
      if (m.index > last) parent.appendChild(document.createTextNode(src.slice(last, m.index)));
      var tok = m[0];
      if (m[1] && markdown) {
        parent.appendChild(el("code", null, tok.slice(1, -1)));
      } else if (m[2] && markdown) {
        parent.appendChild(el("strong", null, tok.slice(2, -2)));
      } else if (m[3] && markdown) {
        parent.appendChild(el("em", null, tok.slice(1, -1)));
      } else if (m[4]) {
        /* Trailing sentence punctuation is not part of the link. */
        var url = tok, trail = "";
        while (url.length && ".,;:!?".indexOf(url[url.length - 1]) >= 0) {
          trail = url[url.length - 1] + trail; url = url.slice(0, -1);
        }
        var href = safeHref(url);
        if (href) {
          var a = el("a", null, url);
          a.setAttribute("href", href);          // validated http(s) only
          a.setAttribute("target", "_blank");
          a.setAttribute("rel", "noopener noreferrer nofollow");
          a.setAttribute("title", href);
          parent.appendChild(a);
        } else {
          parent.appendChild(document.createTextNode(url));
        }
        if (trail) parent.appendChild(document.createTextNode(trail));
      } else if (m[5]) {
        parent.appendChild(el("b", "mention", tok));
      } else {
        parent.appendChild(document.createTextNode(tok));
      }
      last = m.index + tok.length;
    }
    if (last < src.length) parent.appendChild(document.createTextNode(src.slice(last)));
  }

  function renderBody(parent, text, format) {
    var markdown = format === "markdown";
    var src = String(text === undefined || text === null ? "" : text);
    if (!markdown) { appendInline(parent, src, false); return; }
    /* Fenced code first: everything inside is literal text in a <pre>. */
    var parts = src.split(/```/);
    for (var i = 0; i < parts.length; i++) {
      if (i % 2 === 1) {
        var body = parts[i].replace(/^[A-Za-z0-9_+\-]*\n/, "");   // drop the info string
        var pre = el("pre");
        pre.appendChild(el("code", null, body.replace(/\n$/, "")));
        parent.appendChild(pre);
      } else if (parts[i]) {
        appendInline(parent, parts[i], true);
      }
    }
  }

  /* ============================================================= 5. the store */

  var S = {
    session: "", name: "", fingerprint: "", headSeq: 0, lastSeq: 0,
    hubStarted: NaN, serverTime: NaN, policy: {},
    agents: new Map(),            // agent_id -> record (psr lives on it)
    order: [],                    // stable agent display order
    feed: [],                     // chat + system + day entries, seq order
    reactions: new Map(),         // target event id -> {reaction: count}
    tasks: new Map(),
    files: { count: 0, bytes: 0, recent: [], conflicts: [], heat: {} },
    ledger: null, graph: null, notices: [],
    tl: { lanes: new Map(), marks: [], t0: NaN },
    connected: false,

    /* The Exchange. `capsKey` / `reqsKey` record whether the Hub sent the key at
       all, which is a different fact from "the key was empty" and gets a
       different, quieter empty state. A Hub that predates §15 must not look
       like a parley where nobody ever offered anything. */
    caps: [], capsKey: false,
    reqs: new Map(), reqsKey: false, reqCounts: null, consentRaw: []
  };

  var dirty = {};
  var rafPending = false;
  var reqProgressDirty = new Set();   // request ids whose progress moved since the last frame
  function mark(what) { dirty[what] = true; schedule(); }
  function schedule() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(flush);
  }
  function flush() {
    rafPending = false;
    var d = dirty; dirty = {};
    try {
      if (d.bar) renderBar();
      if (d.roster) { renderRoster(); renderAttention(); }
      if (d.caps) renderCaps();
      /* A burst of request.progress is the one event storm this panel can see
         (SPEC §8.4): a provider streaming progress on four requests at once.
         Structural changes rebuild the list; progress only writes into the
         handful of nodes it actually moved. Both still cost one frame. */
      if (d.reqs) { renderRequests(); renderAttention(); }
      else if (d.reqProgress) paintReqProgress();
      if (d.ledger) renderLedger();
      if (d.graph) renderGraph();
      if (d.tasks) renderTasks();
      if (d.files) { renderHeat(); renderFiles(); renderAttention(); }
      if (d.timeline) renderTimeline();
      if (d.chat) flushChat();
    } catch (err) {
      /* A render bug must never take the live feed down. */
      if (window.console) console.error("[deck] render failed", err);
    }
  }

  function agentOf(id) { return S.agents.get(id) || null; }
  function agentName(id) { var a = agentOf(id); return a ? a.name : (id || "unknown"); }

  function psrState(a) {
    if (!a) return "unknown";
    if (a.online === false) return "offline";
    if (!a.psr || !a.psr.state) return "unknown";
    return PSR_STATES.indexOf(a.psr.state) >= 0 ? a.psr.state : "unknown";
  }
  function psrAgeSec(a) {
    if (!a || !a.psr) return Infinity;
    if (isFinite(a.psr.age_s)) return a.psr.age_s;
    var t = parseTs(a.psr.since);
    return isFinite(t) ? (nowMs() - t) / 1000 : Infinity;
  }
  function psrStale(a) {
    if (!a || !a.psr) return false;
    if (typeof a.psr.stale === "boolean") return a.psr.stale;
    var maxAge = S.policy && S.policy.psr_max_age_s ? S.policy.psr_max_age_s : 30;
    return psrAgeSec(a) > 3 * maxAge;
  }

  /* The Hub's clock is authoritative; track the offset so "age" is honest even
     if this browser's clock is wrong. */
  var clockOffset = 0;
  function nowMs() { return Date.now() + clockOffset; }

  /* ===================================================== 6. snapshot ingest */

  function ingestSnapshot(snap) {
    if (!snap || typeof snap !== "object") return;
    var reset = S.session && snap.session && snap.session !== S.session;
    if (reset) {
      S.feed.length = 0; S.tl.lanes.clear(); S.tl.marks.length = 0;
      chatReset();
      announce("The Hub restarted with a different parley. The Deck reloaded from scratch.");
    }
    S.session = snap.session || S.session;
    S.name = snap.name || S.name;
    S.fingerprint = snap.fingerprint || S.fingerprint;
    S.policy = snap.policy || S.policy || {};
    S.serverTime = parseTs(snap.server_time);
    if (isFinite(S.serverTime)) clockOffset = S.serverTime - Date.now();
    S.hubStarted = parseTs(snap.hub_started);
    if (isFinite(snap.head_seq)) {
      S.headSeq = snap.head_seq;
      if (snap.head_seq < S.lastSeq) {         // log was truncated / rebuilt
        S.lastSeq = snap.head_seq;
        S.feed.length = 0; chatReset();
        announce("The Hub's log restarted; the Deck resynchronised.");
      } else if (!S.lastSeq) {
        S.lastSeq = snap.head_seq;
      }
    }

    var seen = new Set();
    (snap.agents || []).forEach(function (a) {
      if (!a || !a.agent_id) return;
      seen.add(a.agent_id);
      var prev = S.agents.get(a.agent_id) || {};
      var rec = {
        agent_id: a.agent_id,
        name: a.name || a.agent_id,
        kind: a.kind || "",
        model: a.model || "",
        os: a.os || "",
        host: a.host || "",
        status: a.status || "active",
        online: a.online !== false,
        last_seen: a.last_seen,
        joined: a.joined,
        psr: a.psr || prev.psr || null
      };
      S.agents.set(a.agent_id, rec);
      seedLane(rec);
    });
    /* Agents the snapshot no longer lists are gone, not merely quiet. */
    Array.from(S.agents.keys()).forEach(function (id) {
      if (!seen.has(id)) S.agents.delete(id);
    });
    S.order = (snap.agents || []).map(function (a) { return a.agent_id; });

    /* Chat: the snapshot carries the last ~200 messages. Only take the ones we
       have not already placed, so a resync never duplicates the feed — and only
       rebuild the chat DOM when something was actually added, so the 60-second
       refresh never yanks the reader's scroll position or collapses history
       they expanded. */
    var known = new Set(S.feed.map(function (e) { return e.key; }));
    var addedChat = 0;
    (snap.chat || []).forEach(function (ev) {
      var k = feedKey(ev);
      if (!known.has(k)) { pushChat(ev, true); known.add(k); addedChat++; }
    });
    if (addedChat) {
      S.feed.sort(function (a, b) { return (a.seq || 0) - (b.seq || 0); });
      rebuildDaySeparators();
    }

    S.tasks.clear();
    (snap.tasks || []).forEach(function (t) { if (t && t.id) S.tasks.set(t.id, t); });

    var f = snap.files || {};
    S.files = {
      count: f.count || 0, bytes: f.bytes || 0,
      recent: (f.recent || []).slice(),
      conflicts: (f.conflicts || []).slice(),
      heat: f.heat || {}
    };
    S.ledger = snap.ledger || null;
    S.graph = snap.graph || null;
    S.notices = snap.notices || [];
    S.locks = snap.locks || [];

    ingestCapabilities(snap.capabilities);
    ingestRequests(snap.requests);

    if (addedChat) chatReset();
    mark("bar"); mark("roster"); mark("ledger"); mark("graph");
    mark("tasks"); mark("files"); mark("timeline"); mark("chat");
    mark("caps"); mark("reqs");
  }

  /* ---------------------------------------------------- the Exchange ingest
   * Both keys are brand new (INTERNAL-API "StateView snapshot additions") and a
   * Hub that has not shipped them yet must degrade quietly, so every field is
   * read defensively and nothing here can throw into the snapshot path.
   * ====================================================================== */

  function num(v, dflt) { return (typeof v === "number" && isFinite(v)) ? v : (dflt || 0); }
  function str(v) { return v === undefined || v === null ? "" : String(v); }

  function ingestCapabilities(raw) {
    var rows = null;
    if (raw && typeof raw === "object" && Array.isArray(raw.capabilities)) rows = raw.capabilities;
    else if (Array.isArray(raw)) rows = raw;            // tolerate a bare list
    if (!rows) return;                                   // key absent: keep what we have
    S.capsKey = true;
    S.caps = rows.filter(function (c) { return c && typeof c === "object"; }).map(capRec);
  }

  function capRec(c) {
    return {
      name: str(c.name),
      title: str(c.title),
      kind: str(c.kind) || "tool",
      description: str(c.description),
      input_schema: (c.input_schema && typeof c.input_schema === "object") ? c.input_schema : null,
      output: str(c.output) || "text",
      safety: str(c.safety) || "guarded",
      cost: str(c.cost),
      concurrency: Math.max(0, Math.round(num(c.concurrency, 1))),
      exclusive: c.exclusive === true,
      avg_duration_s: num(c.avg_duration_s),
      agent_id: str(c.agent_id),
      agent_name: str(c.agent_name),
      online: c.online !== false,
      in_flight: Math.max(0, Math.round(num(c.in_flight)))
    };
  }

  function ingestRequests(raw) {
    if (!raw || typeof raw !== "object") return;
    S.reqsKey = true;
    var seen = Object.create(null);
    [raw.in_flight, raw.recent].forEach(function (list) {
      if (!Array.isArray(list)) return;
      list.forEach(function (r) {
        var rec = reqRec(r);
        if (!rec) return;
        seen[rec.id] = true;
        S.reqs.set(rec.id, rec);
      });
    });
    /* Records the Deck applied from the live stream but the snapshot has not
       caught up to are kept; the next snapshot supersedes them. Terminal ones
       are capped so a long session cannot grow without bound. */
    trimRequests();
    S.reqCounts = (raw.counts && typeof raw.counts === "object") ? raw.counts : null;
    S.consentRaw = Array.isArray(raw.pending_consent) ? raw.pending_consent : [];
  }

  function reqRec(r) {
    if (!r || typeof r !== "object") return null;
    var id = str(r.id);
    if (!id) return null;
    var state = str(r.state) || "pending";
    if (REQ_STATES.indexOf(state) < 0) state = "pending";
    var err = (r.error && typeof r.error === "object") ? r.error : null;
    return {
      id: id,
      from: str(r.from),
      to: str(r.to) || "any",
      capability: str(r.capability),
      instruction: str(r.instruction),
      reason: str(r.reason),
      state: state,
      created_at: str(r.created_at),
      created_ts: num(r.created_ts),
      accepted_by: str(r.accepted_by),
      accepted_at: str(r.accepted_at),
      eta_s: num(r.eta_s),
      timeout_s: num(r.timeout_s, DEFAULT_TIMEOUT_S) || DEFAULT_TIMEOUT_S,
      priority: Math.max(1, Math.min(5, Math.round(num(r.priority, 3)))),
      progress: (typeof r.progress === "number" && isFinite(r.progress))
        ? Math.max(0, Math.min(1, r.progress)) : null,
      note: str(r.note),
      output_text: str(r.output_text),
      files: Array.isArray(r.files) ? r.files.filter(function (f) { return typeof f === "string"; }) : [],
      error: err ? { code: str(err.code), message: str(err.message), hint: str(err.hint) } : null,
      duration_s: num(r.duration_s),
      decline_code: str(r.decline_code),
      decline_reason: str(r.decline_reason),
      terminal_at: str(r.terminal_at),
      terminal_ts: num(r.terminal_ts),
      abandoned: r.abandoned === true,
      late_result: r.late_result === true,
      anomalies: Array.isArray(r.anomalies) ? r.anomalies.map(str) : [],
      /* Two shapes the Hub may use to say "this one is waiting on the operator".
         Neither is load-bearing: `requests.pending_consent` is the documented
         carrier and this is belt-and-braces. */
      needs_consent: r.needs_consent === true ||
        r.consent === "ask" ||
        !!(r.consent && typeof r.consent === "object" && r.consent.action === "ask"),
      safety: str(r.safety) || (r.consent && typeof r.consent === "object" ? str(r.consent.safety) : ""),
      why: str(r.why) || (r.consent && typeof r.consent === "object" ? str(r.consent.why) : "")
    };
  }

  function trimRequests() {
    var terminal = [];
    S.reqs.forEach(function (r) {
      if (REQ_TERMINAL.indexOf(r.state) >= 0) terminal.push(r);
    });
    if (terminal.length <= MAX_LOCAL_TERMINAL) return;
    terminal.sort(function (a, b) { return reqEndMs(a) - reqEndMs(b); });
    terminal.slice(0, terminal.length - MAX_LOCAL_TERMINAL).forEach(function (r) {
      S.reqs.delete(r.id);
    });
  }

  function reqStartMs(r) {
    var t = parseTs(r.created_at);
    if (isFinite(t)) return t;
    return r.created_ts > 0 ? r.created_ts * 1000 : NaN;
  }
  function reqEndMs(r) {
    var t = parseTs(r.terminal_at);
    if (isFinite(t)) return t;
    return r.terminal_ts > 0 ? r.terminal_ts * 1000 : NaN;
  }
  function reqDeadlineMs(r) {
    var s = reqStartMs(r);
    return isFinite(s) ? s + r.timeout_s * 1000 : NaN;
  }

  function seedLane(rec) {
    if (S.tl.lanes.has(rec.agent_id)) return;
    var pts = [];
    var since = rec.psr && rec.psr.since ? parseTs(rec.psr.since) : NaN;
    if (rec.psr && isFinite(since)) pts.push({ t: since, state: psrState(rec) });
    S.tl.lanes.set(rec.agent_id, pts);
  }

  function lanePush(agentId, t, state) {
    if (!S.tl.lanes.has(agentId)) S.tl.lanes.set(agentId, []);
    var pts = S.tl.lanes.get(agentId);
    if (!isFinite(t)) return;
    var i = pts.length - 1;
    while (i >= 0 && pts[i].t > t) i--;
    if (i >= 0 && pts[i].state === state) return;      // no-op transition
    pts.splice(i + 1, 0, { t: t, state: state });
    if (pts.length > 600) pts.splice(0, pts.length - 600);
  }

  /* ======================================================= 7. event ingest */

  function feedKey(ev) { return (ev.id || "") + "|" + (ev.seq || 0); }

  function applyEvent(ev) {
    if (!ev || typeof ev !== "object") return;
    if (isFinite(ev.seq)) {
      if (ev.seq <= S.lastSeq) return;                 // idempotent replay
      S.lastSeq = ev.seq;
      if (ev.seq > S.headSeq) S.headSeq = ev.seq;
    }
    var t = parseTs(ev.ts);
    if (!isFinite(t)) t = nowMs();
    var b = (ev.body && typeof ev.body === "object") ? ev.body : {};
    var type = String(ev.type || "");
    var actor = ev.actor || "hub";

    switch (type) {
      case "chat.message":
        pushChat(ev, false);
        mark("chat");
        scheduleStateRefresh();
        break;

      case "chat.reaction": {
        var tgt = String(b.target || "");
        var map = S.reactions.get(tgt) || {};
        var r = String(b.reaction || "?");
        map[r] = (map[r] || 0) + 1;
        S.reactions.set(tgt, map);
        repaintReactions(tgt);
        break;
      }

      case "status.update": {
        var a = agentOf(actor);
        if (!a) break;
        var prevState = psrState(a), prevStale = psrStale(a);
        a.psr = b;
        if (!a.psr.since) a.psr.since = ev.ts;
        a.psr.age_s = 0; a.psr.stale = false;
        a.online = true;
        lanePush(actor, parseTs(b.since) || t, PSR_STATES.indexOf(b.state) >= 0 ? b.state : "unknown");
        if (psrState(a) === "blocked" && prevState !== "blocked") {
          announce(a.name + " is blocked: " + (b.headline || ""));
        } else if (prevStale) {
          /* recovering from stale is good news but not worth announcing */
        }
        mark("roster"); mark("timeline");
        break;
      }

      case "agent.hello": {
        var rec = S.agents.get(actor) || { agent_id: actor, psr: null };
        rec.name = b.name || rec.name || actor;
        rec.kind = b.kind || rec.kind || "";
        rec.model = b.model || rec.model || "";
        rec.os = b.os || rec.os || "";
        rec.host = b.host || rec.host || "";
        rec.online = true;
        rec.status = rec.status || "active";
        if (!rec.joined) rec.joined = ev.ts;
        S.agents.set(actor, rec);
        if (S.order.indexOf(actor) < 0) S.order.push(actor);
        seedLane(rec);
        sysLine(ev, t, [[rec.name, "b"], [" joined — " + (rec.kind || "agent") + (rec.os ? " on " + rec.os : ""), ""]]);
        mark("roster"); mark("graph"); mark("timeline");
        scheduleStateRefresh();
        break;
      }

      case "agent.offline":
      case "agent.bye":
      case "agent.revoked": {
        var who = b.agent_id || actor;
        var ao = agentOf(who);
        if (ao) {
          ao.online = false;
          if (type === "agent.revoked") ao.status = "revoked";
          lanePush(who, t, "offline");
        }
        sysLine(ev, t, [[agentName(who), "b"], [
          type === "agent.revoked" ? " was revoked" :
          type === "agent.bye" ? " left" :
          " went offline (" + (b.reason || "timeout") + ")", ""]]);
        if (type !== "agent.bye") announce(agentName(who) + " went offline.");
        mark("roster"); mark("timeline");
        break;
      }

      case "agent.heartbeat": {
        var ah = agentOf(actor);
        if (ah) { ah.online = true; ah.last_seen = ev.ts; }
        mark("roster");
        break;
      }

      case "file.put": {
        var p = String(b.path || "");
        S.files.recent.unshift({
          path: p, hash: b.hash, size: b.size, author: actor, seq: ev.seq, ts: ev.ts,
          lock_violation: b.lock_violation === true
        });
        if (S.files.recent.length > 120) S.files.recent.length = 120;
        S.files.heat[p] = (S.files.heat[p] || 0) + 1;
        S.tl.marks.push({ t: t, agent: actor, kind: "put", label: p });
        sysLine(ev, t, [[agentName(actor), "b"], [" wrote ", ""], [p, "mono"]]);
        mark("files"); mark("timeline");
        scheduleStateRefresh();
        break;
      }

      case "file.delete":
        sysLine(ev, t, [[agentName(actor), "b"], [" deleted ", ""], [String(b.path || ""), "mono"]]);
        mark("files");
        break;

      case "file.move":
        sysLine(ev, t, [[agentName(actor), "b"], [" moved ", ""], [String(b.from || ""), "mono"],
                        [" → ", ""], [String(b.to || ""), "mono"]]);
        mark("files");
        break;

      case "file.conflict": {
        var cp = String(b.path || "");
        S.files.conflicts.unshift({
          path: cp, kept_as: b.kept_as, ours: b.ours, theirs: b.theirs, seq: ev.seq, ts: ev.ts
        });
        S.tl.marks.push({ t: t, agent: (b.theirs && b.theirs.agent) || actor, kind: "conflict", label: cp });
        sysLine(ev, t, [["conflict", "b"], [" on ", ""], [cp, "mono"], [" — both versions kept", ""]]);
        announce("Sync conflict on " + cp + ". Both versions were kept.");
        mark("files"); mark("timeline");
        break;
      }

      case "lock.acquire":
        sysLine(ev, t, [[agentName(actor), "b"], [" locked ", ""], [(b.paths || []).join(", "), "mono"],
                        [b.intent ? " — " + b.intent : "", ""]]);
        break;
      case "lock.release":
        sysLine(ev, t, [[agentName(actor), "b"], [" released ", ""], [(b.paths || []).join(", "), "mono"]]);
        break;
      case "lock.denied":
        sysLine(ev, t, [["lock denied", "b"], [" on ", ""], [(b.paths || []).join(", "), "mono"],
                        [" — held by " + agentName(b.held_by), ""]]);
        break;

      case "task.create":
        S.tasks.set(b.id, {
          id: b.id, title: b.title, detail: b.detail, status: "todo",
          tags: b.tags || [], priority: b.priority, claimed_by: null, progress: 0
        });
        sysLine(ev, t, [[agentName(actor), "b"], [" created task ", ""], [String(b.id || ""), "mono"]]);
        mark("tasks");
        break;
      case "task.claim": {
        var tc = S.tasks.get(b.id); if (tc) { tc.claimed_by = actor; if (tc.status === "todo") tc.status = "doing"; }
        sysLine(ev, t, [[agentName(actor), "b"], [" claimed ", ""], [String(b.id || ""), "mono"]]);
        mark("tasks"); break;
      }
      case "task.release": {
        var tr = S.tasks.get(b.id); if (tr) { tr.claimed_by = null; tr.status = "todo"; }
        sysLine(ev, t, [[agentName(actor), "b"], [" released ", ""], [String(b.id || ""), "mono"]]);
        mark("tasks"); break;
      }
      case "task.update": {
        var tu = S.tasks.get(b.id);
        if (tu) { if (b.status) tu.status = b.status; if (isFinite(b.progress)) tu.progress = b.progress; }
        sysLine(ev, t, [[agentName(actor), "b"], [" set ", ""], [String(b.id || ""), "mono"],
                        [" → " + (b.status || "") + (b.note ? " · " + b.note : ""), ""]]);
        mark("tasks"); break;
      }
      case "task.done": {
        var td = S.tasks.get(b.id); if (td) { td.status = "done"; td.progress = 1; }
        S.tl.marks.push({ t: t, agent: actor, kind: "done", label: (td && td.title) || String(b.id || "") });
        sysLine(ev, t, [[agentName(actor), "b"], [" finished ", ""], [String(b.id || ""), "mono"]]);
        mark("tasks"); mark("timeline"); scheduleStateRefresh();
        break;
      }

      case "knowledge.contribution":
        sysLine(ev, t, [[agentName(actor), "b"], [" filed a " + (b.kind || "contribution") + ": ", ""],
                        [String(b.title || ""), ""]]);
        scheduleStateRefresh();
        break;

      case "decision.propose":
        sysLine(ev, t, [[agentName(actor), "b"], [" proposed: " + String(b.question || ""), ""]]);
        break;
      case "decision.vote":
        sysLine(ev, t, [[agentName(actor), "b"], [" voted " + String(b.option || ""), ""]]);
        break;
      case "decision.resolve":
        sysLine(ev, t, [["decision resolved", "b"], [" → " + String(b.option || ""), ""]]);
        break;

      case "hub.started":
        S.hubStarted = t;
        sysLine(ev, t, [["the Hub started", "b"]]);
        mark("bar");
        break;
      case "hub.policy":
        S.policy = Object.assign({}, S.policy, b);
        sysLine(ev, t, [["policy updated", "b"]]);
        break;
      case "hub.notice":
        S.notices.unshift({ ts: ev.ts, text: String(b.text || ""), level: b.level || "info" });
        sysLine(ev, t, [["notice", "b"], [" " + String(b.text || ""), ""]]);
        break;

      /* ---- the Exchange (SPEC §15) ----------------------------------------
         `capability.announce` is total, not incremental: it replaces that
         agent's whole catalogue. Applying it any other way would leave a
         revoked capability on the Deck forever. */
      case "capability.announce": {
        var announced = Array.isArray(b.capabilities) ? b.capabilities : [];
        S.capsKey = true;
        S.caps = S.caps.filter(function (c) { return c.agent_id !== actor; });
        announced.forEach(function (c) {
          if (!c || typeof c !== "object") return;
          var rec = capRec(c);
          if (!rec.agent_id) rec.agent_id = actor;
          if (!rec.agent_name) rec.agent_name = agentName(actor);
          rec.online = true;
          S.caps.push(rec);
        });
        sysLine(ev, t, [[agentName(actor), "b"],
          [" announced " + announced.length + (announced.length === 1 ? " capability" : " capabilities"), ""]]);
        mark("caps"); scheduleStateRefresh();
        break;
      }

      case "capability.revoke": {
        var gone = Array.isArray(b.names) ? b.names.map(str) : [];
        S.caps = S.caps.filter(function (c) {
          return !(c.agent_id === actor && gone.indexOf(c.name) >= 0);
        });
        sysLine(ev, t, [[agentName(actor), "b"], [" withdrew ", ""], [gone.join(", "), "mono"]]);
        mark("caps"); scheduleStateRefresh();
        break;
      }

      case "request.create": {
        var rid = str(b.id);
        if (rid && !S.reqs.has(rid)) {
          S.reqsKey = true;
          var fresh = reqRec({
            id: rid, from: actor, to: b.to, capability: b.capability,
            instruction: b.instruction, reason: b.reason, state: "pending",
            created_at: ev.ts, timeout_s: b.timeout_s, priority: b.priority
          });
          if (fresh) S.reqs.set(rid, fresh);
        }
        sysLine(ev, t, [[agentName(actor), "b"],
          [" asked " + (str(b.to) === "any" || !b.to ? "anyone" : agentName(b.to)) + " for ", ""],
          [str(b.capability) || "a free-form task", "mono"]]);
        mark("reqs"); scheduleStateRefresh();
        break;
      }

      case "request.accept": {
        var ra = S.reqs.get(str(b.id));
        if (ra && ra.state === "pending") {
          ra.state = "accepted"; ra.accepted_by = actor; ra.accepted_at = ev.ts;
          ra.eta_s = num(b.eta_s); ra.needs_consent = false;
        }
        sysLine(ev, t, [[agentName(actor), "b"], [" accepted ", ""], [str(b.id), "mono"]]);
        mark("reqs"); scheduleStateRefresh();
        break;
      }

      /* The chatty one. No system line, no state refresh, no full re-render —
         just the record and a targeted repaint of the nodes that moved. */
      case "request.progress": {
        var rp = S.reqs.get(str(b.id));
        if (rp) {
          if (typeof b.progress === "number" && isFinite(b.progress)) {
            rp.progress = Math.max(0, Math.min(1, b.progress));
          }
          if (typeof b.note === "string") rp.note = b.note;
          reqProgressDirty.add(rp.id);
          mark("reqProgress");
        }
        break;
      }

      case "request.result": {
        var rr = S.reqs.get(str(b.id));
        var rok = b.ok !== false;
        if (rr && REQ_TERMINAL.indexOf(rr.state) < 0) {
          rr.state = rok ? "done" : "failed";
          if (!rr.accepted_by) rr.accepted_by = actor;
          rr.output_text = str(b.output_text);
          rr.files = Array.isArray(b.files)
            ? b.files.filter(function (f) { return typeof f === "string"; }) : [];
          rr.error = (b.error && typeof b.error === "object")
            ? { code: str(b.error.code), message: str(b.error.message), hint: str(b.error.hint) } : null;
          rr.duration_s = num(b.duration_s);
          rr.terminal_at = ev.ts;
          if (rok) rr.progress = 1;
          rr.needs_consent = false;
        }
        sysLine(ev, t, [[agentName(actor), "b"],
          [(rok ? " finished " : " failed "), ""], [str(b.id), "mono"]]);
        mark("reqs"); scheduleStateRefresh();
        break;
      }

      case "request.decline": {
        var rd = S.reqs.get(str(b.id));
        if (rd && REQ_TERMINAL.indexOf(rd.state) < 0) {
          rd.state = "declined";
          rd.decline_code = str(b.code) || "other";
          rd.decline_reason = str(b.reason);
          rd.terminal_at = ev.ts;
          rd.needs_consent = false;
        }
        sysLine(ev, t, [[agentName(actor), "b"], [" declined ", ""], [str(b.id), "mono"],
          [str(b.code) ? " — " + str(b.code) : "", ""]]);
        mark("reqs"); scheduleStateRefresh();
        break;
      }

      case "request.cancel": {
        var rc = S.reqs.get(str(b.id));
        if (rc && REQ_TERMINAL.indexOf(rc.state) < 0) {
          rc.state = "cancelled";
          rc.decline_reason = str(b.reason);
          rc.terminal_at = ev.ts;
          rc.needs_consent = false;
        }
        sysLine(ev, t, [[agentName(actor), "b"], [" withdrew ", ""], [str(b.id), "mono"]]);
        mark("reqs"); scheduleStateRefresh();
        break;
      }

      case "request.expired": {
        var rx = S.reqs.get(str(b.id));
        var abandoned = b.abandoned === true;
        if (rx && REQ_TERMINAL.indexOf(rx.state) < 0) {
          rx.state = "expired";
          rx.abandoned = abandoned;
          rx.terminal_at = ev.ts;
          rx.needs_consent = false;
        }
        var provider = str(b.provider) || (rx && (rx.accepted_by || rx.to)) || "";
        sysLine(ev, t, [[str(b.id), "mono"],
          [" expired — " + (str(b.reason) || "nobody answered in time"), ""]]);
        if (abandoned) {
          /* The one unforgivable Exchange behaviour (SPEC §15.3), and the only
             thing in the whole Ledger that subtracts. Worth saying out loud. */
          announce(agentName(provider) + " accepted a request and never answered it.");
        }
        mark("reqs"); scheduleStateRefresh();
        break;
      }

      case "request.taken": {
        var rt = S.reqs.get(str(b.id));
        if (rt && !rt.accepted_by) rt.accepted_by = str(b.by);
        mark("reqs");
        break;
      }

      default:
        /* Unknown and x.* types are stored, shown as a bare line, never parsed. */
        sysLine(ev, t, [[type || "event", "mono"], [" from " + agentName(actor), ""]]);
        break;
    }
    mark("bar");
  }

  /* ============================================================ 8. the feed */

  function pushChat(ev, quiet) {
    var t = parseTs(ev.ts); if (!isFinite(t)) t = nowMs();
    var b = (ev.body && typeof ev.body === "object") ? ev.body : {};
    S.feed.push({
      kind: "chat", key: feedKey(ev), seq: ev.seq || 0, t: t,
      id: ev.id || "", actor: ev.actor || "hub",
      text: String(b.text === undefined ? "" : b.text),
      format: b.format === "markdown" ? "markdown" : "text",
      to: Array.isArray(b.to) ? b.to : null,
      replyTo: b.reply_to || b.thread || null,
      refs: Array.isArray(b.refs) ? b.refs : null
    });
    if (!quiet) maybeAnnounceChat();
  }

  function sysLine(ev, t, chunks) {
    S.feed.push({
      kind: "sys", key: feedKey(ev), seq: ev.seq || 0, t: t,
      actor: ev.actor || "hub", chunks: chunks
    });
    mark("chat");
  }

  function rebuildDaySeparators() {
    var out = [], lastDay = null;
    for (var i = 0; i < S.feed.length; i++) {
      var e = S.feed[i];
      if (e.kind === "day") continue;
      var d = dayOf(e.t);
      if (d !== lastDay) { out.push({ kind: "day", key: "day|" + d, seq: e.seq, t: e.t, day: d }); lastDay = d; }
      out.push(e);
    }
    S.feed = out;
  }

  /* ============================================================= 9. chat UI */

  var chatLog, chatMore, chatJump;
  var renderedFrom = 0, renderedTo = 0, chatNodes = [], newSinceScroll = 0;
  var nodeByEventId = new Map();

  function chatReset() {
    if (!chatLog) return;
    clear(chatLog);
    chatNodes = []; nodeByEventId.clear();
    renderedFrom = Math.max(0, S.feed.length - MAX_CHAT_DOM);
    renderedTo = renderedFrom;
    newSinceScroll = 0;
    mark("chat");
  }

  function atBottom() {
    if (!chatLog) return true;
    return chatLog.scrollHeight - chatLog.scrollTop - chatLog.clientHeight < 56;
  }

  function flushChat() {
    if (!chatLog) return;
    var wasBottom = atBottom();
    var frag = document.createDocumentFragment();
    var added = 0;
    while (renderedTo < S.feed.length) {
      var entry = S.feed[renderedTo];
      var prev = renderedTo > renderedFrom ? S.feed[renderedTo - 1] : null;
      var node = renderEntry(entry, prev);
      entry._node = node;
      chatNodes.push(node);
      frag.appendChild(node);
      renderedTo++; added++;
    }
    if (added) chatLog.appendChild(frag);

    /* Cap the DOM. Only trim while the reader is at the bottom, so trimming can
       never yank the scroll position out from under someone reading history. */
    if (wasBottom) {
      while (chatNodes.length > MAX_CHAT_DOM) {
        var dead = chatNodes.shift();
        if (dead && dead.parentNode) dead.parentNode.removeChild(dead);
        if (dead && dead.__evid) nodeByEventId.delete(dead.__evid);
        renderedFrom++;
      }
    }
    updateMoreButton();

    if (wasBottom) {
      chatLog.scrollTop = chatLog.scrollHeight;
      newSinceScroll = 0;
      show(chatJump, false);
    } else if (added) {
      newSinceScroll += added;
      chatJump.textContent = "Jump to latest · " + newSinceScroll + " new";
      show(chatJump, true);
    }
  }

  function updateMoreButton() {
    if (renderedFrom > 0) {
      chatMore.textContent = "Show " + Math.min(CHAT_PAGE, renderedFrom) + " earlier · " +
        renderedFrom + " hidden";
      show(chatMore, true);
    } else {
      show(chatMore, false);
    }
  }

  function showEarlier() {
    if (renderedFrom <= 0) return;
    var start = Math.max(0, renderedFrom - CHAT_PAGE);
    var before = chatLog.scrollHeight;
    var frag = document.createDocumentFragment();
    for (var i = start; i < renderedFrom; i++) {
      var node = renderEntry(S.feed[i], i > start ? S.feed[i - 1] : null);
      S.feed[i]._node = node;
      frag.appendChild(node);
      chatNodes.splice(i - start, 0, node);
    }
    chatLog.insertBefore(frag, chatLog.firstChild);
    renderedFrom = start;
    chatLog.scrollTop += chatLog.scrollHeight - before;   // hold the reading position
    updateMoreButton();
  }

  function renderEntry(e, prev) {
    if (e.kind === "day") return el("div", "daysep", e.day);
    if (e.kind === "sys") return renderSys(e);
    return renderMsg(e, prev);
  }

  function renderSys(e) {
    var row = el("div", "msg msg--sys");
    row.appendChild(el("div", "msg__gutter sysmark", "·"));
    var body = el("div", "msg__body");
    var line = el("div", "msg__text");
    (e.chunks || []).forEach(function (c) {
      var txt = String(c[0]);
      if (!txt) return;
      if (c[1] === "b") line.appendChild(el("b", null, txt));
      else if (c[1] === "mono") line.appendChild(el("code", null, txt));
      else line.appendChild(document.createTextNode(txt));
    });
    var when = el("span", "msg__when", " " + clockOf(e.t));
    line.appendChild(when);
    body.appendChild(line);
    row.appendChild(body);
    return row;
  }

  function renderMsg(e, prev) {
    var a = agentOf(e.actor);
    var cont = prev && prev.kind === "chat" && prev.actor === e.actor && (e.t - prev.t) < 180000;
    var row = el("div", "msg" + (cont ? " msg--cont" : ""));
    row.__evid = e.id;
    if (e.id) nodeByEventId.set(e.id, row);

    var gutter = el("div", "msg__gutter");
    if (!cont) gutter.appendChild(flagFor(a));
    row.appendChild(gutter);

    var body = el("div", "msg__body");
    if (!cont) {
      var head = el("div", "msg__head");
      head.appendChild(el("span", "msg__who", a ? a.name : e.actor));
      head.appendChild(el("span", "msg__when", clockOf(e.t)));
      if (e.to && e.to.length && e.to.indexOf("all") < 0) {
        head.appendChild(el("span", "msg__to", "→ " + e.to.map(agentName).join(", ")));
      }
      body.appendChild(head);
    }

    if (e.replyTo) {
      var rr = el("div", "msg__refs");
      rr.appendChild(el("span", "ref ref--reply", "↩ reply"));
      body.appendChild(rr);
    }

    var txt = el("div", "msg__text");
    renderBody(txt, e.text, e.format);
    body.appendChild(txt);

    if (e.refs && e.refs.length) {
      var rl = el("div", "msg__refs");
      e.refs.slice(0, 8).forEach(function (r) {
        if (!r || typeof r !== "object") return;
        var kind = String(r.kind || "ref");
        var chip = el("span", "ref" + (kind === "file" ? " ref--file" : ""),
          kind + " · " + String(r.value === undefined ? "" : r.value));
        chip.setAttribute("title", kind + ": " + String(r.value === undefined ? "" : r.value));
        rl.appendChild(chip);
      });
      body.appendChild(rl);
    }

    var rx = el("div", "msg__reactions");
    body.appendChild(rx);
    paintReactions(rx, e.id);

    row.appendChild(body);
    return row;
  }

  function paintReactions(container, eventId) {
    clear(container);
    var map = S.reactions.get(eventId);
    if (!map) return;
    Object.keys(map).forEach(function (k) {
      container.appendChild(el("span", "reaction", k + " " + map[k]));
    });
  }
  function repaintReactions(eventId) {
    var node = nodeByEventId.get(eventId);
    if (!node) return;
    var box = node.querySelector(".msg__reactions");
    if (box) paintReactions(box, eventId);
  }

  /* Screen readers: the chat is role="log" with aria-live OFF by default,
     because announcing every message of a fast multi-agent conversation is
     unusable. The checkbox opts in; the separate announcer below only ever
     carries state changes a human must act on. */
  var announceChat = false;
  function maybeAnnounceChat() {
    if (!announceChat || !chatLog) return;
    chatLog.setAttribute("aria-live", "polite");
  }

  var announcer, lastAnnounce = "", announceAt = 0;
  function announce(text) {
    if (!announcer) return;
    var t = Date.now();
    if (text === lastAnnounce && t - announceAt < 20000) return;
    lastAnnounce = text; announceAt = t;
    announcer.textContent = text;
  }

  /* ========================================================= 10. session bar */

  function renderBar() {
    $("sessionName").textContent = S.name || "a parley";
    $("sessionId").textContent = S.session || "—";
    $("statFingerprint").textContent = S.fingerprint || "—";
    var online = 0, total = 0;
    S.agents.forEach(function (a) { total++; if (a.online && a.status !== "revoked") online++; });
    $("statAgents").textContent = online + " of " + total;
    $("statSeq").textContent = S.headSeq ? "#" + S.headSeq : "—";
    $("statUptime").textContent = isFinite(S.hubStarted) ? fmtDur((nowMs() - S.hubStarted) / 1000) : "—";
  }

  function setConn(kind, text, title) {
    var c = $("conn");
    c.className = "conn conn--" + kind;
    $("connText").textContent = text;
    c.setAttribute("title", title || text);
  }

  /* ============================================================= 11. roster */

  function renderRoster() {
    var list = $("roster");
    clear(list);
    var ids = S.order.filter(function (id) { return S.agents.has(id); });
    S.agents.forEach(function (a, id) { if (ids.indexOf(id) < 0) ids.push(id); });

    if (!ids.length) {
      var li = el("li");
      li.appendChild(el("p", "empty", "No agents have enrolled yet."));
      list.appendChild(li);
      $("rosterMeta").textContent = "";
      return;
    }

    /* Sort: needs-a-human first, then working, then quiet. Agents that are not
       actually participating (pending approval, revoked) go last whatever their
       report says — they are not the thing you came to look at. */
    var rank = { blocked: 0, unknown: 1, waiting: 2, working: 3, reviewing: 4, planning: 5, idle: 6, offline: 7 };
    function rankOf(a) {
      if (a.status === "pending" || a.status === "revoked") return 9;
      if (psrStale(a) && psrState(a) !== "offline") return 1.5;
      var r = rank[psrState(a)];
      return r === undefined ? 6 : r;
    }
    ids.sort(function (x, y) {
      var ax = S.agents.get(x), ay = S.agents.get(y);
      var sx = rankOf(ax), sy = rankOf(ay);
      if (sx !== sy) return sx - sy;
      return String(ax.name).localeCompare(String(ay.name));
    });

    var counts = {};
    ids.forEach(function (id) {
      var a = S.agents.get(id);
      var st = psrState(a);
      counts[st] = (counts[st] || 0) + 1;
      list.appendChild(agentCard(a));
    });
    $("rosterMeta").textContent = ids.length + " enrolled";
  }

  function agentCard(a) {
    var li = el("li", "agent");
    var state = psrState(a);
    var stale = psrStale(a);
    var noPsr = !a.psr;
    if (state === "blocked") li.classList.add("agent--blocked");
    if (noPsr || (stale && a.online)) li.classList.add("agent--nonconf");
    if (!a.online) li.classList.add("agent--offline");
    if (a.status === "pending") li.classList.add("agent--pending");

    var top = el("div", "agent__top");
    top.appendChild(flagFor(a));
    var ident = el("div", "agent__ident");
    ident.appendChild(el("div", "agent__name", a.name));
    var sub = [a.kind || "agent"];
    if (a.model) sub.push(a.model);
    if (a.os) sub.push(a.os);
    if (a.host) sub.push(a.host);
    ident.appendChild(el("div", "agent__kind", sub.join(" · ")));
    top.appendChild(ident);
    var dot = el("span", "agent__pres" + (a.online ? (state === "idle" ? " agent__pres--idle" : " agent__pres--on") : ""));
    dot.setAttribute("title", a.online ? "online" : "offline");
    top.appendChild(dot);
    li.appendChild(top);

    var badges = el("div", "agent__row");
    if (!(noPsr && a.online)) badges.appendChild(badge(state, state === "unknown" ? "no report" : state));
    if (a.status === "pending") badges.appendChild(badge("unknown", "pending approval"));
    if (a.status === "revoked") badges.appendChild(badge("blocked", "revoked"));
    if (noPsr && a.online) {
      badges.appendChild(extraBadge("nopsr", "no standing report"));
    } else if (stale && a.online) {
      badges.appendChild(extraBadge("stale", "stale " + fmtAge(psrAgeSec(a))));
    } else if (a.psr) {
      /* `since` is when this state began; `age_s` is how fresh the report is.
         Two different numbers, and conflating them is a classic misread. */
      var sinceT = parseTs(a.psr.since);
      if (isFinite(sinceT)) {
        badges.appendChild(el("span", "badge badge--idle", fmtAge((nowMs() - sinceT) / 1000) + " in state"));
      }
    }
    li.appendChild(badges);

    if (noPsr) {
      li.appendChild(el("p", "agent__headline agent__headline--none",
        "Has filed no Parley Standing Report. This agent is not following the standard — nobody can see what it is doing."));
    } else {
      li.appendChild(el("p", "agent__headline", String(a.psr.headline || "(no headline — the standard requires one)")));
      if (a.psr.detail) li.appendChild(el("p", "agent__detail", String(a.psr.detail)));
    }

    if (a.psr && isFinite(a.psr.progress)) {
      var m = el("div", "meter");
      var track = el("div", "meter__track");
      var fill = el("div", "meter__fill");
      fill.style.width = Math.max(0, Math.min(1, a.psr.progress)) * 100 + "%";
      fill.style.setProperty("--a", agentColor(a.agent_id));
      track.appendChild(fill); m.appendChild(track);
      m.appendChild(el("span", "meter__val", Math.round(a.psr.progress * 100) + "%"));
      li.appendChild(m);
    }

    if (a.psr && a.psr.blocked_on && typeof a.psr.blocked_on === "object") {
      var bb = el("div", "blockedbox");
      bb.appendChild(el("span", "blockedbox__label", "blocked on"));
      var bd = el("div", "blockedbox__body");
      var blocker = agentOf(a.psr.blocked_on.agent);
      if (blocker) { bd.appendChild(whoChip(blocker)); bd.appendChild(document.createTextNode(" — ")); }
      bd.appendChild(document.createTextNode(String(a.psr.blocked_on.reason || "no reason given")));
      bb.appendChild(bd);
      li.appendChild(bb);
    }

    if (a.psr && Array.isArray(a.psr.needs) && a.psr.needs.length) {
      var nd = el("p", "agent__detail", "Needs: " + a.psr.needs.map(String).join(" · "));
      li.appendChild(nd);
    }

    if (a.psr && Array.isArray(a.psr.focus) && a.psr.focus.length) {
      var paths = el("div", "paths");
      a.psr.focus.slice(0, 8).forEach(function (p) {
        var hot = (S.files.heat[p] || 0) > 0;
        var c = el("span", "path" + (hot ? " path--hot" : ""), shortPath(p));
        c.setAttribute("title", String(p));
        paths.appendChild(c);
      });
      li.appendChild(paths);
    }

    var foot = el("div", "agent__foot");
    if (a.psr && a.psr.task) {
      var tk = S.tasks.get(a.psr.task);
      foot.appendChild(el("span", null, "task " + a.psr.task + (tk ? " · " + tk.title : "")));
    }
    if (a.psr && isFinite(a.psr.eta_s)) foot.appendChild(el("span", null, "eta " + fmtAge(a.psr.eta_s)));
    foot.appendChild(el("span", null, a.agent_id));
    li.appendChild(foot);
    return li;
  }

  function badge(state, label) {
    var b = el("span", "badge badge--" + state);
    b.appendChild(el("span", "badge__dot"));
    b.appendChild(document.createTextNode(label));
    return b;
  }
  function extraBadge(kind, label) {
    var b = el("span", "badge badge--" + kind);
    b.appendChild(el("span", "badge__dot"));
    b.appendChild(document.createTextNode(label));
    return b;
  }

  /* ========================================================== 12. attention */

  function renderAttention() {
    var list = $("attnList");
    clear(list);
    consentClocks.length = 0;
    var items = [];

    S.agents.forEach(function (a) {
      if (a.status === "pending") {
        items.push({ k: "pending", a: a, text: "is waiting for the host to approve it" });
      }
      if (!a.online) return;
      var st = psrState(a);
      if (st === "blocked") {
        var bo = (a.psr && a.psr.blocked_on) || {};
        items.push({
          k: "blocked", a: a,
          text: "blocked on " + (bo.agent ? agentName(bo.agent) : "something") +
                (bo.reason ? " — " + bo.reason : "")
        });
      } else if (!a.psr) {
        items.push({ k: "nopsr", a: a, text: "has filed no standing report" });
      } else if (psrStale(a)) {
        items.push({ k: "stale", a: a, text: "standing report is " + fmtAge(psrAgeSec(a)) + " old" });
      }
    });

    S.files.conflicts.slice(0, 5).forEach(function (c) {
      items.push({ k: "conflict", a: null, text: "conflict on " + c.path + " — kept as " + (c.kept_as || "a sidecar") });
    });

    items.forEach(function (it) {
      var li = el("li", "attn__item");
      li.appendChild(el("span", "attn__kind attn__kind--" + it.k, it.k === "nopsr" ? "no psr" : it.k));
      if (it.a) li.appendChild(whoChip(it.a));
      li.appendChild(el("span", "attn__what", it.text));
      list.appendChild(li);
    });

    /* Consent prompts are the human-in-the-loop surface for the whole §15.4
       model, so they belong here with the other things only a person can clear
       — but only when this Deck actually holds a host token. Without one the
       buttons would be theatre, so the Requests panel points at the CLI
       instead (see renderConsentNote). */
    var consent = HST ? consentList() : [];
    consent.forEach(function (c) {
      list.appendChild(consentPrompt(c));
    });

    show($("attn"), (items.length + consent.length) > 0);
  }

  /* ================================================== 12b. the Exchange: caps
   * "What each agent can do for the others" (SPEC §8.1 item 8, §15.1).
   *
   * Grouped by agent because the question this panel answers is not "what
   * capabilities exist" but "who should I ask". Inside a group, `exclusive`
   * sorts first and is washed in the accent: an exclusive capability is the
   * entire argument for holding a parley instead of working alone, and it has
   * to survive being glanced at. `dangerous` carries the reserved critical
   * colour and a four-pixel hazard band — the bench convention, not an
   * exclamation mark.
   *
   * An offline agent keeps its capabilities on the Deck, marked unavailable.
   * Dropping them would quietly turn "the only machine wired to the hardware is
   * asleep" into "nobody can reach the hardware", which is a different and much
   * less actionable fact.
   * ====================================================================== */

  var capOpen = new Set();   // "<agent> <name>" of descriptions read in full

  function renderCaps() {
    var host = $("capGroups");
    clear(host);
    var meta = $("capsMeta");

    if (!S.capsKey) {
      host.appendChild(el("p", "empty",
        "This Hub has not sent a capability registry. Nothing is missing from the parley — this Deck simply cannot see what the agents offer each other."));
      meta.textContent = "";
      return;
    }

    var rows = S.caps || [];
    var byAgent = new Map();
    rows.forEach(function (c) {
      var k = c.agent_id || "";
      if (!byAgent.has(k)) byAgent.set(k, []);
      byAgent.get(k).push(c);
    });

    /* Every enrolled agent gets a group, even an empty one: an agent that has
       announced nothing is a visible fact, not an absence. */
    var ids = S.order.filter(function (id) { return S.agents.has(id); });
    S.agents.forEach(function (a, id) { if (ids.indexOf(id) < 0) ids.push(id); });
    byAgent.forEach(function (_v, id) { if (id && ids.indexOf(id) < 0) ids.push(id); });

    if (!ids.length) {
      host.appendChild(el("p", "empty", "Nobody has enrolled yet, so nobody has anything to lend."));
      meta.textContent = "";
      return;
    }

    var nExcl = 0, nDanger = 0;
    rows.forEach(function (c) { if (c.exclusive) nExcl++; if (c.safety === "dangerous") nDanger++; });

    ids.sort(function (x, y) {
      var cx = byAgent.get(x) || [], cy = byAgent.get(y) || [];
      if (!cx.length !== !cy.length) return cx.length ? -1 : 1;
      var ex = cx.filter(function (c) { return c.exclusive; }).length;
      var ey = cy.filter(function (c) { return c.exclusive; }).length;
      if (ex !== ey) return ey - ex;
      if (cx.length !== cy.length) return cy.length - cx.length;
      return String(agentName(x)).localeCompare(String(agentName(y)));
    });

    ids.forEach(function (id) {
      host.appendChild(capGroup(id, byAgent.get(id) || []));
    });

    meta.textContent = rows.length + (rows.length === 1 ? " capability" : " capabilities") +
      " · " + nExcl + " exclusive" + (nDanger ? " · " + nDanger + " dangerous" : "");
  }

  function capGroup(agentId, caps) {
    var a = agentOf(agentId);
    var who = a || { agent_id: agentId, name: (caps[0] && caps[0].agent_name) || agentId };
    /* "Available" is stricter than "online": an agent still waiting for the
       host to approve it cannot be asked for anything, and one that was revoked
       certainly cannot. Its catalogue still shows — it is a fact about the
       parley — but it shows as unreachable. */
    var why = "";
    if (a && a.status === "pending") why = "pending approval — unavailable";
    else if (a && a.status === "revoked") why = "revoked — unavailable";
    else if (a ? a.online === false : !(caps.length && caps[0].online)) why = "offline — unavailable";
    var online = !why;

    var box = el("div", "capgroup" + (online ? "" : " capgroup--off"));
    var head = el("div", "capgroup__head");
    head.appendChild(flagFor(who, "sm"));
    head.appendChild(el("span", "capgroup__name", who.name));
    if (why) head.appendChild(badgeOf("unavail", why));
    var m = el("span", "capgroup__meta");
    m.textContent = caps.length
      ? caps.length + (caps.length === 1 ? " capability" : " capabilities")
      : "nothing announced";
    head.appendChild(m);
    box.appendChild(head);

    if (!caps.length) {
      box.appendChild(el("p", "capgroup__none",
        "Has announced no capabilities. The others cannot ask it for anything — only talk to it."));
      return box;
    }

    caps = caps.slice().sort(function (p, q) {
      if (p.exclusive !== q.exclusive) return p.exclusive ? -1 : 1;
      var dp = p.safety === "dangerous", dq = q.safety === "dangerous";
      if (dp !== dq) return dp ? -1 : 1;
      return String(p.name).localeCompare(String(q.name));
    });

    var grid = el("div", "capgrid");
    caps.forEach(function (c) { grid.appendChild(capCard(c, who, online, why)); });
    box.appendChild(grid);
    return box;
  }

  function capCard(c, who, online, why) {
    var card = el("div", "capcard" +
      (c.exclusive ? " capcard--exclusive" : "") +
      (c.safety === "dangerous" ? " capcard--dangerous" : ""));

    var top = el("div", "capcard__top");
    var kind = el("span", "kindchip", c.kind || "tool");
    kind.setAttribute("title", "kind: " + (c.kind || "tool"));
    top.appendChild(kind);
    top.appendChild(el("span", "capname", c.name || "(unnamed)"));
    card.appendChild(top);

    if (c.title) card.appendChild(el("p", "captitle", c.title));

    var flags = el("div", "capflags");
    if (c.exclusive) {
      var ex = badgeOf("excl", "only " + (who.name || "this agent"));
      ex.setAttribute("title",
        "No other agent in this parley has announced this capability. It is why the parley is worth more than these agents working alone.");
      flags.appendChild(ex);
    }
    var safety = c.safety || "guarded";
    var skey = safety === "dangerous" ? "danger" : safety === "safe" ? "safe"
             : safety === "guarded" ? "guarded" : "unavail";
    var sb = badgeOf(skey, SAFETY_LABEL[safety] || ("safety: " + safety));
    sb.setAttribute("title", safety === "dangerous"
      ? "Declared dangerous: it may never be auto-accepted. Every single call needs an explicit human approval (SPEC §15.4)."
      : safety === "safe"
        ? "Declared safe: read-only, no side effects outside the workspace. May be auto-accepted."
        : safety === "guarded"
          ? "Declared guarded: never auto-accepted unless the provider's own policy names this capability and this requester."
          : "The provider declared a safety level the Deck does not recognise. Treat it as at least guarded.");
    flags.appendChild(sb);
    if (c.cost) flags.appendChild(badgeOf("cost", c.cost));
    if (!online) flags.appendChild(badgeOf("unavail", "unavailable"));
    card.appendChild(flags);

    /* The description is the single highest-value field in the Exchange — it is
       what another model reads to decide whether to ask. Clamping it is fine;
       truncating it away is not. */
    if (c.description) {
      var key = (c.agent_id || "") + " " + (c.name || "");
      var longish = c.description.length > 168;
      var open = capOpen.has(key);
      var p = el("p", "capdesc" + (longish && !open ? " is-clamped" : ""), c.description);
      card.appendChild(p);
      if (longish) {
        var more = el("button", "capmore", open ? "show less" : "read the whole description");
        more.type = "button";
        more.setAttribute("aria-expanded", open ? "true" : "false");
        more.addEventListener("click", function () {
          if (capOpen.has(key)) capOpen.delete(key); else capOpen.add(key);
          renderCaps();
        });
        card.appendChild(more);
      }
    }

    var schema = c.input_schema;
    if (schema && typeof schema === "object" && schema.properties && typeof schema.properties === "object") {
      var req = Array.isArray(schema.required) ? schema.required.map(str) : [];
      var keys = Object.keys(schema.properties);
      if (keys.length) {
        var sc = el("div", "capschema");
        sc.appendChild(el("span", "capschema__lbl", "takes"));
        keys.slice(0, 7).forEach(function (k) {
          var isReq = req.indexOf(k) >= 0;
          var chip = el("span", "capkey" + (isReq ? " capkey--req" : ""), k + (isReq ? "*" : ""));
          chip.setAttribute("title", isReq ? k + " (required)" : k + " (optional)");
          sc.appendChild(chip);
        });
        if (keys.length > 7) sc.appendChild(el("span", "capkey", "+" + (keys.length - 7)));
        card.appendChild(sc);
      }
    }

    card.appendChild(capSlots(c, who, online, why));

    var foot = el("div", "capfoot");
    foot.appendChild(el("span", null, "returns " + (c.output || "text")));
    if (c.avg_duration_s > 0) foot.appendChild(el("span", null, "~" + fmtAge(c.avg_duration_s) + " typical"));
    card.appendChild(foot);
    return card;
  }

  /* Discrete berths rather than a percentage: `concurrency` is a small integer
     the provider published, and "2 of 2 taken" is a thing you can count. */
  function capSlots(c, who, online, why) {
    var cap = Math.max(0, c.concurrency || 0);
    var live = S.reqsKey ? liveInFlight(c.agent_id, c.name) : 0;
    var used = Math.max(c.in_flight || 0, live);
    var full = cap > 0 && used >= cap;

    var box = el("div", "slots" + (full ? " is-full" : ""));
    if (online && cap > 0 && cap <= 8) {
      var cells = el("span", "slots__cells");
      cells.setAttribute("aria-hidden", "true");
      for (var i = 0; i < cap; i++) {
        var cell = el("span", "slots__cell" + (i < used ? " is-on" : ""));
        if (i < used) cell.style.setProperty("--a", agentColor(who.agent_id));
        cells.appendChild(cell);
      }
      box.appendChild(cells);
    }
    var txt = el("span", "slots__txt");
    if (!online) {
      txt.textContent = (why || "unavailable").split(" — ")[0] + " — asking will be declined";
    } else if (cap > 0) {
      txt.textContent = used + " of " + cap + " in flight" + (full ? " · at capacity" : "");
    } else {
      txt.textContent = used + " in flight · no limit declared";
    }
    box.appendChild(txt);
    return box;
  }

  function liveInFlight(agentId, name) {
    var n = 0;
    S.reqs.forEach(function (r) {
      if (r.state !== "accepted") return;
      if ((r.accepted_by || r.to) !== agentId) return;
      if (name && r.capability && r.capability !== name) return;
      if (name && !r.capability) return;          // free-form work is not this capability
      n++;
    });
    return n;
  }

  function badgeOf(kind, label) {
    var b = el("span", "badge badge--" + kind);
    b.appendChild(document.createTextNode(label));
    return b;
  }

  /* ============================================== 12c. requests in flight
   * The delegation view (SPEC §8.1 item 9, §15.3): who asked whom for what, how
   * long ago, and how close it is to running out. Live work is a card; settled
   * work drops into a quiet single-line history, because a finished request
   * should stop competing for attention the moment it finishes.
   * ====================================================================== */

  var reqNodes = new Map();       // id -> the few nodes a progress/clock tick writes into
  var consentClocks = [];         // countdown nodes in the attention strip
  var reqHistAll = false;
  var reqReasonOpen = new Set();

  function renderRequests() {
    var live = $("reqsLive");
    clear(live);
    reqNodes.clear();
    var meta = $("reqsMeta");

    if (!S.reqsKey) {
      live.appendChild(el("p", "empty",
        "This Hub has not sent the Exchange's request log. Agents may still be delegating work to each other — this Deck cannot see it."));
      meta.textContent = "";
      show($("reqsHistSec"), false);
      renderConsentNote();
      return;
    }

    var all = [];
    S.reqs.forEach(function (r) { all.push(r); });
    var inflight = all.filter(function (r) { return r.state === "pending" || r.state === "accepted"; });
    var settled = all.filter(function (r) { return REQ_TERMINAL.indexOf(r.state) >= 0; });

    /* Sorted by absolute deadline, soonest first. A fixed ordering, so the list
       does not re-shuffle under the reader every time a second passes. */
    inflight.sort(function (p, q) {
      var dp = reqDeadlineMs(p), dq = reqDeadlineMs(q);
      if (!isFinite(dp)) dp = Infinity;
      if (!isFinite(dq)) dq = Infinity;
      if (dp !== dq) return dp - dq;
      return String(p.id).localeCompare(String(q.id));
    });
    settled.sort(function (p, q) {
      var ep = reqEndMs(p), eq = reqEndMs(q);
      return (isFinite(eq) ? eq : 0) - (isFinite(ep) ? ep : 0);
    });

    if (!inflight.length) {
      live.appendChild(el("p", "empty", settled.length
        ? "Nothing in flight. Every request has been answered."
        : "No delegated work yet. Agents ask each other with “parley ask” and “parley instruct”."));
    } else {
      inflight.forEach(function (r) { live.appendChild(reqCard(r)); });
    }

    var consentN = consentList().length;
    meta.textContent = inflight.length + " in flight" +
      (consentN ? " · " + consentN + " awaiting consent" : "") +
      (settled.length ? " · " + settled.length + " settled" : "");

    renderReqHistory(settled);
    renderConsentNote();
    paintReqClocks();
  }

  function reqCard(r) {
    var clock = reqClock(r);
    var expiring = !!(clock && (clock.level === "crit") && r.state !== "done");
    var card = el("div", "req" +
      (r.needs_consent ? " req--consent" : "") +
      (expiring ? " req--expiring" : ""));

    var top = el("div", "req__top");
    top.appendChild(whoChip(agentOf(r.from) || { agent_id: r.from, name: r.from || "unknown" }, "sm"));
    var arrow = el("span", "req__arrow", "→");
    arrow.setAttribute("aria-label", "asked");
    top.appendChild(arrow);
    top.appendChild(reqTargetChip(r));
    top.appendChild(reqStateChip(r));
    if (!r.capability) {
      var ff = el("span", "rchip rchip--free", "free-form");
      ff.setAttribute("title",
        "A natural-language instruction, not a registered capability. SPEC §15.4 rule 2: nobody validated it against a schema, so it is never treated as safe.");
      top.appendChild(ff);
    }
    var idn = el("span", "req__id", r.id);
    idn.setAttribute("title", "request id " + r.id);
    top.appendChild(idn);
    card.appendChild(top);

    var ask = el("p", "req__ask" + (r.capability ? "" : " req__instruction"));
    if (r.capability) {
      ask.appendChild(el("span", "req__cap", r.capability));
    } else {
      ask.appendChild(document.createTextNode(r.instruction || "(no instruction given)"));
    }
    card.appendChild(ask);

    if (r.reason) card.appendChild(reqReason(r));

    /* Progress, when the provider sends it. The nodes are remembered so a burst
       of request.progress can write into them without rebuilding the card. */
    var refs = { row: card };
    if (r.state === "accepted") {
      var m = el("div", "meter");
      var track = el("div", "meter__track");
      var fill = el("div", "meter__fill");
      var pv = (r.progress === null || !isFinite(r.progress)) ? 0 : r.progress;
      fill.style.width = (pv * 100) + "%";
      fill.style.setProperty("--a", agentColor(r.accepted_by || r.to));
      track.appendChild(fill);
      m.appendChild(track);
      var val = el("span", "meter__val", Math.round(pv * 100) + "%");
      m.appendChild(val);
      /* `progress` is optional. An empty track with a dash in it looks like a
         bug rather than like "the provider has not said"; the bar appears the
         moment the first request.progress does. */
      show(m, r.progress !== null);
      card.appendChild(m);
      refs.meter = m; refs.fill = fill; refs.val = val;

      var note = el("p", "req__note", r.note || "");
      show(note, !!r.note);
      card.appendChild(note);
      refs.note = note;
    }

    if (clock) {
      var g = el("div", "gauge" + (clock.level ? " is-" + clock.level : ""));
      var gt = el("div", "gauge__track");
      var gf = el("div", "gauge__fill");
      gf.style.width = (clock.frac * 100) + "%";
      gt.appendChild(gf);
      g.appendChild(gt);
      var gx = el("span", "gauge__txt", clockText(clock, r));
      g.appendChild(gx);
      g.setAttribute("title",
        "Elapsed against the caller's timeout_s of " + fmtDur(r.timeout_s) +
        ". When it runs out the Hub expires the request" +
        (r.state === "accepted" ? " and charges the provider for abandoning it." : "."));
      card.appendChild(g);
      refs.gauge = g; refs.gfill = gf; refs.gtext = gx;
    }

    reqNodes.set(r.id, refs);
    return card;
  }

  function reqTargetChip(r) {
    if (r.to === "any") {
      var any = el("span", "whochip");
      any.appendChild(el("span", "flag flag--sm flag--off"));
      any.appendChild(el("span", "whochip__name", r.accepted_by ? agentName(r.accepted_by) : "anyone"));
      any.setAttribute("title", r.accepted_by
        ? "Offered to anyone who holds this capability; " + agentName(r.accepted_by) + " took it."
        : "Offered to anyone who holds this capability. The first to accept wins.");
      return any;
    }
    var id = r.accepted_by || r.to;
    return whoChip(agentOf(id) || { agent_id: id, name: id || "unknown" }, "sm");
  }

  function reqStateChip(r) {
    var label = r.state;
    if (r.state === "pending" && r.needs_consent) label = "needs consent";
    if (r.state === "expired" && r.abandoned) label = "abandoned";
    var cls = r.state === "expired" && r.abandoned ? "abandoned" : r.state;
    var chip = el("span", "rchip rchip--" + cls);
    chip.appendChild(el("span", "rchip__dot"));
    chip.appendChild(document.createTextNode(label));
    chip.setAttribute("title", reqStateHelp(r));
    return chip;
  }

  function reqStateHelp(r) {
    switch (r.state) {
      case "pending": return "Sent. The provider has not yet accepted or declined it.";
      case "accepted": return "The provider committed to it. Having accepted, it must answer or decline — never go quiet.";
      case "done": return "Answered successfully.";
      case "failed": return "The provider answered and said it did not work. Failing honestly costs nothing in the Ledger.";
      case "declined": return "Refused. Declining is always acceptable and is never a fault.";
      case "cancelled": return "The caller withdrew it.";
      case "expired": return r.abandoned
        ? "Accepted and then never answered. This is the one unforgivable Exchange behaviour, and the only thing in the Ledger that subtracts."
        : "Nobody answered within the caller's timeout_s.";
      default: return r.state;
    }
  }

  /* `reason` is mandatory on every request precisely so the audit trail means
     something, so it is shown, not hidden behind a hover. */
  function reqReason(r) {
    var open = reqReasonOpen.has(r.id);
    var box = el("p", "req__reason");
    box.appendChild(el("b", null, "why: "));
    var span = el("span", null, open || r.reason.length <= 150
      ? r.reason
      : r.reason.slice(0, 150) + "…");
    box.appendChild(span);
    if (r.reason.length > 150) {
      box.appendChild(document.createTextNode(" "));
      var b = el("button", "capmore", open ? "less" : "more");
      b.type = "button";
      b.setAttribute("aria-expanded", open ? "true" : "false");
      b.addEventListener("click", function () {
        if (reqReasonOpen.has(r.id)) reqReasonOpen.delete(r.id); else reqReasonOpen.add(r.id);
        mark("reqs");
      });
      box.appendChild(b);
    }
    return box;
  }

  function renderReqHistory(settled) {
    var sec = $("reqsHistSec"), host = $("reqsHist"), btn = $("reqsMoreBtn");
    clear(host);
    if (!settled.length) { show(sec, false); show(btn, false); return; }
    show(sec, true);

    var shown = reqHistAll ? settled : settled.slice(0, REQ_HISTORY_PAGE);
    shown.forEach(function (r) { host.appendChild(reqHistRow(r)); });

    if (settled.length > REQ_HISTORY_PAGE) {
      btn.textContent = reqHistAll
        ? "Show the latest " + REQ_HISTORY_PAGE
        : "Show all " + settled.length + " settled requests";
      btn.setAttribute("aria-expanded", reqHistAll ? "true" : "false");
      show(btn, true);
    } else {
      show(btn, false);
    }

    /* The Hub keeps the full tally but only ships the most recent records, so
       say so rather than letting the list imply it is everything. */
    var total = terminalTotal();
    if (total > settled.length) {
      host.appendChild(el("p", "note",
        "The Hub has settled " + total + " requests in this parley; it sends the Deck the most " +
        "recent " + settled.length + ". The whole history is in the log."));
    }
  }

  function terminalTotal() {
    var c = S.reqCounts;
    if (!c) return 0;
    var n = 0;
    REQ_TERMINAL.forEach(function (st) {
      var v = c[st];
      if (typeof v === "number" && isFinite(v)) n += v;
    });
    return n;
  }

  function reqHistRow(r) {
    var row = el("div", "reqhist__row");
    row.appendChild(reqStateChip(r));
    var what = el("span", "reqhist__what");
    what.textContent = agentName(r.from) + " → " +
      agentName(r.accepted_by || r.to) + " · " + (r.capability || "free-form");
    row.appendChild(what);

    var why = el("span", "reqhist__why");
    if (r.state === "declined") {
      why.textContent = (r.decline_code ? r.decline_code + " — " : "") + (r.decline_reason || "no reason given");
    } else if (r.state === "failed") {
      why.textContent = (r.error && (r.error.message || r.error.code)) || "no error detail";
    } else if (r.state === "cancelled") {
      why.textContent = r.decline_reason || "withdrawn by the caller";
    } else if (r.state === "expired") {
      why.textContent = r.abandoned
        ? "accepted and never answered — charged against " + agentName(r.accepted_by || r.to) + " in the Ledger"
        : "nobody answered within " + fmtDur(r.timeout_s);
    } else {
      why.textContent = r.output_text || (r.files.length ? r.files.join(", ") : "answered");
    }
    row.appendChild(why);

    var when = el("span", "reqhist__when");
    var end = reqEndMs(r);
    when.textContent = (r.duration_s > 0 ? fmtAge(r.duration_s) + " · " : "") +
      (isFinite(end) ? fmtAge((nowMs() - end) / 1000) + " ago" : "—");
    row.appendChild(when);
    return row;
  }

  function reqClock(r) {
    if (REQ_TERMINAL.indexOf(r.state) >= 0) return null;
    var start = reqStartMs(r), dl = reqDeadlineMs(r);
    if (!isFinite(start) || !isFinite(dl)) return null;
    var now = nowMs();
    var total = Math.max(1000, dl - start);
    var elapsed = Math.max(0, now - start);
    var leftS = (dl - now) / 1000;
    return {
      frac: Math.max(0, Math.min(1, elapsed / total)),
      elapsedS: elapsed / 1000,
      leftS: leftS,
      level: leftS <= 0 ? "crit"
           : (leftS <= 45 || elapsed / total >= 0.9) ? "crit"
           : (leftS <= 120 || elapsed / total >= 0.7) ? "warn" : ""
    };
  }

  function clockText(clock, r) {
    if (clock.leftS <= 0) {
      return "overdue by " + fmtLeft(-clock.leftS) + " · the Hub will expire it";
    }
    return fmtLeft(clock.elapsedS) + " elapsed · " + fmtLeft(clock.leftS) + " left of " + fmtDur(r.timeout_s);
  }

  function fmtLeft(sec) {
    if (!isFinite(sec)) return "—";
    var s = Math.max(0, Math.round(sec));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
    return fmtAge(s);
  }

  /* Targeted repaint: the only nodes a progress burst is allowed to touch. */
  function paintReqProgress() {
    reqProgressDirty.forEach(function (id) {
      var refs = reqNodes.get(id), r = S.reqs.get(id);
      if (!refs || !r) return;
      if (refs.fill) {
        var pv = (r.progress === null || !isFinite(r.progress)) ? 0 : r.progress;
        refs.fill.style.width = (pv * 100) + "%";
        if (refs.val) refs.val.textContent = Math.round(pv * 100) + "%";
        if (refs.meter) show(refs.meter, r.progress !== null);
      }
      if (refs.note) {
        refs.note.textContent = r.note || "";
        show(refs.note, !!r.note);
      }
    });
    reqProgressDirty.clear();
  }

  /* Clocks move on their own. They are repainted from the one-second tick in
     place — never by re-rendering the panel, which would throw away scroll
     position and expanded reasons once a second. */
  function paintReqClocks() {
    reqNodes.forEach(function (refs, id) {
      var r = S.reqs.get(id);
      if (!r || !refs.gauge) return;
      var c = reqClock(r);
      if (!c) return;
      refs.gfill.style.width = (c.frac * 100) + "%";
      refs.gtext.textContent = clockText(c, r);
      refs.gauge.className = "gauge" + (c.level ? " is-" + c.level : "");
      if (refs.row) {
        refs.row.classList.toggle("req--expiring", c.level === "crit");
      }
    });
    var now = nowMs();
    consentClocks.forEach(function (cc) {
      if (!isFinite(cc.deadline)) return;
      var left = (cc.deadline - now) / 1000;
      cc.node.textContent = left > 0
        ? "auto-declines in " + fmtLeft(left)
        : "past its deadline — being auto-declined";
      cc.node.classList.toggle("is-crit", left <= 45);
    });
  }

  /* =============================================== 12d. consent (§15.4)
   * A request is a proposal, not a command, and `ask` means a person decides.
   * The Deck is one of the three places that decision can be made (the CLI and
   * Pigeonhole's pending.json are the others), and it only ever claims to be
   * one of them when it actually holds a host token.
   * ====================================================================== */

  function consentList() {
    var out = [], seen = Object.create(null);
    (S.consentRaw || []).forEach(function (e) {
      if (!e || typeof e !== "object") return;
      var id = str(e.id);
      if (!id || seen[id]) return;
      var rec = S.reqs.get(id) || null;
      if (rec && REQ_TERMINAL.indexOf(rec.state) >= 0) return;   // already answered
      seen[id] = true;
      out.push({
        id: id,
        from: str(e.from) || (rec ? rec.from : ""),
        capability: str(e.capability) || (rec ? rec.capability : ""),
        instruction: str(e.instruction) || (rec ? rec.instruction : ""),
        reason: str(e.reason) || (rec ? rec.reason : ""),
        safety: str(e.safety) || (rec ? rec.safety : "") || "guarded",
        why: str(e.why),
        detail: str(e.detail),
        deadline: consentDeadline(e, rec)
      });
    });
    S.reqs.forEach(function (r) {
      if (seen[r.id] || !r.needs_consent || r.state !== "pending") return;
      seen[r.id] = true;
      out.push({
        id: r.id, from: r.from, capability: r.capability, instruction: r.instruction,
        reason: r.reason, safety: r.safety || "guarded", why: r.why, detail: "",
        deadline: reqDeadlineMs(r)
      });
    });
    out.sort(function (p, q) {
      var dp = isFinite(p.deadline) ? p.deadline : Infinity;
      var dq = isFinite(q.deadline) ? q.deadline : Infinity;
      return dp - dq;
    });
    return out;
  }

  function consentDeadline(e, rec) {
    var d = num(e.deadline_ts);
    if (d > 0) return d * 1000;
    if (rec) return reqDeadlineMs(rec);
    var asked = parseTs(e.asked_at);
    var to = num(e.timeout_s, DEFAULT_TIMEOUT_S) || DEFAULT_TIMEOUT_S;
    return isFinite(asked) ? asked + to * 1000 : NaN;
  }

  function consentPrompt(c) {
    var li = el("li", "attn__item attn__item--consent");
    li.appendChild(el("span", "attn__kind attn__kind--consent", "consent"));

    var box = el("div", "consent");

    var top = el("div", "consent__top");
    top.appendChild(whoChip(agentOf(c.from) || { agent_id: c.from, name: c.from || "unknown" }, "sm"));
    top.appendChild(el("span", "req__arrow", "asks to run"));
    var safety = c.safety || "guarded";
    var skey = safety === "dangerous" ? "danger" : safety === "safe" ? "safe"
             : safety === "guarded" ? "guarded" : "unavail";
    top.appendChild(badgeOf(skey, SAFETY_LABEL[safety] || ("safety: " + safety)));
    var clock = el("span", "gauge__txt");
    top.appendChild(clock);
    consentClocks.push({ node: clock, deadline: c.deadline });
    box.appendChild(top);

    var ask = el("p", "consent__ask");
    if (c.capability) ask.appendChild(el("span", "req__cap", c.capability));
    else ask.appendChild(document.createTextNode(c.instruction || "(a free-form instruction with no text)"));
    box.appendChild(ask);

    var reason = el("p", "req__reason");
    reason.appendChild(el("b", null, "their reason: "));
    reason.appendChild(document.createTextNode(c.reason || "none given"));
    box.appendChild(reason);

    if (c.why || c.detail) {
      var why = el("p", "req__reason");
      why.appendChild(el("b", null, "your policy says: "));
      why.appendChild(document.createTextNode([c.why, c.detail].filter(Boolean).join(" — ")));
      box.appendChild(why);
    }

    var acts = el("div", "consent__acts");
    var ok = el("button", "btn btn--primary", "Accept");
    ok.type = "button";
    var no = el("button", "btn btn--danger", "Decline");
    no.type = "button";
    ok.addEventListener("click", function () { consentAct(c, box, acts, "accept"); });
    no.addEventListener("click", function () { consentAct(c, box, acts, "decline"); });
    acts.appendChild(ok);
    acts.appendChild(no);
    var idc = el("span", "consent__cli", c.id);
    idc.setAttribute("title", "request id — the same decision can be made with “parley accept” or “parley decline” on the machine running that agent");
    acts.appendChild(idc);
    box.appendChild(acts);

    li.appendChild(box);
    return li;
  }

  function consentAct(c, box, acts, what) {
    Array.prototype.forEach.call(acts.querySelectorAll("button"), function (b) { b.disabled = true; });
    var body = what === "accept"
      ? { id: c.id }
      : { id: c.id, reason: "declined by the host from the Deck", code: "policy" };
    adminPost("/v1/admin/request-" + what, body)
      .then(function () {
        var rec = S.reqs.get(c.id);
        if (rec) {
          rec.needs_consent = false;
          if (what === "decline" && REQ_TERMINAL.indexOf(rec.state) < 0) {
            rec.state = "declined";
            rec.decline_code = "policy";
            rec.decline_reason = "declined by the host from the Deck";
            rec.terminal_at = new Date(nowMs()).toISOString();
          }
        }
        S.consentRaw = (S.consentRaw || []).filter(function (e) { return str(e.id) !== c.id; });
        mark("reqs"); mark("roster");
        scheduleStateRefresh();
      })
      .catch(function (err) {
        /* The Hub may simply not expose this action. Say so, and hand over the
           command that definitely works instead of leaving a dead button. */
        var old = box.querySelector(".consent__err");
        if (old) old.remove();
        box.appendChild(el("p", "consent__err",
          "The Hub would not take that from the Deck (" + (err && err.message ? err.message : "no reason given") +
          "). Answer it on the machine running that agent instead:"));
        box.appendChild(el("p", "consent__cli", what === "accept"
          ? "parley accept " + c.id
          : "parley decline " + c.id + " --reason \"…\""));
      });
  }

  function renderConsentNote() {
    var sec = $("reqsConsentSec"), note = $("reqsConsentNote");
    var n = consentList().length;
    if (!n || HST) { show(sec, false); return; }
    clear(note);
    note.appendChild(el("b", null,
      n + (n === 1 ? " request is" : " requests are") + " waiting on an operator's consent."));
    note.appendChild(document.createTextNode(
      " This Deck has no host token, so it can only show them. Answer on the machine running that agent: "));
    note.appendChild(el("code", null, "parley requests --pending"));
    note.appendChild(document.createTextNode(", then "));
    note.appendChild(el("code", null, "parley accept <id>"));
    note.appendChild(document.createTextNode(" or "));
    note.appendChild(el("code", null, "parley decline <id> --reason \"…\""));
    note.appendChild(document.createTextNode(". An unanswered ask is auto-declined when its timeout runs out."));
    show(sec, true);
  }

  /* ============================================================= 13. ledger */

  var openLedgerRows = new Set();
  var ledgerRowBars = [];

  function renderLedger() {
    var host = $("ledger");
    clear(host);
    ledgerRowBars = [];
    var L = S.ledger;
    var lines = (L && Array.isArray(L.lines)) ? L.lines.slice() : [];
    if (!lines.length) {
      host.appendChild(el("p", "empty", "No contributions recorded yet. The Ledger fills as agents file knowledge, finish tasks and write files."));
      $("ledgerMeta").textContent = "";
      return;
    }
    lines.sort(function (a, b) { return (b.total || 0) - (a.total || 0); });
    /* Scale on *gross* earned points, not on the net total. `service` is the one
       component that can go negative (§15.5's abandoned-request penalty), and a
       bar scaled on a net total has nowhere to draw the part that was taken
       back. Gross gives the deduction a length. */
    var anyPenalty = false;
    var max = 0;
    lines.forEach(function (l) {
      var p = ledgerParts(l);
      if (p.gross > max) max = p.gross;
      if (p.penalty > 0) anyPenalty = true;
    });
    if (!(max > 0)) max = 1;
    var sum = lines.reduce(function (s, l) { return s + (l.total || 0); }, 0);

    lines.forEach(function (line, i) {
      host.appendChild(ledgerRow(line, max, sum, i));
    });
    show($("ledgerLegendPenalty"), anyPenalty);
    $("ledgerMeta").textContent = fmtNum(sum, 1) + " pts · " +
      (L && L.event_count ? L.event_count + " events" : "");
    layoutLedgerBars();
  }

  /* Split a Ledger line into what it earned and what was taken back. */
  function ledgerParts(line) {
    var comps = (line && line.components) || {};
    var pos = [], penalty = 0;
    LEDGER_COMPONENTS.forEach(function (c) {
      var v = comps[c[0]];
      v = (typeof v === "number" && isFinite(v)) ? v : 0;
      pos.push(v > 0 ? v : 0);
      if (v < 0) penalty += -v;
    });
    var gross = pos.reduce(function (a, b) { return a + b; }, 0);
    return { pos: pos, gross: gross, penalty: penalty, net: gross - penalty };
  }

  /* A true minus sign, and never a bare "-5.00" that reads as a hyphen. */
  function fmtSigned(v, dp) {
    if (!isFinite(v)) return "—";
    var s = fmtNum(Math.abs(v), dp);
    return (v < 0 ? "−" : "") + s;
  }

  function ledgerRow(line, max, sum, index) {
    var wrap = el("div", "lrow");
    var open = openLedgerRows.has(line.agent_id);
    if (open) wrap.classList.add("is-open");

    var btn = el("button", "lrow__btn");
    btn.type = "button";
    btn.setAttribute("aria-expanded", open ? "true" : "false");

    var who = el("div", "lrow__who");
    var ag = agentOf(line.agent_id);
    who.appendChild(flagFor(ag || { agent_id: line.agent_id, name: line.name }, "sm"));
    who.appendChild(el("span", "lrow__name", line.name || agentName(line.agent_id)));
    btn.appendChild(who);

    var barBox = el("div", "lrow__bar");
    var svg = sv("svg", { height: 18, width: "100%", "aria-hidden": "true", focusable: "false" });
    barBox.appendChild(svg);
    btn.appendChild(barBox);
    ledgerRowBars.push({ svg: svg, line: line, max: max, index: index || 0 });

    var parts = ledgerParts(line);
    var fig = el("div", "lrow__fig");
    var pts = el("span", "lrow__pts" + ((line.total || 0) < 0 ? " is-negative" : ""), fmtSigned(line.total, 1));
    if (parts.penalty > 0) {
      pts.setAttribute("title", fmtNum(parts.gross, 2) + " earned, " +
        fmtNum(parts.penalty, 2) + " deducted for requests accepted and never answered.");
    }
    fig.appendChild(pts);
    fig.appendChild(el("span", "lrow__share",
      (isFinite(line.share) ? Math.round(line.share * 100) : Math.round((line.total / (sum || 1)) * 100)) + "%"));
    fig.appendChild(el("span", "lrow__caret", open ? "▾" : "▸"));
    btn.appendChild(fig);

    btn.addEventListener("click", function () {
      if (openLedgerRows.has(line.agent_id)) openLedgerRows.delete(line.agent_id);
      else openLedgerRows.add(line.agent_id);
      renderLedger();
    });
    wrap.appendChild(btn);

    if (open) wrap.appendChild(ledgerWhy(line));
    return wrap;
  }

  function ledgerWhy(line) {
    var box = el("div", "lwhy");
    var comps = line.components || {};
    var grid = el("div", "lwhy__grid");
    LEDGER_COMPONENTS.forEach(function (c, i) {
      var v = comps[c[0]];
      v = (typeof v === "number" && isFinite(v)) ? v : 0;
      var cell = el("div", "lcomp" + (v < 0 ? " is-negative" : ""));
      var k = el("div", "lcomp__k");
      k.appendChild(el("span", "sw lc-" + (i + 1)));
      k.appendChild(document.createTextNode(c[1]));
      cell.appendChild(k);
      cell.appendChild(el("div", "lcomp__v", fmtSigned(v, 2)));
      cell.appendChild(el("div", "lcomp__n", c[2]));
      grid.appendChild(cell);
    });
    box.appendChild(grid);

    var ev = line.evidence || {};
    var rows = [], penalties = [];
    LEDGER_COMPONENTS.forEach(function (c, i) {
      (ev[c[0]] || []).forEach(function (e) {
        var row = { comp: c[1], ci: i + 1, seq: e.seq, label: e.label, points: e.points };
        if ((e.points || 0) < 0) penalties.push(row); else rows.push(row);
      });
    });
    rows.sort(function (a, b) { return (b.points || 0) - (a.points || 0); });
    /* Deductions go first and are never cut by the 60-row window. A point the
       user cannot trace violates R6; a *penalty* they cannot trace violates it
       harder, because it is the one number that reads as an accusation. */
    penalties.sort(function (a, b) { return (a.points || 0) - (b.points || 0); });
    rows = penalties.concat(rows);

    if (!rows.length) {
      box.appendChild(el("p", "note", "The Hub did not send per-event evidence for this agent. The component totals above are still derived from the log."));
      return box;
    }

    var table = el("table", "evtable");
    var thead = el("thead");
    var hr = el("tr");
    [["Seq", ""], ["Component", ""], ["Event", ""], ["Pts", "num"]].forEach(function (h) {
      var th = el("th", h[1], h[0]); th.setAttribute("scope", "col"); hr.appendChild(th);
    });
    thead.appendChild(hr); table.appendChild(thead);
    var tb = el("tbody");
    rows.slice(0, 60).forEach(function (r) {
      var neg = (r.points || 0) < 0;
      var tr = el("tr", neg ? "is-penalty" : "");
      tr.appendChild(el("td", "seq", isFinite(r.seq) ? "#" + r.seq : "—"));
      var td = el("td", "cmp");
      td.appendChild(el("span", neg ? "sw sw--penalty" : "sw lc-" + r.ci));
      td.appendChild(document.createTextNode(" " + r.comp));
      tr.appendChild(td);
      tr.appendChild(el("td", "lbl", String(r.label === undefined ? "" : r.label)));
      tr.appendChild(el("td", "num", fmtSigned(r.points, 2)));
      tb.appendChild(tr);
    });
    table.appendChild(tb);
    box.appendChild(table);
    if (rows.length > 60) box.appendChild(el("p", "note", "Showing the 60 highest-scoring of " + rows.length + " events."));
    return box;
  }

  /* Stacked bar: 2px surface gaps between segments, square at the baseline,
     4px rounded data-end at the tip. Drawn at measured pixel width. */
  function layoutLedgerBars() {
    ledgerRowBars.forEach(function (b) {
      var w = b.svg.clientWidth || b.svg.parentNode.clientWidth || 200;
      drawLedgerBar(b.svg, b.line, b.max, w, b.index);
    });
  }

  function roundPath(x, y, w, h, rTopRight, rBotRight) {
    if (w <= 0) return "";
    var r = Math.min(rTopRight, w, h / 2), r2 = Math.min(rBotRight, w, h / 2);
    return "M" + x + "," + y +
      "H" + (x + w - r) + (r ? "a" + r + "," + r + " 0 0 1 " + r + "," + r : "") +
      "V" + (y + h - r2) + (r2 ? "a" + r2 + "," + r2 + " 0 0 1 " + (-r2) + "," + r2 : "") +
      "H" + x + "Z";
  }

  function drawLedgerBar(svg, line, max, width, index) {
    clear(svg);
    var H = 18, GAP = 2;
    var parts = ledgerParts(line);
    var span = Math.max(0, width - 2);
    var grossW = Math.max(0, (parts.gross / (max || 1)) * span);

    /* Track: one hairline so an empty bar is still legible. */
    svg.appendChild(sv("rect", { x: 0, y: H / 2 - 0.5, width: span, height: 1, fill: "currentColor", opacity: 0.08 }));

    if (!parts.gross || grossW <= 0) {
      svg.appendChild(sv("rect", { x: 0, y: 4, width: 2, height: H - 8, rx: 1, fill: "currentColor", opacity: 0.25 }));
      return;
    }
    var nonEmpty = parts.pos.filter(function (v) { return v > 0; }).length;
    var usable = Math.max(1, grossW - GAP * Math.max(0, nonEmpty - 1));
    var x = 0, drawn = 0;
    parts.pos.forEach(function (v, i) {
      if (v <= 0) return;
      drawn++;
      var w = usable * (v / parts.gross);
      var isLast = drawn === nonEmpty;
      var p = sv("path", { d: roundPath(x, 0, w, H, isLast ? 4 : 0, isLast ? 4 : 0) });
      p.setAttribute("class", "lfill lfill--" + (i + 1));
      var ttl = sv("title", {});
      ttl.textContent = LEDGER_COMPONENTS[i][1] + ": " + fmtNum(v, 2) + " pts";
      p.appendChild(ttl);
      svg.appendChild(p);
      x += w + GAP;
    });

    if (parts.penalty > 0) drawLedgerPenalty(svg, parts, grossW, H, index);
  }

  /* The deduction. Drawn as the struck-through tail of the same bar: the solid
     part ends where the agent actually stands, the hatched tail is what was
     taken back, and a rule marks the boundary. It is deliberately impossible to
     mistake for a seventh component — it has no hue of its own, only texture in
     the reserved critical colour. */
  function drawLedgerPenalty(svg, parts, grossW, H, index) {
    var netW = Math.max(0, grossW * (parts.net / parts.gross));
    var patId = "lpen-" + (index || 0);

    var defs = sv("defs");
    var pat = sv("pattern", {
      id: patId, width: 6, height: 6,
      patternUnits: "userSpaceOnUse", patternTransform: "rotate(135)"
    });
    var ln = sv("line", { x1: 0, y1: 0, x2: 0, y2: 6 });
    ln.setAttribute("class", "lpen-line");
    pat.appendChild(ln);
    defs.appendChild(pat);
    svg.appendChild(defs);

    var wash = sv("rect", { x: netW, y: 0, width: Math.max(0, grossW - netW), height: H, rx: 2 });
    wash.setAttribute("class", "lpen-wash");
    svg.appendChild(wash);

    var hatch = sv("rect", { x: netW, y: 0, width: Math.max(0, grossW - netW), height: H, rx: 2 });
    hatch.setAttribute("fill", "url(#" + patId + ")");
    svg.appendChild(hatch);

    var edge = sv("line", { x1: netW, y1: 0, x2: netW, y2: H });
    edge.setAttribute("class", "lpen-edge");
    svg.appendChild(edge);

    var ttl = sv("title", {});
    ttl.textContent = "Deducted: " + fmtNum(parts.penalty, 2) +
      " pts for requests accepted and never answered. " +
      fmtNum(parts.gross, 2) + " earned, " + fmtNum(parts.net, 2) + " net.";
    hatch.appendChild(ttl);
  }

  /* ================================================== 14. collaboration graph
   * A settled, deterministic circular layout. Nodes are ordered by join time
   * (stable for the life of the parley), evenly spaced, and edges are chords
   * bowed toward the centre. Nothing animates, nothing iterates, nothing
   * jitters, and the layout is identical on every participant's Deck. A force
   * simulation would look busier and tell you less.
   * ====================================================================== */

  function renderGraph() {
    var svg = $("graphSvg");
    clear(svg);
    var ttl = sv("title", { id: "graphDesc" });
    ttl.textContent = "Which agents are working together";
    svg.appendChild(ttl);
    svg.classList.remove("is-focused");

    var g = S.graph || { nodes: [], edges: [] };
    var nodes = (g.nodes || []).filter(function (n) { return n && n.id; });
    var edges = (g.edges || []).filter(function (e) { return e && e.source && e.target; });

    var tbody = $("graphTable").querySelector("tbody");
    clear(tbody);

    if (nodes.length < 2) {
      $("graphMeta").textContent = "";
      $("graphEmpty").textContent = nodes.length
        ? "Only one participant so far — there is nothing to link yet."
        : "No collaboration recorded yet.";
      show($("graphEmpty"), true);
      show($("graphScroll"), false);
      return;
    }
    show($("graphEmpty"), false);
    show($("graphScroll"), true);

    /* stable order: join time, then id */
    nodes.sort(function (a, b) {
      var aa = agentOf(a.id), ab = agentOf(b.id);
      var ta = aa && aa.joined ? parseTs(aa.joined) : NaN;
      var tb = ab && ab.joined ? parseTs(ab.joined) : NaN;
      if (isFinite(ta) && isFinite(tb) && ta !== tb) return ta - tb;
      return String(a.id).localeCompare(String(b.id));
    });

    var W = 520, H = 440, cx = W / 2, cy = H / 2;
    var R = nodes.length <= 4 ? 118 : nodes.length <= 8 ? 136 : 150;
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);

    var pos = {}, i;
    for (i = 0; i < nodes.length; i++) {
      var ang = -Math.PI / 2 + (i * 2 * Math.PI / nodes.length);
      pos[nodes[i].id] = { x: cx + R * Math.cos(ang), y: cy + R * Math.sin(ang), a: ang };
    }

    var defs = sv("defs");
    var marker = sv("marker", {
      id: "pg-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5,
      markerWidth: 5, markerHeight: 5, orient: "auto-start-reverse"
    });
    var ap = sv("path", { d: "M0,1 L9,5 L0,9 z" });
    ap.setAttribute("class", "garrow");
    marker.appendChild(ap);
    defs.appendChild(marker);

    var maxW = 1, maxDel = 0, deg = {};
    edges.forEach(function (e) {
      var w = isFinite(e.weight) ? e.weight : 1;
      if (w > maxW) maxW = w;
      var d = (e.kinds && e.kinds.delegation) || 0;
      if (d > maxDel) maxDel = d;
      deg[e.source] = (deg[e.source] || 0) + w;
      deg[e.target] = (deg[e.target] || 0) + w;
    });
    var maxDeg = Math.max.apply(null, nodes.map(function (n) { return deg[n.id] || 0; })) || 1;

    var edgeLayer = sv("g"), hitLayer = sv("g"), nodeLayer = sv("g", { role: "list" });
    var edgesByNode = {};

    edges.sort(function (a, b) { return (a.weight || 0) - (b.weight || 0); });
    edges.forEach(function (e, idx) {
      var A = pos[e.source], B = pos[e.target];
      if (!A || !B) return;
      var kinds = e.kinds || {};
      var blocked = (kinds.blocked_on || 0) > 0;
      var w = isFinite(e.weight) ? e.weight : 1;
      var sw = 1 + 5 * Math.sqrt(Math.max(0, w) / maxW);

      var mx = (A.x + B.x) / 2, my = (A.y + B.y) / 2;
      var qx = cx + (mx - cx) * 0.34, qy = cy + (my - cy) * 0.34;
      var d = "M" + A.x.toFixed(1) + "," + A.y.toFixed(1) +
              " Q" + qx.toFixed(1) + "," + qy.toFixed(1) +
              " " + B.x.toFixed(1) + "," + B.y.toFixed(1);

      var path = sv("path", { d: d, "stroke-width": sw.toFixed(2) });
      path.setAttribute("class", "gedge" + (blocked ? " gedge--blocked" : ""));
      if (blocked) {
        path.setAttribute("marker-end", "url(#pg-arrow)");
        path.setAttribute("stroke-dasharray", "1 0");
      } else {
        var gid = "pg-grad-" + idx;
        var grad = sv("linearGradient", {
          id: gid, gradientUnits: "userSpaceOnUse",
          x1: A.x.toFixed(1), y1: A.y.toFixed(1), x2: B.x.toFixed(1), y2: B.y.toFixed(1)
        });
        var s1 = sv("stop", { offset: "0%" }); s1.setAttribute("stop-color", agentColor(e.source));
        var s2 = sv("stop", { offset: "100%" }); s2.setAttribute("stop-color", agentColor(e.target));
        grad.appendChild(s1); grad.appendChild(s2);
        defs.appendChild(grad);
        path.setAttribute("stroke", "url(#" + gid + ")");
        path.setAttribute("opacity", "0.72");
      }
      edgeLayer.appendChild(path);

      /* Delegation rides inside the chord instead of replacing it. The chord is
         how much these two work together; the filament is how much of that was
         one of them doing work the other asked for. Same settled layout, one
         more fact — and it is also in the table below, so it is never only a
         hairline you have to notice. */
      var del = kinds.delegation || 0;
      if (del > 0) {
        var fw = 1 + 2.6 * Math.sqrt(del / (maxDel || 1));
        var fil = sv("path", { d: d, "stroke-width": Math.min(fw, Math.max(1, sw - 0.8)).toFixed(2) });
        fil.setAttribute("class", "gfil");
        edgeLayer.appendChild(fil);
        (edgesByNode[e.source] = edgesByNode[e.source] || []).push(fil);
        (edgesByNode[e.target] = edgesByNode[e.target] || []).push(fil);
      }

      var hit = sv("path", { d: d, "stroke-width": Math.max(14, sw + 10), stroke: "transparent", fill: "none" });
      hit.setAttribute("class", "ghit");
      hit.__tip = edgeTip(e, blocked);
      hitLayer.appendChild(hit);

      (edgesByNode[e.source] = edgesByNode[e.source] || []).push(path);
      (edgesByNode[e.target] = edgesByNode[e.target] || []).push(path);

      var tr = el("tr");
      tr.appendChild(el("td", null, agentName(e.source) + " ↔ " + agentName(e.target)));
      tr.appendChild(el("td", null, fmtNum(w, 0)));
      tr.appendChild(el("td", null, kindSummary(kinds)));
      tbody.appendChild(tr);
    });

    nodes.forEach(function (n) {
      var P = pos[n.id];
      var a = agentOf(n.id);
      var r = 6 + 7 * Math.sqrt((deg[n.id] || 0) / maxDeg);
      var grp = sv("g", { tabindex: "0", role: "listitem" });
      grp.setAttribute("class", "gnode");
      grp.setAttribute("aria-label", (a ? a.name : n.label || n.id) + ", " +
        fmtNum(deg[n.id] || 0, 0) + " units of shared work");

      var disc = sv("circle", { cx: P.x.toFixed(1), cy: P.y.toFixed(1), r: r.toFixed(1) });
      disc.setAttribute("class", "gnode__disc");
      disc.setAttribute("fill", agentColor(n.id));
      grp.appendChild(disc);

      var right = Math.cos(P.a) > -0.01;
      var lx = P.x + (right ? r + 7 : -(r + 7));
      var label = sv("text", { x: lx.toFixed(1), y: (P.y + 3).toFixed(1) });
      label.setAttribute("class", "gnode__label");
      label.setAttribute("text-anchor", right ? "start" : "end");
      var nm = String((a && a.name) || n.label || n.id);
      label.textContent = nm.length > 16 ? nm.slice(0, 15) + "…" : nm;
      grp.appendChild(label);

      var sub = sv("text", { x: lx.toFixed(1), y: (P.y + 14).toFixed(1) });
      sub.setAttribute("class", "gnode__sub");
      sub.setAttribute("text-anchor", right ? "start" : "end");
      sub.textContent = psrState(a);
      grp.appendChild(sub);

      grp.__tip = { title: nm, rows: [["state", psrState(a)], ["shared work", fmtNum(deg[n.id] || 0, 0)]] };
      grp.__lit = (edgesByNode[n.id] || []);
      grp.__node = true;
      grp.addEventListener("pointerenter", function () { litOn(svg, grp); });
      grp.addEventListener("pointerleave", function () { litOff(svg); });
      grp.addEventListener("focus", function () { litOn(svg, grp); showTipAt(grp.__tip, P.x, P.y, svg); });
      grp.addEventListener("blur", function () { litOff(svg); hideTip(); });
      nodeLayer.appendChild(grp);
    });

    /* Edge hit targets sit under the nodes: a node must always win the pointer
       where an edge passes through it. */
    svg.appendChild(defs);
    svg.appendChild(edgeLayer);
    svg.appendChild(hitLayer);
    svg.appendChild(nodeLayer);

    $("graphMeta").textContent = nodes.length + " agents · " + edges.length + " links";
  }

  function kindSummary(k) {
    var out = [];
    if (k.reply) out.push(k.reply + " replies");
    if (k.citation) out.push(k.citation + " citations");
    if (k.co_edit) out.push(k.co_edit + " co-edits");
    if (k.delegation) out.push(k.delegation + " delegated");
    if (k.blocked_on) out.push(k.blocked_on + " blocked-on");
    return out.join(", ") || "—";
  }
  function edgeTip(e, blocked) {
    var k = e.kinds || {};
    return {
      title: agentName(e.source) + (blocked ? " → " : " ↔ ") + agentName(e.target),
      rows: [
        ["weight", fmtNum(e.weight, 0)],
        ["replies", String(k.reply || 0)],
        ["citations", String(k.citation || 0)],
        ["co-edited files", String(k.co_edit || 0)],
        ["delegated requests", String(k.delegation || 0)],
        ["blocked on", String(k.blocked_on || 0)]
      ]
    };
  }
  function litOn(svg, grp) {
    svg.classList.add("is-focused");
    grp.classList.add("is-lit");
    (grp.__lit || []).forEach(function (p) { p.classList.add("is-lit"); });
  }
  function litOff(svg) {
    svg.classList.remove("is-focused");
    Array.prototype.forEach.call(svg.querySelectorAll(".is-lit"), function (n) { n.classList.remove("is-lit"); });
  }

  /* ============================================================== 15. tasks */

  function renderTasks() {
    var board = $("board");
    clear(board);
    var all = Array.from(S.tasks.values());
    if (!all.length) {
      board.appendChild(el("p", "empty", "No tasks on the board. Agents create them with “parley task create”."));
      $("tasksMeta").textContent = "";
      return;
    }
    var open = all.filter(function (t) { return t.status !== "done"; }).length;
    $("tasksMeta").textContent = open + " open / " + all.length;

    TASK_LANES.forEach(function (ln) {
      var lane = el("div", "lane lane--" + ln[0]);
      var head = el("div", "lane__head");
      head.appendChild(document.createTextNode(ln[1]));
      var items = all.filter(function (t) { return (t.status || "todo") === ln[0]; });
      head.appendChild(el("span", "lane__n", String(items.length)));
      lane.appendChild(head);
      if (!items.length) {
        lane.appendChild(el("p", "task__meta", "—"));
      }
      items.sort(function (a, b) { return (a.priority || 3) - (b.priority || 3); });
      items.forEach(function (t) { lane.appendChild(taskCard(t)); });
      board.appendChild(lane);
    });
  }

  function taskCard(t) {
    var c = el("div", "task task--" + (t.status || "todo"));
    c.appendChild(el("div", "task__title", String(t.title || t.id || "untitled")));
    var meta = el("div", "task__meta");
    if (isFinite(t.priority)) meta.appendChild(el("span", "task__pri task__pri--" + t.priority, "p" + t.priority));
    meta.appendChild(el("span", null, String(t.id || "")));
    c.appendChild(meta);
    if (t.claimed_by) {
      var cl = el("div", "task__meta");
      cl.appendChild(whoChip(agentOf(t.claimed_by) || { agent_id: t.claimed_by, name: t.claimed_by }, "sm"));
      c.appendChild(cl);
    }
    if (isFinite(t.progress) && t.progress > 0 && t.status !== "done") {
      var m = el("div", "meter");
      var tr = el("div", "meter__track"), f = el("div", "meter__fill");
      f.style.width = Math.max(0, Math.min(1, t.progress)) * 100 + "%";
      if (t.claimed_by) f.style.setProperty("--a", agentColor(t.claimed_by));
      tr.appendChild(f); m.appendChild(tr);
      m.appendChild(el("span", "meter__val", Math.round(t.progress * 100) + "%"));
      c.appendChild(m);
    }
    if (Array.isArray(t.tags) && t.tags.length) {
      var tg = el("div", "tags");
      t.tags.slice(0, 5).forEach(function (x) { tg.appendChild(el("span", "tag", String(x))); });
      c.appendChild(tg);
    }
    return c;
  }

  /* =========================================================== 16. timeline */

  var tlWindow = 3600;    // seconds; 0 = all

  function renderTimeline() {
    var svg = $("tlSvg");
    clear(svg);
    var ttl = sv("title", { id: "tlDesc" });
    ttl.textContent = "Activity swimlane per agent, coloured by standing-report state";
    svg.appendChild(ttl);

    var ids = S.order.filter(function (id) { return S.agents.has(id); });
    S.agents.forEach(function (a, id) { if (ids.indexOf(id) < 0) ids.push(id); });
    if (!ids.length) {
      svg.setAttribute("width", 680); svg.setAttribute("height", 40);
      $("tlNote").textContent = "";
      return;
    }

    var t1 = nowMs();
    var earliest = t1;
    S.tl.lanes.forEach(function (pts) { if (pts.length && pts[0].t < earliest) earliest = pts[0].t; });
    S.tl.marks.forEach(function (m) { if (m.t < earliest) earliest = m.t; });
    if (isFinite(S.hubStarted) && S.hubStarted < earliest) earliest = S.hubStarted;
    var t0 = tlWindow ? t1 - tlWindow * 1000 : earliest;
    if (t0 > earliest && !tlWindow) t0 = earliest;
    if (t1 - t0 < 60000) t0 = t1 - 60000;

    var GUT = 88, PAD_R = 14, LANE = 18, GAP = 5, TOP = 6, AXIS = 20;
    var box = $("tlScroll").clientWidth || 700;
    var W = Math.max(680, box);
    var H = TOP + ids.length * (LANE + GAP) + AXIS;
    svg.setAttribute("width", W); svg.setAttribute("height", H);
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);

    var plotW = W - GUT - PAD_R;
    function X(t) { return GUT + Math.max(0, Math.min(1, (t - t0) / (t1 - t0))) * plotW; }

    /* hatch for the "no standing report" / unknown region */
    var defs = sv("defs");
    var pat = sv("pattern", { id: "hatch-unknown", width: 7, height: 7, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" });
    var bg = sv("rect", { width: 7, height: 7 }); bg.setAttribute("class", "hatch-bg");
    var ln = sv("line", { x1: 0, y1: 0, x2: 0, y2: 7 }); ln.setAttribute("class", "hatch-line");
    pat.appendChild(bg); pat.appendChild(ln); defs.appendChild(pat);
    svg.appendChild(defs);

    /* grid + ticks */
    var span = (t1 - t0) / 1000;
    var step = span <= 1200 ? 300 : span <= 7200 ? 900 : span <= 43200 ? 3600 : span <= 172800 ? 21600 : 86400;
    var gridG = sv("g");
    var firstTick = Math.ceil(t0 / (step * 1000)) * step * 1000;
    for (var tt = firstTick; tt <= t1; tt += step * 1000) {
      var gx = X(tt);
      var gl = sv("line", { x1: gx, y1: TOP, x2: gx, y2: H - AXIS });
      gl.setAttribute("class", "tl-grid"); gridG.appendChild(gl);
      var lab = sv("text", { x: gx, y: H - 6, "text-anchor": "middle" });
      lab.setAttribute("class", "tl-tick");
      lab.textContent = clockOf(tt);
      gridG.appendChild(lab);
    }
    svg.appendChild(gridG);

    var axis = sv("line", { x1: GUT, y1: H - AXIS, x2: W - PAD_R, y2: H - AXIS });
    axis.setAttribute("class", "tl-axis"); svg.appendChild(axis);

    var present = {};
    ids.forEach(function (id, row) {
      var a = S.agents.get(id);
      var y = TOP + row * (LANE + GAP);

      var bgr = sv("rect", { x: GUT, y: y, width: plotW, height: LANE, rx: 3 });
      bgr.setAttribute("class", "tl-lane-bg");
      svg.appendChild(bgr);

      var nameT = sv("text", { x: GUT - 8, y: y + LANE / 2 + 4, "text-anchor": "end" });
      nameT.setAttribute("class", "tl-name");
      var nm = String(a.name || id);
      nameT.textContent = nm.length > 13 ? nm.slice(0, 12) + "…" : nm;
      svg.appendChild(nameT);

      var pts = (S.tl.lanes.get(id) || []).slice();
      /* close the lane with the live state */
      var segs = [];
      if (!pts.length) {
        segs.push({ a: t0, b: t1, state: "unknown" });
      } else {
        if (pts[0].t > t0) segs.push({ a: t0, b: Math.min(pts[0].t, t1), state: "unknown" });
        for (var i = 0; i < pts.length; i++) {
          var sA = Math.max(pts[i].t, t0);
          var sB = i + 1 < pts.length ? pts[i + 1].t : t1;
          if (sB <= t0 || sA >= t1) continue;
          segs.push({ a: sA, b: Math.min(sB, t1), state: pts[i].state });
        }
      }
      segs.forEach(function (s) {
        var x = X(s.a), w = Math.max(1.5, X(s.b) - X(s.a));
        var r = sv("rect", { x: x.toFixed(1), y: y, width: w.toFixed(1), height: LANE, rx: 2 });
        r.setAttribute("class", "tl-seg tl-seg--" + s.state);
        present[s.state] = true;
        r.__tip = {
          title: a.name, rows: [
            ["state", s.state === "unknown" ? "no report on file" : s.state],
            ["from", clockOf(s.a)],
            ["for", fmtAge((s.b - s.a) / 1000)]
          ]
        };
        svg.appendChild(r);
      });
    });

    /* markers */
    var marks = S.tl.marks.filter(function (m) { return m.t >= t0 && m.t <= t1; });
    if (marks.length > 900) marks = marks.slice(marks.length - 900);
    marks.forEach(function (m) {
      var row = ids.indexOf(m.agent);
      if (row < 0) return;
      var y = TOP + row * (LANE + GAP);
      var x = X(m.t);
      var node;
      if (m.kind === "put") {
        node = sv("line", { x1: x.toFixed(1), y1: y + LANE - 5, x2: x.toFixed(1), y2: y + LANE - 1 });
        node.setAttribute("class", "tl-mark-put");
      } else if (m.kind === "conflict") {
        node = sv("path", { d: diamond(x, y + LANE / 2, 4.5) });
        node.setAttribute("class", "tl-mark-conflict");
        present.__conflict = true;
      } else {
        node = sv("circle", { cx: x.toFixed(1), cy: y + LANE / 2, r: 3.5 });
        node.setAttribute("class", "tl-mark-done");
        present.__done = true;
      }
      node.__tip = { title: m.kind === "put" ? "file write" : m.kind === "conflict" ? "sync conflict" : "task done", rows: [["", m.label], ["at", clockOf(m.t)]] };
      svg.appendChild(node);
    });

    var nowX = X(t1);
    var nl = sv("line", { x1: nowX, y1: TOP - 4, x2: nowX, y2: H - AXIS });
    nl.setAttribute("class", "tl-now"); svg.appendChild(nl);
    var nlab = sv("text", { x: nowX - 4, y: TOP + 7, "text-anchor": "end" });
    nlab.setAttribute("class", "tl-now-lbl"); nlab.textContent = "now";
    svg.appendChild(nlab);

    renderTlLegend(present);
    $("tlNote").textContent = "Window: " + (tlWindow ? fmtDur(tlWindow) : "whole parley") +
      ". Hatched means the agent had no standing report on file for that stretch — the Deck " +
      "cannot invent one. History before the Deck connected is backfilled from the Hub's log where the Hub allows it.";
  }

  function diamond(x, y, r) {
    return "M" + x + "," + (y - r) + "L" + (x + r) + "," + y + "L" + x + "," + (y + r) + "L" + (x - r) + "," + y + "Z";
  }

  function renderTlLegend(present) {
    var box = $("tlLegend");
    clear(box);
    PSR_STATES.concat(["unknown"]).forEach(function (st) {
      if (!present[st]) return;
      var li = el("li");
      li.appendChild(el("span", "sw sw-st-" + st));
      li.appendChild(document.createTextNode(st === "unknown" ? "no report" : st));
      box.appendChild(li);
    });
    if (present.__conflict) {
      var c = el("li");
      c.appendChild(el("span", "sw sw-crit"));
      c.appendChild(document.createTextNode("conflict")); box.appendChild(c);
    }
    if (present.__done) {
      var d = el("li");
      d.appendChild(el("span", "sw sw--dot sw-good"));
      d.appendChild(document.createTextNode("task done")); box.appendChild(d);
    }
  }

  /* ========================================================== 17. workspace */

  function renderHeat() {
    var host = $("heat");
    clear(host);
    var entries = Object.keys(S.files.heat || {}).map(function (p) { return [p, S.files.heat[p]]; });
    if (!entries.length) {
      host.appendChild(el("p", "empty", "Nothing written yet."));
      show($("heatScale"), false);
      return;
    }
    show($("heatScale"), true);
    entries.sort(function (a, b) { return b[1] - a[1]; });
    var max = entries[0][1] || 1;
    entries.slice(0, 96).forEach(function (e) {
      var bucket = Math.max(1, Math.min(6, Math.ceil((e[1] / max) * 6)));
      var cell = el("button", "heat__cell h-" + bucket);
      cell.type = "button";
      cell.setAttribute("aria-label", e[0] + " — heat " + e[1]);
      cell.__tip = { title: e[0], rows: [["heat", String(e[1])]] };
      host.appendChild(cell);
    });
  }

  function renderFiles() {
    var tb = $("fileTable").querySelector("tbody");
    clear(tb);
    var conflictPaths = new Map();
    S.files.conflicts.forEach(function (c) { conflictPaths.set(c.path, c); });

    $("filesMeta").textContent = (S.files.count || 0) + " files · " + fmtBytes(S.files.bytes || 0) +
      (S.files.conflicts.length ? " · " + S.files.conflicts.length + " conflict" + (S.files.conflicts.length > 1 ? "s" : "") : "");

    if (!S.files.recent.length) {
      var tr0 = el("tr");
      var td0 = el("td", "empty", "No file writes recorded yet.");
      td0.setAttribute("colspan", "5");
      tr0.appendChild(td0); tb.appendChild(tr0);
      return;
    }

    S.files.recent.slice(0, 40).forEach(function (f) {
      var conflict = conflictPaths.get(f.path);
      var tr = el("tr", conflict ? "is-conflict" : "");
      var tdp = el("td", "fpath");
      tdp.appendChild(document.createTextNode(String(f.path || "")));
      if (conflict) {
        var cb = el("span", "cbadge", "conflict");
        cb.setAttribute("title", "Both versions kept. Displaced copy: " + String(conflict.kept_as || ""));
        tdp.appendChild(cb);
        tdp.appendChild(el("div", "kept", "kept as " + String(conflict.kept_as || "")));
      }
      if (f.lock_violation) {
        var lb = el("span", "cbadge", "lock");
        lb.setAttribute("title", "Written while another agent held an advisory lock on this path.");
        tdp.appendChild(lb);
      }
      tr.appendChild(tdp);

      var tdw = el("td");
      tdw.appendChild(whoChip(agentOf(f.author) || { agent_id: f.author || "", name: f.author || "unknown" }, "sm"));
      tr.appendChild(tdw);

      tr.appendChild(el("td", "num", isFinite(f.size) ? fmtBytes(f.size) : "—"));
      tr.appendChild(el("td", "num", String(S.files.heat[f.path] || 0)));
      var when = parseTs(f.ts);
      tr.appendChild(el("td", "fwhen", isFinite(when) ? fmtAge((nowMs() - when) / 1000) + " ago" : "—"));
      tb.appendChild(tr);
    });
  }

  /* ============================================================ 18. tooltip */

  var tipEl;
  function showTip(data, x, y) {
    if (!data) return;
    clear(tipEl);
    if (data.title) tipEl.appendChild(el("div", null, data.title));
    (data.rows || []).forEach(function (r) {
      var row = el("div", "tip__row");
      row.appendChild(el("span", "tip__k", String(r[0])));
      row.appendChild(el("span", "tip__v", String(r[1])));
      tipEl.appendChild(row);
    });
    show(tipEl, true);
    var w = tipEl.offsetWidth, h = tipEl.offsetHeight;
    var left = Math.min(window.innerWidth - w - 8, Math.max(8, x + 14));
    var top = y - h - 12 < 8 ? y + 18 : y - h - 12;
    tipEl.style.left = left + "px";
    tipEl.style.top = top + "px";
  }
  function showTipAt(data, ux, uy, svg) {
    var r = svg.getBoundingClientRect();
    var vb = svg.viewBox.baseVal;
    var sx = vb && vb.width ? r.width / vb.width : 1;
    var sy = vb && vb.height ? r.height / vb.height : 1;
    showTip(data, r.left + ux * sx, r.top + uy * sy);
  }
  function hideTip() { show(tipEl, false); }

  function wireTooltips() {
    document.addEventListener("pointermove", function (e) {
      var t = e.target, data = null, hops = 0;
      while (t && hops++ < 4) { if (t.__tip) { data = t.__tip; break; } t = t.parentNode; }
      if (data) showTip(data, e.clientX, e.clientY); else hideTip();
    }, { passive: true });
    document.addEventListener("pointerleave", hideTip, { passive: true });
    window.addEventListener("blur", hideTip);
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") hideTip(); });
  }

  /* =========================================================== 19. transport */

  var mode = "idle";          // idle | sse | poll | fixture
  var es = null, attempt = 0, sseFails = 0, retryTimer = null, pollAbort = null;
  var stopped = false;

  function api(path, params) {
    var u = new URL(path, location.href);
    if (VT) u.searchParams.set("vt", VT);
    if (params) Object.keys(params).forEach(function (k) {
      if (params[k] !== undefined && params[k] !== null) u.searchParams.set(k, params[k]);
    });
    return u.pathname + u.search;
  }

  function backoffMs() {
    /* SPEC §5.2: min(30, 0.5 * 2^attempt) * random(), capped at 30 s. */
    var base = Math.min(30, 0.5 * Math.pow(2, Math.min(attempt, 8)));
    return Math.max(250, base * Math.random() * 1000);
  }

  function startSSE() {
    if (stopped) return;
    stopSSE();
    mode = "sse";
    setConn(attempt ? "retry" : "init", attempt ? "reconnecting" : "connecting",
      attempt ? "Reconnecting to the Hub (attempt " + attempt + ")" : "Opening the event stream");
    var url = api("/v1/stream", { since: S.lastSeq });
    try {
      es = new EventSource(url);
    } catch (err) { onSSEFail(); return; }

    es.onopen = function () {
      attempt = 0; sseFails = 0;
      setConn("live", "live", "Streaming from the Hub over Server-Sent Events");
      announce("Connected to the Hub.");
      refreshState();
    };
    es.onerror = function () { onSSEFail(); };
    es.onmessage = function (m) { onFrame(m); };
    es.addEventListener("parley", function (m) { onFrame(m); });
  }

  function onFrame(m) {
    if (!m || typeof m.data !== "string") return;
    if (!m.data || m.data.charAt(0) === ":") return;      // keep-alive comment
    var ev;
    try { ev = JSON.parse(m.data); } catch (err) { return; }
    if (Array.isArray(ev)) ev.forEach(applyEvent); else applyEvent(ev);
    if (es && es.readyState === 1) setConn("live", "live", "Streaming from the Hub");
  }

  function stopSSE() {
    if (es) { try { es.close(); } catch (e) {} es = null; }
  }

  function onSSEFail() {
    stopSSE();
    sseFails++;
    attempt++;
    if (sseFails >= 2) {
      /* SPEC §8.2 — degrade to long-poll after two consecutive SSE failures. */
      setConn("poll", "long-poll", "EventSource failed twice; falling back to long-polling /v1/events");
      startPolling();
      return;
    }
    var wait = backoffMs();
    setConn("retry", "reconnecting", "Stream dropped. Retrying in " + Math.round(wait / 100) / 10 + " s");
    clearTimeout(retryTimer);
    retryTimer = setTimeout(startSSE, wait);
  }

  var pollSuccesses = 0;
  function startPolling() {
    if (stopped) return;
    mode = "poll";
    pollOnce();
  }

  function pollOnce() {
    if (stopped || mode !== "poll") return;
    if (pollAbort) { try { pollAbort.abort(); } catch (e) {} }
    pollAbort = (typeof AbortController !== "undefined") ? new AbortController() : null;
    var opts = { cache: "no-store", credentials: "same-origin" };
    if (pollAbort) opts.signal = pollAbort.signal;
    fetch(api("/v1/events", { since: S.lastSeq, wait: 25, limit: 500 }), opts)
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        attempt = 0;
        pollSuccesses++;
        var evs = Array.isArray(data) ? data : (data && data.events) || [];
        evs.forEach(applyEvent);
        setConn("poll", "long-poll", "Long-polling the Hub (EventSource is unavailable on this path)");
        /* Try to climb back to SSE occasionally — a proxy hiccup should not
           sentence the Deck to polling for the rest of the session. */
        if (pollSuccesses % 6 === 0) { sseFails = 0; mode = "sse"; startSSE(); return; }
        setTimeout(pollOnce, 250);
      })
      .catch(function () {
        attempt++;
        var wait = backoffMs();
        setConn(attempt > 4 ? "down" : "retry", attempt > 4 ? "offline" : "reconnecting",
          "Cannot reach the Hub. Next try in " + Math.round(wait / 100) / 10 + " s");
        setTimeout(pollOnce, wait);
      });
  }

  /* ---- /v1/state refresh: the Deck computes roster/chat/tasks/files from
     events, but the Ledger, the collaboration graph and the file totals are
     the Hub's derived numbers. Refresh them on a debounce rather than
     recomputing them in the browser and risking disagreement. ---- */
  var refreshTimer = null, lastRefresh = 0, refreshInFlight = false;
  function scheduleStateRefresh() {
    if (FIXTURE) return;
    if (refreshTimer) return;
    var since = Date.now() - lastRefresh;
    var wait = Math.max(3000, 10000 - since);
    refreshTimer = setTimeout(function () { refreshTimer = null; refreshState(); }, wait);
  }

  function refreshState() {
    if (FIXTURE || refreshInFlight) return Promise.resolve();
    refreshInFlight = true;
    lastRefresh = Date.now();
    return fetch(api("/v1/state"), { cache: "no-store", credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (snap) { ingestSnapshot(snap); })
      .catch(function () { /* the stream is the live path; a failed refresh is not fatal */ })
      .then(function () { refreshInFlight = false; });
  }

  /* Backfill the timeline from the log. Viewer tokens may read /v1/events
     (SPEC §5), so this works for the Deck; if the Hub refuses, the timeline
     simply starts where the Deck connected and says so. */
  function backfillTimeline() {
    if (FIXTURE) return Promise.resolve();
    var types = "status.update,file.put,file.conflict,task.done,agent.hello,agent.offline,hub.started";
    return fetch(api("/v1/events", { since: 0, limit: 4000, types: types }),
                 { cache: "no-store", credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        var evs = Array.isArray(data) ? data : (data.events || []);
        ingestBackfill(evs);
      })
      .catch(function () {});
  }

  function ingestBackfill(evs) {
    evs.forEach(function (ev) {
      if (!ev || !ev.type) return;
      var t = parseTs(ev.ts);
      if (!isFinite(t)) return;
      var b = ev.body || {};
      if (ev.type === "status.update") {
        lanePush(ev.actor, parseTs(b.since) || t, PSR_STATES.indexOf(b.state) >= 0 ? b.state : "unknown");
      } else if (ev.type === "agent.offline") {
        lanePush(b.agent_id || ev.actor, t, "offline");
      } else if (ev.type === "file.put") {
        S.tl.marks.push({ t: t, agent: ev.actor, kind: "put", label: String(b.path || "") });
      } else if (ev.type === "file.conflict") {
        S.tl.marks.push({ t: t, agent: (b.theirs && b.theirs.agent) || ev.actor, kind: "conflict", label: String(b.path || "") });
      } else if (ev.type === "task.done") {
        S.tl.marks.push({ t: t, agent: ev.actor, kind: "done", label: String(b.id || "") });
      } else if (ev.type === "hub.started") {
        if (!isFinite(S.hubStarted) || t < S.hubStarted) S.hubStarted = t;
      }
    });
    S.tl.marks.sort(function (a, b) { return a.t - b.t; });
    if (S.tl.marks.length > 4000) S.tl.marks.splice(0, S.tl.marks.length - 4000);
    mark("timeline"); mark("bar");
  }

  /* =============================================================== 20. admin */

  /* SPEC 3.7 pins the host token to exactly one carrier: Authorization:
     Parley-Host. Not a query parameter (it would land in proxy and browser
     logs) and not a second bespoke header -- sending it twice only doubles the
     exposure and guarantees the two paths eventually diverge. */
  function hostHeaders() {
    return {
      "Authorization": "Parley-Host " + HST,
      "Content-Type": "application/json"
    };
  }

  function adminPost(path, body) {
    return fetch(path, {
      method: "POST", headers: hostHeaders(), credentials: "same-origin",
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error((j && j.error && j.error.message) || ("HTTP " + r.status));
        return j;
      });
    });
  }

  function openAdmin() {
    var body = $("adminBody");
    clear(body);

    var pending = [];
    S.agents.forEach(function (a) { if (a.status === "pending") pending.push(a); });

    var sec1 = el("div", "dlg__sec");
    sec1.appendChild(el("h3", null, "Pending agents"));
    if (!pending.length) {
      sec1.appendChild(el("p", "note", "Nobody is waiting for approval."));
    } else {
      pending.forEach(function (a) {
        var row = el("div", "dlg__row");
        row.appendChild(whoChip(a));
        row.appendChild(el("span", "note", a.kind + " · " + a.os));
        var ok = el("button", "btn btn--primary", "Approve");
        ok.type = "button";
        ok.addEventListener("click", function () {
          ok.disabled = true;
          adminPost("/v1/admin/approve", { agent_id: a.agent_id })
            .then(function () { a.status = "active"; mark("roster"); openAdmin(); })
            .catch(function (e) { ok.disabled = false; adminError(sec1, e); });
        });
        row.appendChild(ok);
        sec1.appendChild(row);
      });
    }
    body.appendChild(sec1);

    var sec2 = el("div", "dlg__sec");
    sec2.appendChild(el("h3", null, "Revoke an agent"));
    sec2.appendChild(el("p", "note", "Invalidates that agent's key immediately. Its past events stay in the log — nothing is ever deleted."));
    S.agents.forEach(function (a) {
      if (a.status === "revoked") return;
      var row = el("div", "dlg__row");
      row.appendChild(whoChip(a));
      var rv = el("button", "btn btn--danger", "Revoke");
      rv.type = "button";
      rv.addEventListener("click", function () {
        rv.disabled = true;
        adminPost("/v1/admin/revoke", { agent_id: a.agent_id })
          .then(function () { a.status = "revoked"; a.online = false; mark("roster"); openAdmin(); })
          .catch(function (e) { rv.disabled = false; adminError(sec2, e); });
      });
      row.appendChild(rv);
      sec2.appendChild(row);
    });
    body.appendChild(sec2);

    var sec3 = el("div", "dlg__sec");
    sec3.appendChild(el("h3", null, "The invite"));
    sec3.appendChild(el("p", "note", "The watchword is an enrolment secret only, and it is never rendered without the host token. It is also never written to the log or to any error message."));
    var row3 = el("div", "dlg__row");
    var reveal = el("button", "btn", "Reveal invite");
    reveal.type = "button";
    reveal.addEventListener("click", function () {
      reveal.disabled = true;
      adminPost("/v1/admin/reveal", {})
        .then(function (j) { showSecret(sec3, j.watchword || j.invite || "(the Hub returned no watchword)"); })
        .catch(function () {
          sec3.appendChild(el("p", "note", "This Hub does not keep the watchword in recoverable form — by design, it stores only the derived root key. Rotate to mint a new one."));
          reveal.disabled = false;
        });
    });
    var rotate = el("button", "btn btn--danger", "Rotate watchword");
    rotate.type = "button";
    rotate.addEventListener("click", function () {
      rotate.disabled = true;
      adminPost("/v1/admin/rotate-watchword", {})
        .then(function (j) { showSecret(sec3, j.watchword || "(rotated; the Hub returned no watchword)"); })
        .catch(function (e) { rotate.disabled = false; adminError(sec3, e); });
    });
    row3.appendChild(reveal); row3.appendChild(rotate);
    sec3.appendChild(row3);
    sec3.appendChild(el("p", "note", "Rotating does not disconnect anyone: existing agent keys are minted by the Hub and do not derive from the watchword."));
    body.appendChild(sec3);

    var sec4 = el("div", "dlg__sec");
    sec4.appendChild(el("h3", null, "Viewer link"));
    sec4.appendChild(el("p", "note", "A read-only, expiring token for someone who should see the Deck but not participate."));
    var mint = el("button", "btn", "Mint a viewer link");
    mint.type = "button";
    mint.addEventListener("click", function () {
      mint.disabled = true;
      adminPost("/v1/admin/viewer-token", {})
        .then(function (j) {
          var tok = j.token || j.viewer_token || "";
          showSecret(sec4, tok ? location.origin + location.pathname + "?vt=" + tok : "(no token returned)");
          mint.disabled = false;
        })
        .catch(function (e) { mint.disabled = false; adminError(sec4, e); });
    });
    sec4.appendChild(mint);
    body.appendChild(sec4);

    $("adminDlg").showModal();
  }

  function showSecret(sec, text) {
    var old = sec.querySelector(".secret");
    if (old) old.remove();
    sec.appendChild(el("div", "secret", String(text)));
  }
  function adminError(sec, err) {
    var p = el("p", "note", "Failed: " + (err && err.message ? err.message : "unknown error"));
    sec.appendChild(p);
  }

  /* ============================================================== 21. theme */

  var THEME_KEY = "parley.deck.theme";
  var themeModes = ["auto", "light", "dark"];
  var themeLabels = { auto: "Auto", light: "Light", dark: "Dark" };

  function applyTheme(m) {
    document.documentElement.setAttribute("data-theme-mode", m);
    if (m === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", m);
    $("themeLabel").textContent = themeLabels[m];
    var btn = $("themeBtn");
    btn.setAttribute("aria-label", "Theme: " + themeLabels[m].toLowerCase() + ". Activate to change.");
    btn.setAttribute("title", "Theme: " + themeLabels[m].toLowerCase());
    readThemeTokens();
    mark("roster"); mark("ledger"); mark("graph"); mark("timeline"); mark("tasks"); mark("files");
    mark("caps"); mark("reqs");
    chatReset();
  }

  function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem(THEME_KEY); } catch (e) {}
    var m = themeModes.indexOf(saved) >= 0 ? saved : "auto";
    applyTheme(m);
    $("themeBtn").addEventListener("click", function () {
      var cur = document.documentElement.getAttribute("data-theme-mode") || "auto";
      var next = themeModes[(themeModes.indexOf(cur) + 1) % themeModes.length];
      try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
      applyTheme(next);
    });
    if (window.matchMedia) {
      var mq = window.matchMedia("(prefers-color-scheme: dark)");
      var onChange = function () {
        if ((document.documentElement.getAttribute("data-theme-mode") || "auto") === "auto") applyTheme("auto");
      };
      if (mq.addEventListener) mq.addEventListener("change", onChange);
      else if (mq.addListener) mq.addListener(onChange);
    }
  }

  /* =============================================================== 22. boot */

  function wireChrome() {
    chatLog = $("chatLog"); chatMore = $("chatMore"); chatJump = $("chatJump");
    announcer = $("announcer"); tipEl = $("tip");

    chatMore.addEventListener("click", showEarlier);
    chatJump.addEventListener("click", function () {
      chatLog.scrollTop = chatLog.scrollHeight;
      newSinceScroll = 0; show(chatJump, false);
      mark("chat");
    });
    chatLog.addEventListener("scroll", function () {
      if (atBottom()) { newSinceScroll = 0; show(chatJump, false); }
    }, { passive: true });

    $("togSys").addEventListener("change", function (e) {
      chatLog.classList.toggle("show-sys", e.target.checked);
      if (atBottom()) chatLog.scrollTop = chatLog.scrollHeight;
    });
    $("togAnnounce").addEventListener("change", function (e) {
      announceChat = e.target.checked;
      chatLog.setAttribute("aria-live", announceChat ? "polite" : "off");
    });

    $("reqsMoreBtn").addEventListener("click", function () {
      reqHistAll = !reqHistAll;
      mark("reqs");
    });

    Array.prototype.forEach.call($("tlRange").querySelectorAll(".seg__btn"), function (b) {
      b.addEventListener("click", function () {
        Array.prototype.forEach.call($("tlRange").querySelectorAll(".seg__btn"), function (o) {
          o.classList.remove("is-on"); o.removeAttribute("aria-pressed");
        });
        b.classList.add("is-on"); b.setAttribute("aria-pressed", "true");
        tlWindow = parseInt(b.getAttribute("data-win"), 10) || 0;
        mark("timeline");
      });
    });

    if (HST) {
      show($("adminBtn"), true);
      $("adminBtn").addEventListener("click", openAdmin);
    }

    /* keep --bar-h honest so the sticky chat column is exactly right */
    var bar = document.querySelector(".bar");
    var setBarH = function () {
      document.documentElement.style.setProperty("--bar-h", bar.offsetHeight + "px");
    };
    setBarH();
    if (window.ResizeObserver) new ResizeObserver(setBarH).observe(bar);
    else window.addEventListener("resize", setBarH);

    var reflow = null;
    window.addEventListener("resize", function () {
      clearTimeout(reflow);
      reflow = setTimeout(function () { layoutLedgerBars(); mark("timeline"); }, 120);
    });

    wireTooltips();
  }

  var tickN = 0;
  function tick() {
    /* Ages, uptime and staleness move on their own; repaint cheaply on a timer
       rather than on every event. The timeline redraws every 5 s — often enough
       that the "now" rule visibly tracks, cheap enough to leave on a monitor. */
    tickN++;
    if (document.visibilityState === "hidden") return;
    mark("bar");
    var needRoster = false;
    S.agents.forEach(function (a) {
      if (!a.psr) return;
      if (isFinite(a.psr.age_s)) a.psr.age_s += 1;
      var maxAge = (S.policy && S.policy.psr_max_age_s) || 30;
      var wasStale = a.psr.stale === true;
      a.psr.stale = psrAgeSec(a) > 3 * maxAge;
      if (a.psr.stale !== wasStale) {
        needRoster = true;
        if (a.psr.stale) announce(a.name + "'s standing report has gone stale.");
      }
    });
    if (needRoster || tickN % 15 === 0) mark("roster");
    if (tickN % 5 === 0) mark("timeline");
    /* The expiry clocks are the whole point of the requests panel, so they move
       every second — but in place, writing into a handful of remembered nodes
       rather than re-rendering a panel full of expanded reasons. The settled
       list's "4m ago" column is coarse, so it rides the 30 s rebuild instead. */
    paintReqClocks();
    if (tickN % 30 === 0 && S.reqs.size) mark("reqs");
  }

  function loadFixture() {
    setConn("fixture", "fixture", "Rendering fixture.json — no Hub is involved");
    fetch("./fixture.json", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (fx) {
        ingestSnapshot(fx.state || fx);
        if (Array.isArray(fx.backfill)) ingestBackfill(fx.backfill);
        flush();
        if (REPLAY && Array.isArray(fx.replay) && fx.replay.length) {
          var i = 0;
          var step = function () {
            if (i >= fx.replay.length) {
              setConn("fixture", "fixture", "Fixture replay finished");
              return;
            }
            var ev = fx.replay[i++];
            /* Stamp replayed events with the fixture's own clock (nowMs carries
               the offset adopted from state.server_time) so the timeline, the
               ages and the chat times all agree. */
            ev.ts = new Date(nowMs()).toISOString();
            applyEvent(ev);
            setTimeout(step, 1700);
          };
          setTimeout(step, 1200);
        }
      })
      .catch(function (e) {
        setConn("down", "fixture failed", String(e && e.message));
      });
  }

  function start() {
    wireChrome();
    initTheme();
    renderBar();

    if (FIXTURE) { loadFixture(); setInterval(tick, 1000); return; }

    setConn("init", "connecting", "Fetching the initial snapshot");
    fetch(api("/v1/state"), { cache: "no-store", credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status + (r.status === 403 ? " — this Deck needs a viewer token (?vt=…)" : ""));
        return r.json();
      })
      .then(function (snap) {
        ingestSnapshot(snap);
        flush();
        backfillTimeline();
        startSSE();
      })
      .catch(function (e) {
        setConn("down", "no hub", String(e && e.message || e));
        announce("Cannot reach the Hub.");
        /* Still try the stream — the snapshot may just be slow or gated. */
        attempt = 1;
        setTimeout(startSSE, backoffMs());
      });

    setInterval(tick, 1000);
    setInterval(function () { if (document.visibilityState !== "hidden") refreshState(); }, 60000);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") { refreshState(); mark("timeline"); }
    });
    window.addEventListener("online", function () { attempt = 0; sseFails = 0; startSSE(); });
    window.addEventListener("pagehide", function () { stopped = true; stopSSE(); });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
