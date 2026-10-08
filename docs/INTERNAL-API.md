# Internal API contract

> Companion to `docs/SPEC.md`. The spec says *what the system does*; this says *what the Python
> modules are called and what their signatures are*, so independently-built modules link up.
>
> **These names and signatures are binding.** If you need something that is not here, add it —
> but do not rename or re-shape anything that is.

Every module begins with `from __future__ import annotations`. Python 3.9 floor. Stdlib only.

---

## `parley/version.py`
```python
__version__ = "1.0.0"
WIRE_VERSION = "PARLEY/1"
MIN_PYTHON = (3, 9)
```

## `parley/errors.py`
```python
class ParleyError(Exception):
    code: str = "error"; http_status: int = 500; retryable: bool = False
    def __init__(self, message: str, *, detail: dict | None = None, hint: str = "") -> None: ...
    def to_dict(self) -> dict: ...        # {"error": {"code","message","detail","retryable","hint"}}

# One subclass per code in SPEC §12, e.g.:
class BadSignature(ParleyError):      code="bad_signature";    http_status=401
class StaleTimestamp(ParleyError):    code="stale_timestamp";  http_status=401
class ReplayedNonce(ParleyError):     code="replayed_nonce";   http_status=401
class UnknownAgent(ParleyError):      code="unknown_agent";    http_status=401
class PendingApproval(ParleyError):   code="pending_approval"; http_status=403
class Revoked(ParleyError):           code="revoked";          http_status=403
class EnrollClosed(ParleyError):      code="enroll_closed";    http_status=403
class HostTokenRequired(ParleyError): code="host_token_required"; http_status=403
class ReadOnlyToken(ParleyError):     code="read_only_token";  http_status=403
class NoSuchBlob(ParleyError):        code="no_such_blob";     http_status=404
class DuplicateEvent(ParleyError):    code="duplicate_event";  http_status=409
class TooLarge(ParleyError):          code="too_large";        http_status=413
class BadEvent(ParleyError):          code="bad_event";        http_status=422
class BadPath(ParleyError):           code="bad_path";         http_status=422
class RateLimited(ParleyError):       code="rate_limited";     http_status=429; retryable=True
class FingerprintMismatch(ParleyError): code="fingerprint_mismatch"; http_status=409
class TransportError(ParleyError):    code="transport";        http_status=0;   retryable=True
```

## `parley/jsonutil.py`
```python
def canonical(obj) -> bytes                     # SPEC §1.3
def dumps(obj) -> str                           # compact, UTF-8 safe, for storage/wire
def loads(data: bytes | str)                    # raises BadJson on failure
def sha256_hex(data: bytes) -> str
def now_rfc3339() -> str                        # SPEC §1.2
def parse_rfc3339(s: str) -> float              # -> unix seconds float; raises ValueError
def atomic_write(path: Path, data: bytes) -> None   # temp in same dir + fsync + os.replace
```

## `parley/ids.py`
```python
def new_session_id() -> str      # "ses_" + 16 hex
def new_agent_id() -> str
def new_event_id() -> str
def new_task_id() -> str         # "tsk_" + 8 hex
def new_viewer_token() -> str    # "vwr_" + 32 hex
def new_host_token() -> str      # "hst_" + 32 hex
def is_id(value: str, prefix: str) -> bool
```

## `parley/wordlist.py`
```python
WORDS: tuple[str, ...]           # >= 2048 entries, deduped, 3-7 chars, no homophone pairs,
                                 # no profanity, no visually confusable pairs, ASCII lowercase
def assert_wordlist_sane() -> None   # called by tests: size, uniqueness, charset, no near-dupes
```

## `parley/crypto.py`
```python
BACKEND: str                     # "cryptography" | "pynacl" | "pure" -- which AEAD is in use

def normalise_watchword(s: str) -> str                      # SPEC §3.1
def generate_watchword(words: int = 5) -> str               # readable sentence w/ literal "the"
def derive_root_key(watchword: str, session_id: str, *, iterations: int = 200_000) -> bytes
def hkdf(key: bytes, info: bytes, length: int = 32) -> bytes # RFC 5869, SHA-256, empty salt
def enroll_key(root_key: bytes) -> bytes
def seal_key(root_key: bytes) -> bytes
def fingerprint(root_key: bytes) -> str                     # "lemon-anchor-fox"

def string_to_sign(method: str, path: str, body: bytes, ts: str,
                   nonce: str, session: str, agent: str) -> bytes      # SPEC §3.3
def sign(key: bytes, sts: bytes) -> str                     # hex HMAC-SHA256
def verify(key: bytes, sts: bytes, signature: str) -> bool  # compare_digest

def sign_event(key: bytes, event: dict) -> str              # over canonical(event minus seq,sig)
def verify_event(key: bytes, event: dict) -> bool

def seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes        # nonce||ct||tag
def unseal(key: bytes, sealed: bytes, aad: bytes) -> bytes
def seal_frames(key: bytes, data: bytes, blob_hash: str) -> bytes   # 256 KiB frames, SPEC §3.6
def unseal_frames(key: bytes, data: bytes, blob_hash: str) -> bytes

def new_nonce_hex() -> str       # 16 hex, for request nonces
```

## `parley/protocol.py`
```python
EVENT_TYPES: frozenset[str]          # every type in SPEC §4
PSR_STATES: tuple[str, ...]          # ("idle","planning","working","reviewing","blocked","waiting","offline")
MAX_BODY_BYTES = 256 * 1024
MAX_CHAT_BYTES = 16 * 1024

def make_event(actor: str, session: str, etype: str, body: dict,
               *, event_id: str | None = None, ts: str | None = None) -> dict
def validate_event(event: dict, *, strict: bool = True) -> list[str]
    # returns [] when valid; a list of human-readable problems otherwise.
    # strict=False downgrades "unknown type in x.* namespace" and PSR warnings.
def validate_psr(body: dict) -> list[str]
def normalise_path(p: str) -> str          # SPEC §7.1; raises BadPath
def safe_join(workspace: Path, wire_path: str) -> Path   # raises BadPath on escape
def is_hub_authored(event: dict) -> bool
def event_summary(event: dict) -> str      # one-line human rendering, used by `parley watch`
```

## `parley/config.py`
```python
DEFAULTS: dict      # heartbeat_s=15, psr_max_age_s=30, poll_ms=2000,
                    # max_blob_bytes=26214400, max_agents=16, skew_s=300, nonce_ttl_s=600,
                    # enroll_ttl_s=0, enroll_max_uses=0, require_approval=False, sealed=False

@dataclass
class HubConfig:    # written to <state_dir>/hub.json
    session: str; name: str; created: str; bind: str; port: int
    watchword_hash: str          # sha256 of the normalised watchword, for `--invite` check only
    root_key_hex: str            # Hub stores the derived root key, never the watchword itself
    fingerprint: str; host_token: str; policy: dict; pbkdf2_iterations: int
    @classmethod
    def load(cls, state_dir: Path) -> "HubConfig"
    def save(self, state_dir: Path) -> None

@dataclass
class Credentials:  # written to <workspace>/.parley/credentials.json, mode 0600
    hub_url: str; session: str; agent_id: str; agent_key_hex: str
    fingerprint: str; name: str; kind: str; sealed: bool; policy: dict
    @classmethod
    def load(cls, workspace: Path) -> "Credentials"
    def save(self, workspace: Path) -> None

def workspace_state_dir(workspace: Path) -> Path    # <workspace>/.parley, created 0700
def default_workspace() -> Path                     # cwd
def resolve_hub_url(raw: str) -> str                # adds scheme, strips trailing /, validates
```

---

## `parley/hub/store.py`
SQLite (`sqlite3`, WAL, `check_same_thread=False` + a lock). DB at `<state_dir>/parley.db`,
blobs at `<state_dir>/blobs/<first2>/<sha256>`.

```python
class Store:
    def __init__(self, state_dir: Path) -> None
    def close(self) -> None

    # events
    def append(self, event: dict) -> dict                 # assigns seq; returns stored event
    def append_many(self, events: list[dict]) -> list[dict]
    def head_seq(self) -> int
    def read(self, since: int = 0, limit: int = 1000, types: list[str] | None = None) -> list[dict]
    def find_by_author_id(self, actor: str, event_id: str) -> dict | None   # dedup, SPEC §5.2

    # agents
    def put_agent(self, rec: dict) -> None                # {agent_id,name,kind,model,os,host,
                                                          #  key_hex,status,created,last_seen,...}
    def get_agent(self, agent_id: str) -> dict | None
    def list_agents(self) -> list[dict]
    def set_agent_status(self, agent_id: str, status: str) -> None   # active|pending|revoked
    def touch_agent(self, agent_id: str, when: float) -> None

    # blobs
    def has_blob(self, blob_hash: str) -> bool
    def put_blob(self, blob_hash: str, data: bytes) -> int
    def open_blob(self, blob_hash: str) -> BinaryIO
    def blob_size(self, blob_hash: str) -> int | None

    # file index
    def put_file(self, path: str, rec: dict) -> None      # {hash,size,mode,author,seq,ts}
    def get_file(self, path: str) -> dict | None
    def list_files(self) -> dict[str, dict]
    def delete_file(self, path: str) -> None

    # viewer tokens / nonces
    def put_viewer_token(self, token: str, expires: float, label: str = "") -> None
    def check_viewer_token(self, token: str) -> bool
    def revoke_viewer_token(self, token: str) -> None
    def seen_nonce(self, agent_id: str, nonce: str, ts: float, ttl: float) -> bool  # True = replay
```

## `parley/hub/state.py`
```python
class StateView:
    """Materialised view, updated incrementally by every appended event. Thread-safe."""
    def __init__(self, store: Store, policy: dict) -> None
    def apply(self, event: dict) -> None
    def rebuild(self) -> None                   # replay the whole log
    def snapshot(self, *, for_viewer: bool = False) -> dict
    # snapshot shape (this is exactly what GET /v1/state returns and what the Deck consumes):
    # {"v","session","name","fingerprint","head_seq","server_time","hub_started",
    #  "policy":{...},
    #  "agents":[{"agent_id","name","kind","model","os","host","color_hue","status",
    #             "online","last_seen","joined",
    #             "psr":{"state","headline","detail","focus","task","progress",
    #                    "blocked_on","needs","eta_s","since","age_s","stale"} | null}],
    #  "chat":[<last 200 chat.message events>],
    #  "tasks":[{"id","title","detail","status","progress","claimed_by","tags","priority"}],
    #  "locks":[{"path","agent_id","expires","intent"}],
    #  "files":{"count","bytes","recent":[{path,hash,size,author,seq,ts}],
    #           "conflicts":[{path,kept_as,ours,theirs,seq,ts}],
    #           "heat":{path: score}},
    #  "ledger":<parley.ledger.LedgerResult.to_dict()>,
    #  "graph":{"nodes":[{id,label}],
    #           "edges":[{"source","target","weight","kinds":{"reply":n,"citation":n,
    #                                                         "co_edit":n,"blocked_on":n}}]},
    #  "decisions":[{"id","question","options","votes","resolved","option"}],
    #  "notices":[{"ts","text","level"}]}
```

## `parley/hub/server.py`
```python
class Hub:
    def __init__(self, state_dir: Path, config: HubConfig, *, workspace: Path | None = None) -> None
    def start(self) -> None          # non-blocking; binds and serves on a thread
    def serve_forever(self) -> None  # blocking
    def stop(self) -> None
    @property
    def url(self) -> str             # e.g. "http://192.168.1.20:7777"
    def deck_url(self, *, with_viewer_token: bool = True) -> str
    def submit(self, event: dict, *, actor_key: bytes | None = None) -> dict  # in-process append

def create_parley(workspace: Path, *, name: str, port: int = 7777, bind: str = "0.0.0.0",
                  public: bool = False, sealed: bool = False, require_approval: bool = False,
                  words: int = 5, force: bool = False) -> tuple[Hub, str]
    """Returns (hub, watchword). The watchword is returned ONCE and never stored in plain form.
       Raises HubStateExists when a parley already lives at this workspace unless force=True --
       creating mints a NEW session id and root key, so overwriting strands every enrolled
       agent behind a fingerprint_mismatch."""

def resume_parley(workspace: Path, *, port: int | None = None,
                  bind: str | None = None) -> Hub
    """Restart the Hub on an EXISTING state directory, preserving the session id, root key,
       fingerprint, host token, event log, blobs and enrolled agents. This is what a service
       supervisor must invoke. Raises NoHubState when there is nothing to resume.
       port/bind default to the persisted values and are written back when overridden,
       because `approve` and `invite` read the Hub's address from hub.json."""

def hub_state_dir(workspace: Path) -> Path          # <workspace>/.parley/hub
def find_hub_state_dir(start: Path) -> Path | None  # walk up looking for one
def discard_hub_state(workspace: Path) -> None      # used by init --force and bind-failure rollback
```
`server.py` uses `http.server.ThreadingHTTPServer`. It must set `daemon_threads = True`,
`protocol_version = "HTTP/1.1"`, and must not block the accept loop on a slow SSE client.
LAN discovery: the Hub also answers a UDP broadcast probe on port 7778
(`b"PARLEY/1 DISCOVER"` → `{"session","name","url","fingerprint"}`) so `parley join --discover`
works without anyone typing an IP. Disabled when `--public`.

## `parley/hub/api.py`
```python
def authenticate(store: Store, config: HubConfig, method: str, path: str,
                 headers: Mapping[str, str], body: bytes) -> dict
    """-> {"kind": "agent"|"enroll"|"viewer"|"host", "agent": dict | None}. Raises ParleyError."""
def handle(hub, method: str, path: str, headers, body: bytes) -> tuple[int, dict, bytes]
    """Pure-ish router: returns (status, response_headers, response_body)."""
```

## `parley/hub/ratelimit.py`
```python
class TokenBucket:
    def __init__(self, rate_per_min: float, burst: float) -> None
    def take(self, now: float, n: float = 1.0) -> float   # 0.0 = allowed, else Retry-After secs
class Limiter:
    def check(self, key: str, kind: str, now: float) -> float
```

---

## `parley/client/transport.py`
```python
class Transport:
    """urllib-based, with §3.3 signing, §3.6 sealing, retries and full-jitter backoff."""
    def __init__(self, hub_url: str, session: str, agent_id: str, key: bytes,
                 *, sealed: bool = False, seal_key: bytes | None = None,
                 timeout: float = 30.0) -> None
    def request(self, method: str, path: str, body: bytes | None = None,
                *, headers: dict | None = None, json_body=None) -> tuple[int, dict, bytes]
    def get_json(self, path: str) -> dict
    def post_json(self, path: str, obj) -> dict
    def stream(self, since: int, types: list[str] | None = None) -> Iterator[dict]
        """SSE; falls back to long-poll after 2 consecutive SSE failures. Never raises on a
           transient drop -- it reconnects and keeps yielding. Yields events in seq order."""
    def last_skew(self) -> float
```

## `parley/client/client.py`
```python
class ParleyClient:
    def __init__(self, workspace: Path, creds: Credentials) -> None
    @classmethod
    def enroll(cls, hub_url: str, watchword: str, workspace: Path, *, name: str, kind: str,
               model: str = "", capabilities: list[str] | None = None,
               sealed: bool = False, expect_fingerprint: str = "") -> "ParleyClient"
    def emit(self, etype: str, body: dict) -> dict
    def say(self, text: str, *, to=None, reply_to=None, refs=None) -> dict
    def status(self, headline: str, *, state: str = "working", focus=None, detail="",
               progress=None, task=None, blocked_on=None, needs=None, eta_s=None) -> dict
    def know(self, title: str, kind: str, *, detail: str = "", refs=None) -> dict
    def heartbeat(self) -> dict
    def bye(self, reason: str = "") -> None
    def state(self) -> dict
    def events(self, since: int = 0, limit: int = 1000) -> list[dict]
    def stream(self, since: int = 0) -> Iterator[dict]
    def put_blob(self, data: bytes) -> str
    def get_blob(self, blob_hash: str) -> bytes
```

## `parley/client/ignore.py`
```python
class IgnoreRules:
    @classmethod
    def load(cls, workspace: Path) -> "IgnoreRules"     # builtin + .parleyignore
    def ignored(self, rel_posix_path: str, *, is_dir: bool = False) -> bool
```

## `parley/client/sync.py`
```python
class WorkspaceSync:
    def __init__(self, client: ParleyClient, workspace: Path, *,
                 poll_ms: int = 2000, max_blob_bytes: int = 26214400) -> None
    def scan_once(self) -> list[dict]        # local changes -> emitted file.* events
    def apply_event(self, event: dict) -> None   # remote file.* -> disk (atomic, SPEC §7.5)
    def bootstrap(self) -> None                  # pull the full index on first join
    def run(self, stop: threading.Event) -> None
    @property
    def index(self) -> dict[str, dict]       # path -> {hash,size,mtime_ns}
```
Local index persisted at `<workspace>/.parley/index.json` so a restart does not re-upload the
world or mistake unchanged files for edits.

## `parley/client/pigeonhole.py`
```python
class Pigeonhole:
    """SPEC §10. Mirrors the log into files and publishes what an agent appends."""
    def __init__(self, client: ParleyClient, workspace: Path) -> None
    def on_event(self, event: dict) -> None       # -> inbox.jsonl, chat.md, roster.json, state.json
    def drain_outbox(self) -> list[dict]          # byte-cursor read; publishes; writes outbox.ack.jsonl
    def refresh_psr_from_me_json(self) -> dict | None
```

## `parley/client/runtime.py`
```python
class Runtime:
    """What `parley run` runs: stream + sync + heartbeat + pigeonhole + PSR freshness."""
    def __init__(self, workspace: Path, *, sync: bool = True, pigeonhole: bool = True) -> None
    def run(self) -> int                # blocking; returns an exit code; clean SIGINT/SIGTERM
    def stop(self) -> None
```

---

## `parley/ledger.py`
```python
DEFAULT_WEIGHTS: dict          # SPEC §9

@dataclass
class LedgerLine:
    agent_id: str; name: str; total: float; share: float
    components: dict        # {"contributions": float, "authored": float, "delivery": float,
                            #  "influence": float, "presence": float}
    evidence: dict          # {component: [ {"seq": int, "label": str, "points": float}, ... ]}
    def to_dict(self) -> dict

@dataclass
class LedgerResult:
    lines: list[LedgerLine]; weights: dict; computed_at: str; event_count: int
    def to_dict(self) -> dict
    def why(self, agent_id: str) -> str      # human-readable breakdown for `parley ledger --why`

def compute(events: Iterable[dict], files: Mapping[str, dict],
            weights: dict | None = None) -> LedgerResult
def load_weights(workspace: Path) -> dict    # .parley/ledger.json over DEFAULT_WEIGHTS
```
`compute` must be **pure** (no I/O, no clock beyond an injected `computed_at`) so it is trivially
testable and so the Hub can recompute it incrementally.

## `parley/exchange.py`  — the Exchange, pure logic (SPEC §15)

No I/O, no clock reads beyond injected `now`. The hub, the client and the tests all share this.

```python
SAFETY_LEVELS = ("safe", "guarded", "dangerous")
CAPABILITY_KINDS = ("skill", "mcp", "hardware", "tool", "data", "compute", "human")
DECLINE_CODES = ("unknown_capability", "bad_input", "policy", "busy", "unsafe",
                 "offline", "needs_human", "cancelled", "other")
REQUEST_STATES = ("pending", "accepted", "done", "failed", "declined", "expired", "cancelled")

@dataclass
class Capability:
    name: str; title: str; kind: str; description: str
    input_schema: dict | None = None; output: str = "text"
    safety: str = "guarded"; cost: str = "moderate"; concurrency: int = 1
    exclusive: bool = False; avg_duration_s: float = 0.0; examples: list = field(default_factory=list)
    agent_id: str = ""; agent_name: str = ""
    def to_dict(self) -> dict
    @classmethod
    def from_dict(cls, d: dict) -> "Capability"
    def validate(self) -> list[str]        # [] when the announcement is well-formed

class Registry:
    """Merged capability catalogue across all agents. announce() is total per agent."""
    def announce(self, agent_id: str, agent_name: str, caps: list[dict]) -> None
    def revoke(self, agent_id: str, names: list[str]) -> None
    def drop_agent(self, agent_id: str) -> None
    def find(self, name: str, *, agent_id: str = "") -> list[Capability]
    def all(self) -> list[Capability]
    def to_dict(self, *, online: Mapping[str, bool] | None = None,
                in_flight: Mapping[str, int] | None = None) -> dict

def validate_input(schema: dict | None, value) -> list[str]
    """JSON-Schema subset validator: type, properties, required, enum, minimum, maximum,
       items, additionalProperties. Returns [] when valid. Must never raise on a hostile schema
       or a deeply-nested/recursive value -- bound the depth."""

@dataclass
class Policy:
    default: str = "ask"; auto_accept_safe: bool = True
    rules: list = field(default_factory=list); max_in_flight: int = 4
    max_per_requester_per_hour: int = 60; require_reason: bool = True
    never_auto_accept: list = field(default_factory=lambda: ["dangerous"])
    @classmethod
    def load(cls, workspace: Path) -> "Policy"       # .parley/policy.json, safe defaults if absent
    def save(self, workspace: Path) -> None
    def decide(self, *, requester: str, capability: Capability | None,
               is_instruction: bool, in_flight: int, recent_from_requester: int,
               has_reason: bool) -> tuple[str, str]
        """-> (action, why) where action is "allow" | "ask" | "deny".
           MUST enforce SPEC §15.4: dangerous is never "allow"; a free-form instruction is never
           treated as safe; unknown requester gets at most safe capabilities; first matching rule
           wins. `why` is a human-readable sentence shown to the operator and used as the
           decline reason, and must never leak policy internals that are not the requester's
           business."""

class RequestTracker:
    """The §15.3 state machine, driven by events. Used by hub StateView and client runtime."""
    def apply(self, event: dict, *, now: float) -> None
    def expire_due(self, now: float) -> list[dict]     # -> request.expired events to emit
    def state_of(self, req_id: str) -> str
    def get(self, req_id: str) -> dict | None
    def in_flight_for(self, agent_id: str) -> int
    def addressed_to(self, agent_id: str, *, states: tuple = ("pending", "accepted")) -> list[dict]
    def to_dict(self) -> dict
    # record shape: {"id","from","to","capability"|None,"instruction"|None,"input","reason",
    #                "state","created_ts","accepted_ts","eta_s","timeout_s","priority",
    #                "progress","note","result","error","duration_s","seq"}

def make_request(from_agent: str, to: str, *, capability: str = "", instruction: str = "",
                 input: dict | None = None, reason: str, timeout_s: int = 300,
                 priority: int = 3, expects: str = "text", refs=None) -> dict
    """Builds a validated request.create body. Raises BadEvent if neither/both of
       capability and instruction are given, or if reason is empty."""
```

## `parley/client/exchange.py` — the provider runtime

```python
Handler = Callable[[dict, dict], Any]
    # (input, request_record) -> output. May raise; the runtime converts that into
    # request.result{ok:false}. A handler returning a (output, output_text) tuple sets both.

class Provider:
    """Announces this agent's capabilities and executes delegated work under the local policy."""
    def __init__(self, client: ParleyClient, workspace: Path, *,
                 policy: Policy | None = None) -> None
    def register(self, cap: Capability, handler: Handler | None = None) -> None
        """handler=None means this capability is fulfilled manually -- the runtime surfaces the
           request to the operator/agent via .parley/requests.json and `parley fulfil`."""
    def register_from_file(self, path: Path) -> int       # .parley/capabilities.json
    def announce(self) -> dict
    def revoke(self, names: list[str]) -> dict
    def on_event(self, event: dict) -> None               # routes request.* addressed to me
    def pump(self, now: float) -> None                    # timeouts, queued work, pending expiry
    def accept(self, req_id: str, eta_s: float = 0) -> dict
    def decline(self, req_id: str, reason: str, code: str = "policy") -> dict
    def fulfil(self, req_id: str, *, output=None, output_text: str = "",
               files: list[str] | None = None, ok: bool = True, error: dict | None = None) -> dict
    @property
    def pending_consent(self) -> list[dict]

class Requester:
    """The calling side."""
    def __init__(self, client: ParleyClient) -> None
    def ask(self, to: str, capability: str, input: dict, *, reason: str,
            timeout_s: int = 300, priority: int = 3) -> str          # -> req_id
    def instruct(self, to: str, instruction: str, *, reason: str,
                 timeout_s: int = 600, expects: str = "text") -> str
    def wait(self, req_id: str, *, timeout_s: float | None = None) -> dict
        """Blocks on the event stream until terminal. Returns the request record. Sets the
           caller's PSR to waiting/blocked_on for the duration, and restores it afterwards."""
    def cancel(self, req_id: str, reason: str = "") -> dict
```

Handlers execute in a bounded worker pool owned by `Runtime`, never on the stream thread. A
handler that hangs must not wedge the agent: enforce `timeout_s` and emit a failed result.

### StateView snapshot additions (hub)

```
"capabilities": <Registry.to_dict(online=…, in_flight=…)>,
"requests": {"in_flight": [<record>, …], "recent": [<record>, …],
             "counts": {"pending": n, "accepted": n, "done": n, "failed": n,
                        "declined": n, "expired": n}}
```
and `graph.edges[].kinds` gains `"delegation": n`.

### Ledger additions

`LedgerLine.components` gains `"service"`, and `evidence` gains a `"service"` list. Negative
service points from abandoned requests appear as their own evidence entries with a clear label —
a penalty the user cannot trace would violate SPEC R6.

## `parley/cli.py`
```python
def main(argv: list[str] | None = None) -> int
```
`parley/__main__.py` is `raise SystemExit(main())`. Every subcommand supports `--json`.
Exit codes per SPEC §11.

## `parley/doctor.py`
```python
@dataclass
class Check: name: str; ok: bool; detail: str; fatal: bool = False
def run_checks(workspace: Path, *, hub_url: str = "") -> list[Check]
def render(checks: list[Check], *, as_json: bool = False) -> str
```

---

## Conventions

- **Logging** via `logging.getLogger("parley.<module>")`. Never `print()` outside `cli.py`.
  Never log a key, token or watchword — `crypto.py` provides no `__repr__` that exposes material.
- **Threads** over asyncio (stdlib `http.server` is thread-based; keeps the 3.9 floor simple).
  Anything shared is guarded by an explicit `threading.Lock`; document the lock order.
- **No global mutable singletons.** Pass `Store`, `HubConfig`, `ParleyClient` explicitly.
- **Type hints everywhere**, but under `from __future__ import annotations`.
- Docstrings explain *why*, not *what*. The spec covers *what*.
