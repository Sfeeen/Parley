"""``parley doctor`` -- SPEC 13.

This is the command you tell somebody to run when anything is wrong, so every
check here does the real thing rather than the convenient thing: it actually
opens an SSE connection and waits for an event to come down it, it actually
round-trips a blob through the Hub, it actually measures clock skew against the
Hub's own clock, and it actually looks at whether the listening socket is exposed
to the public internet without TLS or sealed mode.

A diagnostic that only checks the easy things is worse than no diagnostic, because
it ends the investigation with a clean bill of health.

Every ``Check`` carries a *detail that says what to do about it*.  The status of a
check is derived from ``ok`` plus a small prefix convention on ``detail``:

====================  =======================================================
``ok=False``          **fail**  (``fatal=True`` marks a security-relevant one)
``detail`` ``warning: ...``  **warn** -- works, but somebody should look at it
``detail`` ``skipped: ...``  **skip** -- a precondition was not met
otherwise             **pass**
====================  =======================================================
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["Check", "run_checks", "render", "summarise", "status_of", "exit_code_for"]

WARN_PREFIX = "warning: "
SKIP_PREFIX = "skipped: "

#: SPEC 3.3 -- the Hub rejects a request whose timestamp is further out than this.
MAX_SKEW_S = 300.0
#: Below this we say nothing; above it we nag before authentication starts failing.
NAG_SKEW_S = 30.0


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fatal: bool = False


# --------------------------------------------------------------------------- #
# status helpers
# --------------------------------------------------------------------------- #


def status_of(check: Check) -> str:
    if not check.ok:
        return "fail"
    if check.detail.startswith(WARN_PREFIX):
        return "warn"
    if check.detail.startswith(SKIP_PREFIX):
        return "skip"
    return "pass"


def _pass(name: str, detail: str) -> Check:
    return Check(name, True, detail)


def _warn(name: str, detail: str) -> Check:
    return Check(name, True, WARN_PREFIX + detail)


def _skip(name: str, detail: str) -> Check:
    return Check(name, True, SKIP_PREFIX + detail)


def _fail(name: str, detail: str, fatal: bool = False) -> Check:
    return Check(name, False, detail, fatal)


def summarise(checks: List[Check]) -> Dict[str, Any]:
    """The machine-readable form of a doctor run (also the ``--json`` payload)."""
    rows = []
    counts = {"pass": 0, "fail": 0, "warn": 0, "skip": 0}
    for c in checks:
        st = status_of(c)
        counts[st] += 1
        detail = c.detail
        for prefix in (WARN_PREFIX, SKIP_PREFIX):
            if detail.startswith(prefix):
                detail = detail[len(prefix):]
        rows.append({"name": c.name, "status": st, "ok": c.ok, "fatal": c.fatal, "detail": detail})
    return {
        "checks": rows,
        "summary": counts,
        "healthy": counts["fail"] == 0,
        "python": platform.python_version(),
        "platform": sys.platform,
    }


def exit_code_for(checks: List[Check]) -> int:
    """Most specific exit code for a failing run (SPEC 11)."""
    codes = {"fingerprint": 5, "hub reachable": 4, "credentials": 3}
    worst = 0
    for c in checks:
        if c.ok:
            continue
        lowered = c.name.lower()
        for needle, code in codes.items():
            if needle in lowered:
                return code
        worst = 1
    return worst


# --------------------------------------------------------------------------- #
# small local utilities (duplicated deliberately: doctor must run even when the
# rest of the package is broken -- that is precisely when it is needed)
# --------------------------------------------------------------------------- #


def _parse_rfc3339(text: str) -> float:
    import datetime as _dt

    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(text).timestamp()
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return _dt.datetime.strptime(text, fmt).timestamp()
            except ValueError:
                continue
    raise ValueError("not an RFC 3339 timestamp: %r" % text)


def _human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0 or unit == "TiB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0
    return "%.1f TiB" % n


def _outbound_ip() -> str:
    """The address this machine would use to reach the outside world."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.3)
        sock.connect(("198.51.100.1", 9))  # TEST-NET-3, never routed, never sent to
        return str(sock.getsockname()[0])
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        sock.close()


def _is_private(ip: str) -> bool:
    try:
        import ipaddress

        addr = ipaddress.ip_address(ip)
        return bool(addr.is_private or addr.is_loopback or addr.is_link_local)
    except Exception:
        return True  # unknown -> do not cry wolf


def _http_get(url: str, timeout: float = 6.0, headers: Optional[Dict[str, str]] = None):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    return urllib.request.urlopen(req, timeout=timeout)


# --------------------------------------------------------------------------- #
# the checks
# --------------------------------------------------------------------------- #


def run_checks(workspace: Path, *, hub_url: str = "") -> List[Check]:
    """Run every SPEC 13 check against *workspace*.

    Nothing in here raises: a check that cannot run reports itself as skipped with
    the reason, because a traceback from the diagnostic tool helps nobody.
    """
    workspace = Path(workspace)
    checks: List[Check] = []
    ctx: Dict[str, Any] = {
        "workspace": workspace,
        "creds": None,
        "hub_url": hub_url,
        "hello": None,
        "hello_headers": {},
        "client": None,
        "head_seq": 0,
        "state": None,
        "sealed": False,
        "hub_config": None,
        "policy": {},
    }

    _check_python(checks)
    _check_stdlib(checks)
    _check_parley_modules(checks)
    _check_crypto_backend(checks, ctx)
    _check_workspace(checks, ctx)
    _check_state_dir(checks, ctx)
    _check_credentials(checks, ctx)
    _check_local_hub_config(checks, ctx)
    _check_hub_reachable(checks, ctx)
    _check_version_match(checks, ctx)
    _check_fingerprint(checks, ctx)
    _check_clock_skew(checks, ctx)
    _check_client(checks, ctx)
    _check_sse(checks, ctx)
    _check_longpoll(checks, ctx)
    _check_blob_roundtrip(checks, ctx)
    _check_psr_freshness(checks, ctx)
    _check_ignore_rules(checks, ctx)
    _check_disk(checks, ctx)
    _check_listen_exposure(checks, ctx)

    return checks


# -- environment ------------------------------------------------------------ #


def _check_python(checks: List[Check]) -> None:
    name = "python version"
    version = platform.python_version()
    if sys.version_info < (3, 9):
        checks.append(_fail(
            name,
            "Python %s is below the 3.9 floor. Install Python 3.9 or newer and run "
            "parley with that interpreter (python3.11 -m parley ...)." % version,
        ))
        return
    checks.append(_pass(name, "Python %s on %s (%s)" % (version, sys.platform, platform.machine() or "?")))


def _check_stdlib(checks: List[Check]) -> None:
    name = "stdlib completeness"
    required = [
        "hashlib", "hmac", "secrets", "json", "sqlite3", "socket", "threading",
        "http.server", "urllib.request", "zlib", "gzip", "unicodedata", "ipaddress",
        "base64", "argparse", "dataclasses", "pathlib", "shutil", "ssl",
    ]
    missing = []
    for mod in required:
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    extras = []
    try:
        import hashlib

        if not hasattr(hashlib, "pbkdf2_hmac"):
            extras.append("hashlib.pbkdf2_hmac")
        if "sha256" not in getattr(hashlib, "algorithms_available", ()):
            extras.append("sha256")
    except Exception:  # pragma: no cover - defensive
        pass
    if missing or extras:
        checks.append(_fail(
            name,
            "missing: %s. This Python is a cut-down build; install a full CPython "
            "(sqlite3 and ssl are commonly stripped in minimal containers and "
            "Alpine images -- `apk add python3 sqlite-libs` or use python:3-slim)."
            % ", ".join(missing + extras),
        ))
        return
    checks.append(_pass(name, "all %d required stdlib modules present" % len(required)))


def _check_parley_modules(checks: List[Check]) -> None:
    name = "parley modules"
    wanted = [
        "parley.version", "parley.errors", "parley.jsonutil", "parley.ids",
        "parley.crypto", "parley.protocol", "parley.config", "parley.ledger",
        "parley.hub.server", "parley.client.client", "parley.client.transport",
    ]
    broken = []
    for mod in wanted:
        try:
            __import__(mod)
        except Exception as exc:
            broken.append("%s (%s)" % (mod.split(".", 1)[1], exc.__class__.__name__))
    if broken:
        checks.append(_fail(
            name,
            "cannot import: %s. Run parley from the repository root, or install it "
            "(pip install -e .). A partial checkout gives exactly this." % ", ".join(broken),
        ))
        return
    try:
        from parley.version import __version__, WIRE_VERSION

        checks.append(_pass(name, "parley %s speaking %s, all modules import" % (__version__, WIRE_VERSION)))
    except Exception:
        checks.append(_pass(name, "all modules import"))


def _check_crypto_backend(checks: List[Check], ctx: Dict[str, Any]) -> None:
    name = "crypto backend"
    try:
        from parley import crypto
    except Exception as exc:
        checks.append(_fail(name, "parley.crypto will not import (%s); nothing can be signed." % exc))
        return
    backend = getattr(crypto, "BACKEND", "unknown")
    ctx["backend"] = backend
    if backend == "pure":
        checks.append(_pass(
            name,
            "pure-Python ChaCha20-Poly1305 (~1-3 MB/s). Fine for chat and events. "
            "If you plan to sync large files in sealed mode, `pip install cryptography` "
            "for a 100x speed-up -- it is optional, never required.",
        ))
    elif backend in ("cryptography", "pynacl"):
        checks.append(_pass(name, "%s (compiled AEAD, full speed)" % backend))
    else:
        checks.append(_warn(name, "unrecognised backend %r -- expected cryptography, pynacl or pure." % backend))


# -- workspace -------------------------------------------------------------- #


def _check_workspace(checks: List[Check], ctx: Dict[str, Any]) -> None:
    name = "workspace writable"
    ws = ctx["workspace"]
    if not ws.exists():
        checks.append(_fail(name, "%s does not exist. Create it, or pass --workspace to point at the right folder." % ws))
        return
    if not ws.is_dir():
        checks.append(_fail(name, "%s is not a directory." % ws))
        return
    probe = ws / (".parley-doctor-%d.tmp" % os.getpid())
    try:
        probe.write_bytes(b"parley")
        probe.unlink()
    except Exception as exc:
        checks.append(_fail(
            name,
            "cannot write in %s (%s). Parley syncs files into this folder, so it must "
            "be writable by this user." % (ws, exc),
        ))
        return
    checks.append(_pass(name, str(ws)))


def _check_state_dir(checks: List[Check], ctx: Dict[str, Any]) -> None:
    name = ".parley permissions"
    state = ctx["workspace"] / ".parley"
    if not state.exists():
        checks.append(_skip(
            name,
            "%s does not exist yet -- this workspace has not joined a parley. "
            "Run `parley init` to host one or `parley join` to enter one." % state,
        ))
        return
    if not state.is_dir():
        checks.append(_fail(name, "%s exists but is not a directory. Move it aside." % state))
        return
    if os.name == "nt":
        checks.append(_pass(name, "%s present (POSIX mode bits not applicable on Windows)" % state))
        return
    mode = stat.S_IMODE(state.stat().st_mode)
    if mode & 0o077:
        checks.append(_warn(
            name,
            "%s is mode %04o -- other users on this machine can read it, and it holds "
            "your agent key. Fix: chmod 700 %s" % (state, mode, state),
        ))
        return
    checks.append(_pass(name, "%s is mode %04o" % (state, mode)))


def _check_credentials(checks: List[Check], ctx: Dict[str, Any]) -> None:
    name = "credentials"
    ws = ctx["workspace"]
    path = ws / ".parley" / "credentials.json"
    try:
        from parley.config import Credentials
    except Exception as exc:
        checks.append(_skip(name, "parley.config will not import (%s)." % exc))
        return
    if not path.exists():
        checks.append(_skip(
            name,
            "no %s. You are not enrolled in a parley from this folder. "
            "Run `parley join --hub <url> --invite \"<watchword>\"` (or `parley join "
            "--discover` on the same LAN)." % path,
        ))
        return
    try:
        creds = Credentials.load(ws)
    except Exception as exc:
        checks.append(_fail(
            name,
            "%s exists but will not parse (%s). Delete it and re-join; the Hub will "
            "mint a fresh agent key." % (path, exc),
        ))
        return
    ctx["creds"] = creds
    ctx["sealed"] = bool(getattr(creds, "sealed", False))
    if not ctx["hub_url"]:
        ctx["hub_url"] = getattr(creds, "hub_url", "") or ""
    detail = "%s as %s (%s) on %s" % (
        getattr(creds, "agent_id", "?"),
        getattr(creds, "name", "?"),
        getattr(creds, "kind", "?"),
        getattr(creds, "hub_url", "?"),
    )
    if os.name != "nt":
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                checks.append(_warn(
                    name,
                    "%s -- but the file is mode %04o and holds your agent key. "
                    "Fix: chmod 600 %s" % (detail, mode, path),
                ))
                return
        except OSError:
            pass
    checks.append(_pass(name, detail))


def _check_local_hub_config(checks: List[Check], ctx: Dict[str, Any]) -> None:
    """Not a SPEC 13 line of its own -- it feeds the exposure check below."""
    try:
        from parley.config import HubConfig
    except Exception:
        return
    candidates = []
    env = os.environ.get("PARLEY_STATE_DIR")
    if env:
        candidates.append(Path(env))
    candidates += [ctx["workspace"] / ".parley" / "hub", ctx["workspace"] / ".parley"]
    for cand in candidates:
        try:
            if not (cand / "hub.json").exists():
                continue
            ctx["hub_config"] = HubConfig.load(cand)
            ctx["hub_state_dir"] = cand
            return
        except Exception:
            continue


# -- the Hub ---------------------------------------------------------------- #


def _check_hub_reachable(checks: List[Check], ctx: Dict[str, Any]) -> None:
    name = "hub reachable"
    url = ctx.get("hub_url") or ""
    if not url and ctx.get("hub_config") is not None:
        cfg = ctx["hub_config"]
        host = getattr(cfg, "bind", "127.0.0.1")
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        url = "http://%s:%d" % (host, int(getattr(cfg, "port", 7777)))
        ctx["hub_url"] = url
    if not url:
        checks.append(_skip(
            name,
            "no Hub known. Pass --hub <url>, or join a parley so the URL is stored "
            "in .parley/credentials.json.",
        ))
        return

    started = time.time()
    try:
        resp = _http_get(url.rstrip("/") + "/v1/hello", timeout=8.0)
        raw = resp.read(64 * 1024)
        ctx["hello_headers"] = {k.lower(): v for k, v in resp.headers.items()}
        ctx["hello_recv"] = time.time()
        ctx["hello_rtt"] = ctx["hello_recv"] - started
        ctx["hello"] = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        checks.append(_fail(
            name,
            "%s answered HTTP %s on /v1/hello. Something is listening but it is not a "
            "Parley Hub (a proxy or another service on that port?)." % (url, exc.code),
        ))
        return
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        checks.append(_fail(
            name,
            "cannot reach %s (%s). Check the Hub is running (`parley init` on the host), "
            "that the port is not firewalled, and that you are on the same network. "
            "`parley join --discover` finds a Hub on the LAN without typing an address."
            % (url, reason),
        ))
        return
    except Exception as exc:
        checks.append(_fail(name, "cannot reach %s (%s)." % (url, exc)))
        return

    hello = ctx["hello"] or {}
    ctx["policy"] = hello.get("policy", {}) or {}
    checks.append(_pass(
        name,
        "%s -- parley %r, %s agent(s) online, %.0f ms round trip"
        % (url, hello.get("name", "?"), hello.get("agents_online", "?"), ctx.get("hello_rtt", 0) * 1000),
    ))


def _check_version_match(checks: List[Check], ctx: Dict[str, Any]) -> None:
    name = "/v1/hello version match"
    hello = ctx.get("hello")
    if not hello:
        checks.append(_skip(name, "the Hub did not answer, so there is nothing to compare."))
        return
    try:
        from parley.version import WIRE_VERSION
    except Exception:
        WIRE_VERSION = "PARLEY/1"
    theirs = hello.get("v") or hello.get("version") or ""
    if theirs != WIRE_VERSION:
        checks.append(_fail(
            name,
            "the Hub speaks %r, this client speaks %r. Upgrade whichever side is older; "
            "the wire format is not negotiated." % (theirs, WIRE_VERSION),
        ))
        return
    checks.append(_pass(name, "both sides speak %s" % WIRE_VERSION))


def _check_fingerprint(checks: List[Check], ctx: Dict[str, Any]) -> None:
    name = "fingerprint match"
    hello = ctx.get("hello")
    creds = ctx.get("creds")
    if not hello:
        checks.append(_skip(name, "the Hub did not answer."))
        return
    theirs = hello.get("fingerprint", "")
    if creds is None:
        checks.append(_skip(
            name,
            "not enrolled, so there is no stored fingerprint to compare. The Hub "
            "currently reports %r -- say those three words out loud to the host before "
            "you join." % theirs,
        ))
        return
    ours = getattr(creds, "fingerprint", "")
    if not ours or not theirs:
        checks.append(_warn(name, "one side reported no fingerprint (ours=%r theirs=%r)." % (ours, theirs)))
        return
    if ours != theirs:
        checks.append(_fail(
            name,
            "STOP. Your credentials were issued by a Hub whose fingerprint is %r, but "
            "the Hub at %s says %r. Either the host rebuilt the parley from scratch, or "
            "you are talking to a different Hub than you think. Do not send anything "
            "confidential. Confirm the three words with the host by voice, then delete "
            ".parley/credentials.json and re-join."
            % (ours, ctx.get("hub_url"), theirs),
            fatal=True,
        ))
        return
    checks.append(_pass(name, "%s (matches the stored fingerprint)" % ours))


def _check_clock_skew(checks: List[Check], ctx: Dict[str, Any]) -> None:
    name = "clock skew vs hub"
    hello = ctx.get("hello")
    if not hello:
        checks.append(_skip(name, "the Hub did not answer."))
        return
    stamp = ctx.get("hello_headers", {}).get("x-parley-time") or hello.get("server_time") or ""
    if not stamp:
        checks.append(_warn(
            name,
            "the Hub sent neither an X-Parley-Time header nor server_time, so skew "
            "cannot be measured. That is a Hub bug (SPEC 1.2).",
        ))
        return
    try:
        hub_time = _parse_rfc3339(stamp)
    except ValueError as exc:
        checks.append(_warn(name, "the Hub's time %r is not RFC 3339 (%s)." % (stamp, exc)))
        return
    # Correct for half the round trip: the Hub stamped its clock somewhere in the
    # middle of the exchange, not when we got the bytes.
    local = ctx.get("hello_recv", time.time()) - ctx.get("hello_rtt", 0.0) / 2.0
    skew = local - hub_time
    ctx["skew"] = skew
    direction = "ahead of" if skew > 0 else "behind"
    magnitude = abs(skew)
    if magnitude > MAX_SKEW_S:
        checks.append(_fail(
            name,
            "this machine's clock is %.0f s %s the Hub. Anything over %.0f s makes every "
            "authenticated request fail with 401 stale_timestamp (SPEC 3.3) -- so this is "
            "almost certainly the cause of whatever sent you here. Fix the clock: enable "
            "NTP (`timedatectl set-ntp true`, `w32tm /resync`, or restart the VM's time "
            "sync) and run doctor again." % (magnitude, direction, MAX_SKEW_S),
        ))
        return
    if magnitude > NAG_SKEW_S:
        checks.append(_warn(
            name,
            "%.0f s %s the Hub. Still inside the %.0f s window, but you are one suspended "
            "laptop away from authentication failing. Enable NTP."
            % (magnitude, direction, MAX_SKEW_S),
        ))
        return
    checks.append(_pass(name, "%.2f s %s the Hub (limit %.0f s)" % (magnitude, direction, MAX_SKEW_S)))


def _check_client(checks: List[Check], ctx: Dict[str, Any]) -> None:
    """Build a live authenticated client; everything below needs one."""
    creds = ctx.get("creds")
    if creds is None or not ctx.get("hello"):
        return
    try:
        from parley.client.client import ParleyClient

        client = ParleyClient(ctx["workspace"], creds)
        ctx["client"] = client
        state = client.state()
        ctx["state"] = state
        ctx["head_seq"] = int(state.get("head_seq", 0) or 0)
        ctx["policy"] = state.get("policy", ctx.get("policy") or {}) or {}
    except Exception as exc:
        ctx["client_error"] = "%s: %s" % (exc.__class__.__name__, exc)


def _signed_headers(ctx: Dict[str, Any], method: str, path: str, body: bytes) -> Dict[str, str]:
    from parley import crypto
    from parley.version import WIRE_VERSION

    creds = ctx["creds"]
    ts = str(int(time.time()))
    nonce = crypto.new_nonce_hex()
    sts = crypto.string_to_sign(
        method, path, body, ts, nonce, creds.session, creds.agent_id
    )
    sig = crypto.sign(bytes.fromhex(creds.agent_key_hex), sts)
    return {
        "X-Parley-Version": WIRE_VERSION,
        "X-Parley-Session": creds.session,
        "X-Parley-Agent": creds.agent_id,
        "X-Parley-Timestamp": ts,
        "X-Parley-Nonce": nonce,
        "Authorization": "Parley-HMAC-SHA256 " + sig,
        "Accept": "text/event-stream, application/json",
    }


def _check_sse(checks: List[Check], ctx: Dict[str, Any]) -> None:
    name = "SSE live stream"
    if ctx.get("creds") is None:
        checks.append(_skip(name, "not enrolled; the live stream needs an agent key."))
        return
    if not ctx.get("hello"):
        checks.append(_skip(name, "the Hub did not answer."))
        return
    client = ctx.get("client")
    path = "/v1/stream?since=-1"
    url = ctx["hub_url"].rstrip("/") + path
    try:
        headers = _signed_headers(ctx, "GET", path, b"")
    except Exception as exc:
        checks.append(_fail(name, "cannot sign the stream request (%s)." % exc))
        return

    try:
        resp = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20.0)
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(2048)
        except Exception:
            pass
        checks.append(_fail(
            name,
            "the Hub refused the stream: HTTP %s %s. %s"
            % (exc.code, exc.reason, _hint_for_http(exc.code, body)),
        ))
        return
    except Exception as exc:
        checks.append(_fail(
            name,
            "could not open %s (%s). If a proxy sits between you and the Hub it may be "
            "buffering or rejecting text/event-stream; the long-poll fallback below is "
            "what keeps you working in that case." % (url, exc),
        ))
        return

    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    if ctype and ctype != "text/event-stream":
        checks.append(_fail(
            name,
            "the Hub answered %r instead of text/event-stream -- something in the path "
            "is rewriting the response." % ctype,
        ))
        try:
            resp.close()
        except Exception:
            pass
        return

    # Make something actually happen on the log, so this proves delivery rather
    # than merely proving the socket opened.
    injected = threading.Event()

    def _poke() -> None:
        time.sleep(0.6)
        try:
            if client is not None:
                client.heartbeat()
                injected.set()
        except Exception:
            pass

    poker = threading.Thread(target=_poke, name="parley-doctor-poke", daemon=True)
    poker.start()

    saw_data = False
    saw_ping = False
    deadline = time.time() + 15.0
    try:
        while time.time() < deadline:
            # readline() on the HTTPResponse, not on resp.fp: the response is
            # chunk-encoded and .fp would hand back the chunk framing.
            line = resp.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text.startswith("data:"):
                saw_data = True
                break
            if text.startswith(":"):
                saw_ping = True
    except socket.timeout:
        pass
    except Exception:
        pass
    finally:
        try:
            resp.close()
        except Exception:
            pass

    if saw_data:
        checks.append(_pass(name, "connected and an event arrived within %.0f s" % 15.0))
    elif saw_ping:
        checks.append(_warn(
            name,
            "the stream is open and keep-alive pings arrive, but no event came through "
            "in 15 s%s. Events may not be flowing -- check the Hub's log."
            % ("" if injected.is_set() else " (and this client could not post one to test with)"),
        ))
    else:
        checks.append(_fail(
            name,
            "the stream opened but nothing -- not even a keep-alive -- arrived in 15 s. "
            "A proxy is almost certainly buffering the response. Parley falls back to "
            "long-polling automatically; if that check passes you are still fully "
            "functional, just a little less immediate.",
        ))


def _check_longpoll(checks: List[Check], ctx: Dict[str, Any]) -> None:
    name = "long-poll fallback"
    if ctx.get("creds") is None or not ctx.get("hello"):
        checks.append(_skip(name, "not enrolled, or the Hub did not answer."))
        return
    since = max(0, int(ctx.get("head_seq", 0)) - 1)
    path = "/v1/events?since=%d&limit=1&wait=2" % since
    url = ctx["hub_url"].rstrip("/") + path
    started = time.time()
    try:
        headers = _signed_headers(ctx, "GET", path, b"")
        resp = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15.0)
        raw = resp.read(256 * 1024)
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(2048)
        except Exception:
            pass
        checks.append(_fail(
            name,
            "HTTP %s on /v1/events. %s" % (exc.code, _hint_for_http(exc.code, body)),
        ))
        return
    except Exception as exc:
        checks.append(_fail(
            name,
            "long-poll request failed (%s). With both SSE and long-poll down you cannot "
            "receive anything -- check the network path to %s." % (exc, ctx["hub_url"]),
        ))
        return
    elapsed = time.time() - started
    count = -1
    if not ctx.get("sealed"):
        try:
            payload = json.loads(raw.decode("utf-8"))
            events = payload.get("events", payload if isinstance(payload, list) else [])
            count = len(events)
        except Exception:
            count = -1
    detail = "answered in %.2f s" % elapsed
    if count >= 0:
        detail += " with %d event(s)" % count
    checks.append(_pass(name, detail + " -- hostile proxies will not cut you off"))


def _check_blob_roundtrip(checks: List[Check], ctx: Dict[str, Any]) -> None:
    name = "blob round-trip"
    client = ctx.get("client")
    if client is None:
        reason = ctx.get("client_error")
        if reason:
            checks.append(_fail(
                name,
                "could not build an authenticated client (%s). Credentials may be stale: "
                "delete .parley/credentials.json and re-join." % reason,
            ))
        else:
            checks.append(_skip(name, "not enrolled, or the Hub did not answer."))
        return
    payload = b"parley doctor " + os.urandom(48)
    started = time.time()
    try:
        blob_hash = client.put_blob(payload)
        got = client.get_blob(blob_hash)
    except Exception as exc:
        checks.append(_fail(
            name,
            "upload/download failed (%s: %s). File sync cannot work until this does -- "
            "check free disk on the Hub and that max_blob_bytes is not set absurdly low."
            % (exc.__class__.__name__, exc),
        ))
        return
    if got != payload:
        checks.append(_fail(
            name,
            "the Hub returned %d bytes that do not match the %d uploaded. Something is "
            "corrupting bodies in transit (a transcoding proxy?). Do not trust file sync "
            "until this is resolved." % (len(got or b""), len(payload)),
        ))
        return
    checks.append(_pass(name, "%d bytes up and back in %.0f ms (%s)" % (
        len(payload), (time.time() - started) * 1000, blob_hash[:19] + "...")))


def _check_psr_freshness(checks: List[Check], ctx: Dict[str, Any]) -> None:
    name = "PSR freshness"
    state = ctx.get("state")
    if not state:
        checks.append(_skip(name, "no /v1/state snapshot (not enrolled, or the Hub did not answer)."))
        return
    policy = ctx.get("policy") or {}
    max_age = float(policy.get("psr_max_age_s", 30) or 30)
    creds = ctx.get("creds")
    me = getattr(creds, "agent_id", "") if creds else ""
    agents = state.get("agents", []) or []
    missing, stale, mine = [], [], None
    for agent in agents:
        if (agent.get("status") or "active") == "revoked":
            continue
        psr = agent.get("psr")
        label = agent.get("name") or agent.get("agent_id") or "?"
        if agent.get("agent_id") == me:
            mine = psr
        if not psr:
            if agent.get("online", True):
                missing.append(label)
            continue
        if psr.get("stale") or float(psr.get("age_s") or 0) > 3 * max_age:
            if agent.get("online", True):
                stale.append("%s (%.0fs)" % (label, float(psr.get("age_s") or 0)))
    if mine is None and me:
        checks.append(_fail(
            name,
            "you have never emitted a Parley Standing Report. Every other participant "
            "sees you as non-conforming on the Deck. Fix it now: "
            "`parley status \"what you are doing\"` -- and run `parley run` so it is "
            "refreshed every %.0f s automatically." % max_age,
        ))
        return
    problems = []
    if missing:
        problems.append("no PSR at all: " + ", ".join(missing))
    if stale:
        problems.append("stale (over %.0fs): %s" % (3 * max_age, ", ".join(stale)))
    if problems:
        checks.append(_warn(
            name,
            "%s. Those agents are not running `parley run`, or their client is wedged. "
            "The Deck marks them visibly." % "; ".join(problems),
        ))
        return
    age = float((mine or {}).get("age_s") or 0)
    checks.append(_pass(
        name,
        "all %d agent(s) reporting; yours is %.0f s old (limit %.0f s)" % (len(agents), age, max_age),
    ))


def _check_ignore_rules(checks: List[Check], ctx: Dict[str, Any]) -> None:
    name = "ignore-rule sanity"
    try:
        from parley.client.ignore import IgnoreRules
    except Exception as exc:
        checks.append(_skip(name, "parley.client.ignore will not import (%s)." % exc))
        return
    ws = ctx["workspace"]
    try:
        rules = IgnoreRules.load(ws)
    except Exception as exc:
        checks.append(_fail(
            name,
            "%s/.parleyignore could not be parsed (%s). Until it parses, Parley cannot "
            "know what to skip." % (ws, exc),
        ))
        return
    must_ignore = [".git/config", ".parley/credentials.json", "__pycache__/x.pyc", "node_modules/left-pad/index.js"]
    leaked = [p for p in must_ignore if not _ignored(rules, p)]
    if leaked:
        checks.append(_fail(
            name,
            "these would be synced to every other participant: %s. .git and .parley in "
            "particular must never leave this machine -- .parley holds your agent key. "
            "Check .parleyignore for a stray '!' negation." % ", ".join(leaked),
        ))
        return
    ordinary = ["README.md", "src/main.py", "docs/notes.txt"]
    swallowed = [p for p in ordinary if _ignored(rules, p)]
    if swallowed:
        checks.append(_warn(
            name,
            "your .parleyignore also excludes ordinary files (%s). Nothing will sync and "
            "it will look like Parley is broken. A bare '*' line does this."
            % ", ".join(swallowed),
        ))
        return
    has_file = (ws / ".parleyignore").exists()
    checks.append(_pass(
        name,
        "built-in rules active%s; .git, .parley, __pycache__ and node_modules are excluded"
        % (" plus .parleyignore" if has_file else ""),
    ))


def _ignored(rules, path: str) -> bool:
    try:
        return bool(rules.ignored(path))
    except Exception:
        return False


def _check_disk(checks: List[Check], ctx: Dict[str, Any]) -> None:
    name = "free disk"
    target = ctx["workspace"] if ctx["workspace"].exists() else Path.cwd()
    try:
        usage = shutil.disk_usage(str(target))
    except Exception as exc:
        checks.append(_skip(name, "cannot stat the filesystem at %s (%s)." % (target, exc)))
        return
    free = usage.free
    max_blob = float((ctx.get("policy") or {}).get("max_blob_bytes", 26214400) or 26214400)
    if free < 100 * 1024 * 1024:
        checks.append(_fail(
            name,
            "%s free on %s. Parley stores every synced blob here; below 100 MiB a sync "
            "will fail mid-write. Free some space." % (_human_bytes(free), target),
        ))
        return
    if free < 20 * max_blob:
        checks.append(_warn(
            name,
            "%s free on %s -- under 20 max-size blobs (%s each). Fine for chat, tight "
            "for file sync." % (_human_bytes(free), target, _human_bytes(max_blob)),
        ))
        return
    checks.append(_pass(name, "%s free on %s" % (_human_bytes(free), target)))


def _check_listen_exposure(checks: List[Check], ctx: Dict[str, Any]) -> None:
    name = "listening address"
    cfg = ctx.get("hub_config")
    url = ctx.get("hub_url") or ""
    scheme = urllib.parse.urlparse(url).scheme if url else ""
    sealed = bool(ctx.get("sealed")) or bool((ctx.get("policy") or {}).get("sealed"))

    if cfg is None:
        # Not the host. Still worth saying something about how we are connected.
        if not url:
            checks.append(_skip(name, "no Hub is hosted from this workspace and none is configured."))
            return
        host = urllib.parse.urlparse(url).hostname or ""
        if scheme == "https":
            checks.append(_pass(name, "connected to %s over TLS" % url))
        elif host and not _is_private(host):
            if sealed:
                checks.append(_pass(name, "connected to %s over plain HTTP with sealed bodies" % url))
            else:
                checks.append(_warn(
                    name,
                    "you are connected to %s -- a public address over plain HTTP, unsealed. "
                    "Request signing still prevents forgery and replay, but anyone on the "
                    "path can READ every message. Ask the host for a TLS tunnel or sealed "
                    "mode (--seal)." % url,
                ))
        else:
            checks.append(_pass(name, "connected to %s (private address)" % url))
        return

    bind = str(getattr(cfg, "bind", "127.0.0.1"))
    port = int(getattr(cfg, "port", 7777))
    sealed = sealed or bool((getattr(cfg, "policy", {}) or {}).get("sealed"))
    outbound = _outbound_ip()

    # Can we actually connect to our own listening socket?
    probe_host = "127.0.0.1" if bind in ("0.0.0.0", "::", "") else bind
    reachable = False
    try:
        with socket.create_connection((probe_host, port), timeout=3.0):
            reachable = True
    except Exception:
        reachable = False

    if not reachable:
        checks.append(_fail(
            name,
            "nothing is accepting connections on %s:%d, although a Hub is configured here. "
            "The Hub is not running -- start it with `parley init` (or it crashed; check "
            "the terminal you started it in)." % (probe_host, port),
        ))
        return

    wide = bind in ("0.0.0.0", "::", "")
    if wide and not _is_private(outbound) and scheme != "https" and not sealed:
        checks.append(_fail(
            name,
            "DANGER: this Hub is bound to %s:%d and this machine's outbound address %s is "
            "a PUBLIC internet address, with no TLS and no sealed mode. Every chat message, "
            "every file and every standing report crosses the internet in clear text, and "
            "/v1/enroll is open to the world -- anyone who learns the watchword joins. "
            "Fix one of: restart with --seal; put it behind a TLS tunnel (scripts/tunnel.sh, "
            "docs/DEPLOY.md); or bind to the private interface with --bind %s."
            % (bind, port, outbound, "127.0.0.1"),
            fatal=True,
        ))
        return
    if wide and not _is_private(outbound):
        checks.append(_warn(
            name,
            "bound to %s:%d on a machine with the public address %s. %s is protecting "
            "content, but enrolment is still exposed to the internet -- use --approve and "
            "a short enroll_ttl_s."
            % (bind, port, outbound, "TLS" if scheme == "https" else "Sealed mode"),
        ))
        return
    if wide:
        checks.append(_pass(
            name,
            "bound to %s:%d, reachable on this LAN as %s:%d. Private network, request "
            "signing active%s." % (bind, port, outbound, port, ", bodies sealed" if sealed else ""),
        ))
        return
    checks.append(_pass(name, "bound to %s:%d and accepting connections" % (bind, port)))


def _hint_for_http(code: int, body: bytes) -> str:
    """Turn a SPEC 12 error envelope into advice."""
    parsed = {}
    try:
        parsed = (json.loads(body.decode("utf-8")) or {}).get("error", {})
    except Exception:
        parsed = {}
    hint = parsed.get("hint") or ""
    error_code = parsed.get("code") or ""
    canned = {
        401: "Your agent key is not accepted. If `clock skew` above failed, fix the clock first "
             "-- that is the usual cause. Otherwise delete .parley/credentials.json and re-join.",
        403: "The Hub refused this agent. If you are pending_approval the host must run "
             "`parley approve <your agent id>`; if you were revoked you need a fresh invite.",
        404: "The Hub does not know this session. It was probably restarted from a clean state "
             "directory -- re-join with a current watchword.",
        429: "You are rate limited. Back off and retry; `parley run` honours Retry-After itself.",
        503: "The Hub is shutting down.",
    }
    parts = [p for p in (("%s: %s" % (error_code, hint)) if error_code and hint else error_code or hint,
                         canned.get(code, "")) if p]
    return " ".join(parts) or "No detail returned."


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def render(checks: List[Check], *, as_json: bool = False) -> str:
    """Human table, or the exact JSON envelope the CLI prints for ``--json``."""
    payload = summarise(checks)
    code = exit_code_for(checks)
    if as_json:
        return json.dumps(
            {
                "ok": payload["healthy"],
                "command": "doctor",
                "exit_code": code,
                "data": payload,
            },
            indent=2,
            ensure_ascii=False,
        )

    from parley.term import Term

    t = Term(sys.stdout)
    width = t.layout_width(84)
    lines: List[str] = []
    lines.append(t.rule("parley doctor", width))
    lines.append("")

    marks = {
        "pass": (t.g["pass"], "green", "ok  "),
        "fail": (t.g["fail"], "red", "FAIL"),
        "warn": (t.g["warn"], "yellow", "warn"),
        "skip": (t.g["skip"], "grey", "skip"),
    }
    name_w = max([visible_width_safe(c.name) for c in checks] + [10])
    for check in checks:
        st = status_of(check)
        glyph, colour, label = marks[st]
        detail = check.detail
        for prefix in (WARN_PREFIX, SKIP_PREFIX):
            if detail.startswith(prefix):
                detail = detail[len(prefix):]
        head = "%s %s  %s" % (
            t.paint(glyph, colour, "bold"),
            t.paint(label, colour),
            t.bold(check.name) + " " * max(0, name_w - visible_width_safe(check.name)),
        )
        indent = 2 + 1 + 1 + 4 + 2 + name_w + 2
        wrapped = _wrap(detail, max(24, width - indent))
        lines.append("  " + head + "  " + (wrapped[0] if wrapped else ""))
        for extra in wrapped[1:]:
            lines.append(" " * indent + extra)
        if st == "fail" and check.fatal:
            lines.append(" " * indent + t.paint("^ this one is a security problem, not a nuisance.", "red", "bold"))
    lines.append("")

    counts = payload["summary"]
    tally = "  ".join(
        "%s %d" % (k, counts[k]) for k in ("pass", "warn", "skip", "fail") if counts[k]
    )
    if payload["healthy"]:
        verdict = t.paint("  healthy", "green", "bold")
        if counts["warn"]:
            verdict += t.dim("  -- but read the %d warning(s) above" % counts["warn"])
    else:
        verdict = t.paint("  %d check(s) FAILED" % counts["fail"], "red", "bold")
        verdict += t.dim("  -- each line above says what to do about it")
    lines.append(verdict)
    lines.append(t.dim("  " + tally + "   exit code %d" % code))
    return "\n".join(lines)


def visible_width_safe(text: str) -> int:
    try:
        from parley.term import visible_width

        return visible_width(text)
    except Exception:  # pragma: no cover - defensive
        return len(text)


def _wrap(text: str, width: int) -> List[str]:
    words = text.split()
    if not words:
        return [""]
    out, line = [], ""
    for word in words:
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= width:
            line += " " + word
        else:
            out.append(line)
            line = word
    out.append(line)
    return out
