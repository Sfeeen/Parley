"""The Parley command line.

Design notes that matter if you are changing this file:

* **Two audiences, one command.**  Every subcommand supports ``--json`` and emits a
  single well-formed JSON object on stdout (``watch`` is the documented exception:
  it streams one JSON object per line).  In JSON mode *all* human chatter goes to
  stderr, so a consumer can pipe stdout straight into ``jq`` or ``json.loads``.
  LLM agents are the primary users of this CLI; none of them should ever have to
  parse prose.

* **Exit codes are an API** (SPEC 11): 0 ok, 1 error, 2 usage, 3 auth/credential,
  4 cannot reach the Hub, 5 fingerprint mismatch.  Agents branch on these.

* **Everything below ``parley.cli`` is imported lazily**, inside the command that
  needs it.  That keeps ``parley --help`` -- and most of ``parley doctor`` --
  working on a half-installed or half-built tree, which is exactly when somebody
  needs the help text.

* **Secrets.**  The watchword, the host token and agent keys are printed by
  ``init`` and ``invite --reveal``/``--rotate`` and nowhere else, ever.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from parley.term import Term, truncate, visible_width

# --------------------------------------------------------------------------- #
# Exit codes -- SPEC 11
# --------------------------------------------------------------------------- #

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_NO_HUB = 4
EXIT_FINGERPRINT = 5

#: SPEC 12 error code -> CLI exit code.
_CODE_EXIT = {
    "bad_signature": EXIT_AUTH,
    "unknown_agent": EXIT_AUTH,
    "stale_timestamp": EXIT_AUTH,
    "replayed_nonce": EXIT_AUTH,
    "pending_approval": EXIT_AUTH,
    "revoked": EXIT_AUTH,
    "enroll_closed": EXIT_AUTH,
    "host_token_required": EXIT_AUTH,
    "read_only_token": EXIT_AUTH,
    "no_such_session": EXIT_NO_HUB,
    "fingerprint_mismatch": EXIT_FINGERPRINT,
    "fingerprint_changed": EXIT_FINGERPRINT,
    "transport": EXIT_NO_HUB,
}

KNOWLEDGE_KINDS = ("decision", "design", "finding", "review", "doc", "code", "fix", "answer")
PSR_STATES = ("idle", "planning", "working", "reviewing", "blocked", "waiting", "offline")
TASK_STATUSES = ("todo", "doing", "blocked", "review", "done")

DISCOVERY_PORT = 7778
DISCOVERY_PROBE = b"PARLEY/1 DISCOVER"

_NATO = {
    "a": "Alfa", "b": "Bravo", "c": "Charlie", "d": "Delta", "e": "Echo", "f": "Foxtrot",
    "g": "Golf", "h": "Hotel", "i": "India", "j": "Juliett", "k": "Kilo", "l": "Lima",
    "m": "Mike", "n": "November", "o": "Oscar", "p": "Papa", "q": "Quebec", "r": "Romeo",
    "s": "Sierra", "t": "Tango", "u": "Uniform", "v": "Victor", "w": "Whiskey",
    "x": "X-ray", "y": "Yankee", "z": "Zulu",
}

_JSON_MODE = [False]  # set before argument parsing so usage errors can be JSON too


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class CliError(Exception):
    """An error with everything needed to render it for a human *or* a machine."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "error",
        exit_code: int = EXIT_ERROR,
        hint: str = "",
        detail: Optional[dict] = None,
        loud: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code
        self.hint = hint
        self.detail = detail or {}
        self.loud = loud

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message, "retryable": self.exit_code == EXIT_NO_HUB}
        if self.hint:
            out["hint"] = self.hint
        if self.detail:
            out["detail"] = self.detail
        return out


def _translate(exc: BaseException) -> CliError:
    """Map anything the lower layers throw onto a CLI error with an exit code."""
    import urllib.error

    # A parley.errors.ParleyError (duck-typed so this module never needs the import).
    code = getattr(exc, "code", None)
    if isinstance(code, str) and hasattr(exc, "to_dict"):
        detail = {}
        try:
            detail = (exc.to_dict() or {}).get("error", {}) or {}
        except Exception:
            pass
        return CliError(
            detail.get("message") or str(exc) or code,
            code=code,
            exit_code=_CODE_EXIT.get(code, EXIT_ERROR),
            hint=detail.get("hint", "") or _canned_hint(code),
            detail=detail.get("detail") or {},
            loud=code in ("fingerprint_mismatch", "fingerprint_changed"),
        )

    if isinstance(exc, urllib.error.HTTPError):
        body = b""
        try:
            body = exc.read(4096)
        except Exception:
            pass
        env = {}
        try:
            env = (json.loads(body.decode("utf-8")) or {}).get("error", {}) or {}
        except Exception:
            env = {}
        ecode = env.get("code") or "http_%d" % exc.code
        return CliError(
            env.get("message") or ("the Hub answered HTTP %d %s" % (exc.code, exc.reason)),
            code=ecode,
            exit_code=_CODE_EXIT.get(ecode, EXIT_AUTH if exc.code in (401, 403) else EXIT_ERROR),
            hint=env.get("hint") or _canned_hint(ecode),
            detail={"http_status": exc.code},
        )

    if isinstance(exc, (urllib.error.URLError, socket.timeout, ConnectionError, OSError)):
        reason = getattr(exc, "reason", exc)
        return CliError(
            "cannot reach the Hub (%s)" % reason,
            code="unreachable",
            exit_code=EXIT_NO_HUB,
            hint="Check the Hub is running and the port is open. `parley join --discover` "
                 "finds a Hub on the LAN; `parley doctor` tells you which hop is broken.",
        )

    return CliError("%s: %s" % (exc.__class__.__name__, exc), code="error", exit_code=EXIT_ERROR)


def _canned_hint(code: str) -> str:
    return {
        "bad_signature": "Either the watchword is wrong or your clock is off by more than "
                         "300 s. Run `parley doctor` -- it measures the skew for you.",
        "stale_timestamp": "This machine's clock is more than 300 s from the Hub's. Enable NTP.",
        "pending_approval": "The host has to let you in: ask them to run `parley approve <your agent id>`.",
        "enroll_closed": "The Hub is no longer accepting new participants. Ask the host to "
                         "run `parley invite --rotate` for a fresh watchword.",
        "revoked": "Your access was revoked by the host. You need a new invite.",
        "host_token_required": "This is a host-only action and needs the host token from the "
                               "machine that ran `parley init`.",
        "rate_limited": "Slow down and retry; the Hub sent a Retry-After.",
        "too_large": "That exceeds the Hub's size limit (max_blob_bytes, 25 MiB by default).",
    }.get(code, "")


# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #


class Ctx:
    """Per-invocation output and option state."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.json = bool(getattr(args, "json", False))
        colour = getattr(args, "colour", None)
        unicode_ = False if getattr(args, "ascii", False) else None
        # parley.doctor.render() has a contract-fixed signature and builds its own
        # Term, so the only way to hand it --ascii/--no-color is the environment
        # that parley.term already consults. Process-local, so nothing leaks out.
        if unicode_ is False:
            os.environ["PARLEY_ASCII"] = "1"
        if colour is False:
            os.environ["PARLEY_NO_COLOR"] = "1"
        elif colour is True:
            os.environ.setdefault("FORCE_COLOR", "1")
        self.out = Term(sys.stdout, colour=colour, unicode=unicode_)
        self.err = Term(sys.stderr, colour=colour, unicode=unicode_)
        self.timeout = float(getattr(args, "timeout", 15.0) or 15.0)
        self.verbose = bool(getattr(args, "verbose", False))
        self.quiet = bool(getattr(args, "quiet", False))
        self.streaming = False  # commands that print their own stdout stream

    @property
    def human(self) -> bool:
        return not self.json and not self.quiet

    def say(self, *lines) -> None:
        if self.human:
            self.out.write(*lines)

    def note(self, text: str) -> None:
        """Human chatter that must not pollute stdout in JSON mode."""
        if self.quiet:
            return
        target = self.err if self.json else self.out
        target.write(target.dim(text))

    def warn(self, text: str) -> None:
        t = self.err
        t.write(t.paint("warning: ", "yellow", "bold") + text)


def _emit_json(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Lazy imports
# --------------------------------------------------------------------------- #


def _mod(name: str):
    import importlib

    try:
        return importlib.import_module(name)
    except Exception as exc:  # ImportError, or a broken module body
        raise CliError(
            "cannot load %s (%s: %s)" % (name, exc.__class__.__name__, exc),
            code="module_unavailable",
            hint="Run parley from the repository root, or install it with `pip install -e .`. "
                 "`parley doctor` lists exactly which modules will not import.",
        )


# --------------------------------------------------------------------------- #
# Network helpers
# --------------------------------------------------------------------------- #


def lan_ip() -> str:
    """The address other machines on this network would use to reach us.

    Opening a UDP socket toward a never-routed TEST-NET address makes the kernel
    pick the real outbound interface without sending a single packet.  This is why
    `init` can print a URL a colleague can actually type, instead of 0.0.0.0.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.3)
        sock.connect(("198.51.100.1", 9))
        return str(sock.getsockname()[0])
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        sock.close()


def is_private_ip(host: str) -> bool:
    try:
        import ipaddress

        addr = ipaddress.ip_address(host)
        return bool(addr.is_private or addr.is_loopback or addr.is_link_local)
    except Exception:
        return True  # a hostname, or something unparseable -- do not cry wolf


def public_url(url: str, *, ip: str = "") -> str:
    """Replace a wildcard/loopback host in *url* with this machine's LAN address."""
    if not url:
        return url
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if host not in ("0.0.0.0", "::", "", "127.0.0.1", "localhost", "::1"):
        return url
    replacement = ip or lan_ip()
    netloc = replacement
    if parts.port:
        netloc = "%s:%d" % (replacement, parts.port)
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def discover_hubs(timeout: float = 2.5, port: int = DISCOVERY_PORT) -> List[dict]:
    """UDP broadcast probe for Hubs on this LAN (SPEC: hub/server.py, port 7778)."""
    found: Dict[str, dict] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        sock.settimeout(0.3)
        targets = ["255.255.255.255", "127.0.0.1"]
        local = lan_ip()
        if local and local != "127.0.0.1" and local.count(".") == 3:
            targets.append(local.rsplit(".", 1)[0] + ".255")
        for attempt in range(2):
            for target in targets:
                try:
                    sock.sendto(DISCOVERY_PROBE, (target, port))
                except OSError:
                    continue
            deadline = time.time() + (timeout / 2.0)
            while time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    info = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                if not isinstance(info, dict) or "session" not in info:
                    continue
                info["url"] = public_url(str(info.get("url") or ""), ip=addr[0]) or ("http://%s:7777" % addr[0])
                info["source"] = addr[0]
                found.setdefault(str(info["session"]), info)
            if found and attempt == 0:
                break
    finally:
        sock.close()
    return list(found.values())


def hub_hello(hub_url: str, timeout: float = 8.0) -> dict:
    """Unauthenticated ``GET /v1/hello`` -- reachability, version and fingerprint."""
    import urllib.request

    url = hub_url.rstrip("/") + "/v1/hello"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
            raw = resp.read(256 * 1024)
            info = json.loads(raw.decode("utf-8"))
            if isinstance(info, dict):
                info.setdefault("server_time", resp.headers.get("X-Parley-Time", ""))
            return info
    except Exception as exc:
        err = _translate(exc)
        if err.exit_code == EXIT_NO_HUB:
            err.message = "cannot reach a Parley Hub at %s (%s)" % (hub_url, getattr(exc, "reason", exc))
        raise err


# --------------------------------------------------------------------------- #
# Workspace / credentials
# --------------------------------------------------------------------------- #


def _workspace(args: argparse.Namespace) -> Path:
    raw = getattr(args, "workspace", None) or os.environ.get("PARLEY_WORKSPACE") or ""
    if raw:
        return Path(raw).expanduser()
    try:
        return Path(_mod("parley.config").default_workspace())
    except Exception:
        return Path.cwd()


def _load_credentials(workspace: Path):
    path = workspace / ".parley" / "credentials.json"
    if not path.exists():
        # Checked before importing parley.config so a missing credential always
        # reports as exit 3, never as a module problem.
        raise CliError(
            "no Parley credentials in %s" % workspace,
            code="no_credentials",
            exit_code=EXIT_AUTH,
            hint="You are not in a parley from this folder. Join one with "
                 "`parley join --hub <url> --invite \"<watchword>\"`, or "
                 "`parley join --discover --invite \"<watchword>\"` on the same LAN. "
                 "Use --workspace if the parley lives in a different folder.",
            detail={"expected": str(path)},
        )
    config = _mod("parley.config")
    try:
        return config.Credentials.load(workspace)
    except Exception as exc:
        raise CliError(
            "%s exists but could not be read (%s)" % (path, exc),
            code="bad_credentials",
            exit_code=EXIT_AUTH,
            hint="Delete the file and re-join; the Hub will mint a fresh agent key.",
        )


def _client(workspace: Path):
    creds = _load_credentials(workspace)
    client_mod = _mod("parley.client.client")
    try:
        return client_mod.ParleyClient(workspace, creds), creds
    except Exception as exc:
        raise _translate(exc)


def _hub_state_dir(workspace: Path) -> Optional[Path]:
    env = os.environ.get("PARLEY_STATE_DIR")
    candidates = [Path(env)] if env else []
    candidates += [workspace / ".parley" / "hub", workspace / ".parley"]
    for cand in candidates:
        if (cand / "hub.json").exists():
            return cand
    return None


def _hub_config(workspace: Path):
    """Load the Hub's own config -- only possible on the machine that hosts it."""
    state_dir = _hub_state_dir(workspace)
    if state_dir is None:
        raise CliError(
            "no Hub is hosted from %s" % workspace,
            code="not_the_host",
            exit_code=EXIT_ERROR,
            hint="This command needs the host token, which only exists on the machine that "
                 "ran `parley init`. Run it there, or use --workspace to point at the "
                 "folder the Hub was created in.",
        )
    config = _mod("parley.config")
    try:
        return config.HubConfig.load(state_dir), state_dir
    except Exception as exc:
        raise CliError(
            "%s/hub.json could not be read (%s)" % (state_dir, exc),
            code="bad_hub_config",
            exit_code=EXIT_ERROR,
        )


def _admin_request(hub_url: str, host_token: str, path: str, payload: Optional[dict], timeout: float) -> dict:
    """Call a ``/v1/admin/*`` endpoint with the host token.

    SPEC 3.7 pins the host token to exactly one carrier:
    ``Authorization: Parley-Host <token>``.  Not a query string (it would land in
    proxy and browser logs) and not a bespoke ``X-Parley-Host-Token`` header --
    sending it two ways doubles the exposure surface and guarantees the two paths
    eventually diverge.
    """
    import urllib.request

    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        hub_url.rstrip("/") + path,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Parley-Host " + host_token,
            "X-Parley-Version": "PARLEY/1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(1024 * 1024)
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise _translate(exc)


# --------------------------------------------------------------------------- #
# The invite banner -- the single most important screen in Parley
# --------------------------------------------------------------------------- #


def _spell_aloud(watchword: str, dot: str = "·") -> str:
    """The watchword as a human should pronounce it: words apart, caps for the
    five that carry entropy, lower case for the filler 'the'."""
    words = [w for w in watchword.split("-") if w]
    rendered = [w if w == "the" else w.upper() for w in words]
    return ("  " + dot + "  ").join(rendered)


def _phonetic_lines(watchword: str) -> List[str]:
    out = []
    for word in watchword.split("-"):
        if not word or word == "the":
            continue
        spelled = " ".join(_NATO.get(ch, ch.upper()) for ch in word)
        out.append("%-10s %s" % (word, spelled))
    return out


def render_invite(
    t: Term,
    *,
    name: str,
    session: str,
    watchword: str,
    fingerprint: str,
    hub_url: str,
    deck_url: str,
    workspace: str,
    bind: str,
    port: int,
    policy: Dict[str, Any],
    warnings: Sequence[str] = (),
    phonetic: bool = False,
    host_token_hint: str = "",
    agents_online: Optional[int] = None,
    heading: str = "a parley is open",
) -> List[str]:
    """The `init` / `invite --rotate` screen.

    A human is about to read this down a phone line, so the watchword gets a frame
    of its own, a spelled-out pronunciation line, and nothing competing with it.
    """
    width = t.layout_width(80)
    g = t.g
    dot = g["dot"]
    lines: List[str] = []

    # -- header ---------------------------------------------------------- #
    title = t.paint(" ".join("PARLEY"), "bold", "cyan")
    sub = name or "untitled parley"
    header = [
        title + "   " + t.bold(sub),
        t.dim("%s  %s  %s%s" % (
            session, dot, heading,
            "" if agents_online is None else ("%s %d agent(s) here" % ("  " + dot + "  ", agents_online)),
        )),
    ]
    lines += t.box(header, width=width, double=True, style="cyan")
    lines.append("")

    # -- loud warnings go directly under the header ------------------------ #
    for text in warnings:
        body = [t.paint(line, "red", "bold") for line in _wrap(text, width - 8)]
        lines += t.box(
            [t.paint("!! " + "SECURITY WARNING", "red", "bold")] + [""] + body + [""],
            width=width, double=True, style="red",
        )
        lines.append("")

    # -- 1. the watchword -------------------------------------------------- #
    lines.append("  " + g["n1"] + " " + t.bold("SAY THIS OUT LOUD") + t.dim("  -- the watchword, the only way in"))
    inner = width - 4
    content = [
        "",
        t.centre(t.paint(watchword, "bold", "byellow"), inner - 6),
        "",
        t.centre(t.dim(_spell_aloud(watchword, dot)), inner - 6),
        "",
    ]
    if phonetic:
        content += [t.dim("  spelling:")] + ["  " + t.dim(line) for line in _phonetic_lines(watchword)] + [""]
    lines += t.box(content, width=inner, indent=2, style="yellow")
    lines.append(t.dim("     Anyone who hears these words can join this parley. It is an enrolment"))
    lines.append(t.dim("     secret only -- it is never asked for again, and never appears on the Deck."))
    lines.append("")

    # -- 2. the fingerprint ------------------------------------------------ #
    lines.append("  " + g["n2"] + " " + t.bold("COMPARE THESE THREE WORDS") + t.dim("  -- out loud, both of you"))
    lines.append("")
    lines.append("       " + t.paint(fingerprint or "(none)", "bold", "bcyan"))
    lines.append("")
    lines.append(t.dim("     Their terminal prints three words too when they join. Read yours out."))
    lines.append(t.dim("     Same words = the same parley. Different words = STOP: one of you is"))
    lines.append(t.dim("     connected to a Hub that is not the other's. Do not carry on."))
    lines.append("")

    # -- 3. the command ---------------------------------------------------- #
    join_cmd = 'parley join --hub %s --invite "%s"' % (hub_url, watchword)
    lines.append("  " + g["n3"] + " " + t.bold("THEY RUN THIS") + t.dim("  -- one line, nothing else to configure"))
    lines.append("")
    lines.append("       " + t.paint(join_cmd, "bold", "green"))
    lines.append("")
    lines.append(t.dim("     On this network they can skip the address entirely:"))
    lines.append(t.dim('       parley join --discover --invite "%s"' % watchword))
    lines.append(t.dim("     That line contains the watchword -- send it like a password, or just"))
    lines.append(t.dim("     read the words out and let them type it."))
    lines.append("")

    # -- the facts --------------------------------------------------------- #
    lines.append(t.rule("", width))
    facts: List[Tuple[str, str]] = []
    if deck_url:
        facts.append(("Deck", t.paint(deck_url, "underline") + t.dim("   (open in a browser)")))
    facts.append(("Hub", hub_url + t.dim("   bound %s:%d" % (bind, port))))
    facts.append(("Workspace", workspace))
    facts.append(("Session", session))
    bits = []
    bits.append("enrolment open" if policy.get("enroll_open", True) else "enrolment CLOSED")
    if policy.get("require_approval"):
        bits.append("host must approve")
    bits.append("bodies sealed" if policy.get("sealed") else "bodies in clear")
    ttl = policy.get("enroll_ttl_s") or 0
    if ttl:
        bits.append("watchword expires in %s" % _duration(float(ttl)))
    uses = policy.get("enroll_max_uses") or 0
    if uses:
        bits.append("%d use(s) left" % uses)
    bits.append("max %d agents" % int(policy.get("max_agents", 16) or 16))
    facts.append(("Policy", t.dim((" %s " % dot).join(bits))))
    if host_token_hint:
        facts.append(("Host token", t.dim(host_token_hint)))
    lines += ["  " + line for line in t.kv(facts, gap=3)]
    lines.append("")
    return lines


def _duration(seconds: float) -> str:
    seconds = float(seconds)
    if seconds < 90:
        return "%.0f s" % seconds
    if seconds < 5400:
        return "%.0f min" % (seconds / 60.0)
    if seconds < 172800:
        return "%.1f h" % (seconds / 3600.0)
    return "%.1f days" % (seconds / 86400.0)


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


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


def _existing_parley(server, workspace: Path) -> Optional[dict]:
    """Facts about the parley already hosted from ``workspace``, or ``None``.

    Best effort on purpose: this exists to make a refusal message concrete, and a
    state directory too damaged to describe is still a state directory `init`
    must not silently overwrite.
    """
    state_dir = server.find_hub_state_dir(workspace)
    if state_dir is None:
        return None
    facts = {"state_dir": str(state_dir), "session": "", "fingerprint": "", "name": "",
             "agents": None, "head_seq": None}
    try:
        config = _mod("parley.config").HubConfig.load(state_dir)
        facts.update({"session": config.session, "fingerprint": config.fingerprint,
                      "name": config.name})
    except Exception:
        return facts
    try:
        store = _mod("parley.hub.store").Store(state_dir)
        try:
            facts["agents"] = len(store.list_agents())
            facts["head_seq"] = store.head_seq()
        finally:
            store.close()
    except Exception:
        pass
    return facts


def _refuse_to_clobber(existing: dict) -> CliError:
    """`init` over a live state directory.  The one error that must not be terse."""
    agents = existing.get("agents")
    population = ("" if agents is None
                  else " %d agent(s) are enrolled in it and would all be locked out." % agents)
    return CliError(
        "a parley already exists in %s" % existing["state_dir"],
        code="hub_state_exists",
        exit_code=EXIT_ERROR,
        loud=True,
        hint="Run `parley resume` instead: it restarts this same parley -- same session, same "
             "fingerprint (%s), same log, same enrolled agents -- which is what a service "
             "supervisor should invoke. `parley init --force` starts a GENUINELY NEW parley "
             "over the top: new session id, new watchword, new fingerprint, and the existing "
             "log, blobs and agent keys are deleted.%s"
             % (existing.get("fingerprint") or "unknown", population),
        detail=existing,
    )


def cmd_init(ctx: Ctx, args: argparse.Namespace) -> Tuple[int, dict]:
    server = _mod("parley.hub.server")
    workspace = _workspace(args)
    workspace.mkdir(parents=True, exist_ok=True)

    existing = _existing_parley(server, workspace)
    if existing and not args.force:
        raise _refuse_to_clobber(existing)
    if existing and args.force:
        ctx.warn("--force: discarding the parley in %s. Every agent enrolled in session %s "
                 "is locked out from now on." % (existing["state_dir"], existing["session"] or "?"))

    name = args.name or _default_parley_name(workspace)
    try:
        hub, watchword = server.create_parley(
            workspace,
            name=name,
            port=int(args.port),
            bind=args.bind,
            public=bool(args.public),
            sealed=bool(args.seal),
            require_approval=bool(args.approve),
            words=int(args.words),
            force=bool(args.force),
        )
    except OSError as exc:
        raise CliError(
            "cannot bind %s:%s (%s)" % (args.bind, args.port, exc),
            code="bind_failed",
            hint="Another process is probably already on that port -- a Hub you forgot to "
                 "stop? Pick another with --port, or stop the other one.",
        )
    except Exception as exc:
        raise _translate(exc)

    try:
        hub.start()
    except Exception as exc:
        # A parley that never managed to listen has no log, no agents and nothing
        # worth keeping -- and leaving its state directory behind would make the
        # obvious retry fail with "a parley already exists here".
        try:
            hub.stop()
        except Exception:
            pass
        server.discard_hub_state(hub.state_dir)
        raise _translate(exc)

    config = getattr(hub, "config", None)
    policy = dict(getattr(config, "policy", None) or {})
    policy.setdefault("sealed", bool(args.seal))
    policy.setdefault("require_approval", bool(args.approve))
    policy.setdefault("enroll_open", True)
    fingerprint = getattr(config, "fingerprint", "") or ""
    session = getattr(config, "session", "") or ""
    host_token = getattr(config, "host_token", "") or ""

    raw_url = ""
    try:
        raw_url = hub.url
    except Exception:
        raw_url = "http://%s:%d" % (args.bind, int(args.port))
    hub_url = public_url(raw_url)
    try:
        deck_url = public_url(hub.deck_url(with_viewer_token=True))
    except Exception:
        deck_url = hub_url + "/"

    if not fingerprint:
        try:
            crypto = _mod("parley.crypto")
            fingerprint = crypto.fingerprint(
                crypto.derive_root_key(watchword, session)
            ) if session else ""
        except Exception:
            fingerprint = ""

    # -- exposure assessment ------------------------------------------------ #
    warnings: List[str] = []
    outbound = lan_ip()
    wide = args.bind in ("0.0.0.0", "::", "")
    scheme = urllib.parse.urlsplit(hub_url).scheme
    if (wide or args.public) and not is_private_ip(outbound) and scheme != "https" and not args.seal:
        warnings.append(
            "This Hub is bound to %s:%s and this machine's address %s is on the public "
            "internet. There is no TLS and sealed mode is off, so every message, file and "
            "status report crosses the network in clear text, and /v1/enroll is open to "
            "anyone who finds the port. Stop now and restart with --seal, or put it behind "
            "a TLS tunnel (scripts/tunnel.sh, docs/DEPLOY.md), or bind to a private "
            "interface with --bind 127.0.0.1."
            % (args.bind, args.port, outbound)
        )
    elif args.public and not args.seal and scheme != "https":
        warnings.append(
            "--public was given without --seal and without TLS. Enrolment is reachable "
            "from outside this network and message bodies are in clear text. Use --seal "
            "or a TLS tunnel before you discuss anything you would not post publicly."
        )

    # -- enrol the host so `parley say` works from here immediately ---------- #
    enrolled: Optional[dict] = None
    enrol_problem = ""
    if existing and args.force:
        # The credentials in this folder belong to the parley just discarded, so
        # they authenticate against nothing.  Leaving them would make the host's
        # own terminal the first victim of its own --force.
        _drop_dead_credentials(workspace, existing.get("session", ""))
    if not args.no_join:
        if (workspace / ".parley" / "credentials.json").exists():
            enrol_problem = "kept the credentials already in %s/.parley" % workspace
        else:
            try:
                client_mod = _mod("parley.client.client")
                client = client_mod.ParleyClient.enroll(
                    hub_url,
                    watchword,
                    workspace,
                    name=args.me or _default_agent_name(),
                    kind=args.kind or _default_kind(),
                    model=os.environ.get("PARLEY_MODEL", ""),
                    capabilities=["chat", "sync", "tasks", "psr"],
                    sealed=bool(args.seal),
                    expect_fingerprint=fingerprint,
                )
                creds = getattr(client, "creds", None)
                enrolled = {
                    "agent_id": getattr(creds, "agent_id", "") or getattr(client, "agent_id", ""),
                    "name": getattr(creds, "name", "") or (args.me or _default_agent_name()),
                }
            except Exception as exc:
                enrol_problem = "could not enrol this terminal as a participant (%s)" % _translate(exc).message

    payload = {
        "session": session,
        "name": name,
        "watchword": watchword,
        "fingerprint": fingerprint,
        "hub_url": hub_url,
        "bind": args.bind,
        "port": int(args.port),
        "deck_url": deck_url,
        "workspace": str(workspace),
        "join_command": 'parley join --hub %s --invite "%s"' % (hub_url, watchword),
        "discover_command": 'parley join --discover --invite "%s"' % watchword,
        "host_token": host_token,
        "policy": policy,
        "warnings": warnings,
        "enrolled": enrolled,
        "serving": True,
    }

    if ctx.human:
        ctx.out.blank()
        ctx.out.write(render_invite(
            ctx.out,
            name=name,
            session=session,
            watchword=watchword,
            fingerprint=fingerprint,
            hub_url=hub_url,
            deck_url=deck_url,
            workspace=str(workspace),
            bind=args.bind,
            port=int(args.port),
            policy=policy,
            warnings=warnings,
            phonetic=bool(args.phonetic),
            host_token_hint=_host_token_hint(workspace, host_token),
            heading="the Hub is running",
        ))
        if enrolled:
            ctx.out.write(ctx.out.dim("  This terminal joined as %s (%s). `parley say \"...\"` works now."
                                      % (enrolled.get("name"), enrolled.get("agent_id"))))
        elif enrol_problem:
            ctx.out.write(ctx.out.dim("  This terminal did not join: %s" % enrol_problem))
        ctx.out.write(ctx.out.dim("  Ctrl-C stops the Hub and ends the parley for everyone."))
        ctx.out.write(ctx.out.dim("  While it runs:  parley watch   %s   parley roster   %s   parley doctor"
                                  % (ctx.out.g["dot"], ctx.out.g["dot"])))
        ctx.out.blank()
        ctx.out.flush()
    else:
        _emit_json({"ok": True, "command": "init", "exit_code": EXIT_OK, "data": payload})
        ctx.streaming = True  # already emitted; do not emit twice

    _serve_until_interrupted(ctx, hub)
    return EXIT_OK, payload


def _serve_until_interrupted(ctx: Ctx, hub) -> None:
    """Block until Ctrl-C or SIGTERM, then stop the Hub cleanly.

    The Hub lives inside this process, so stopping this process ends the parley.
    SIGTERM is routed through the same path as Ctrl-C so a process manager gets
    the same clean shutdown a human does.
    """
    import signal

    def _term(_signum, _frame):
        raise KeyboardInterrupt

    for signame in ("SIGTERM", "SIGHUP"):
        handler = getattr(signal, signame, None)
        if handler is not None:
            try:
                signal.signal(handler, _term)
            except (ValueError, OSError):  # not the main thread, or not supported
                pass
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        ctx.note("\nstopping the Hub...")
    finally:
        try:
            hub.stop()
        except Exception:
            pass


def _drop_dead_credentials(workspace: Path, dead_session: str) -> None:
    """Delete this folder's credentials if they belong to ``dead_session``.

    Scoped to that one session on purpose: credentials for some *other* parley
    this folder also takes part in are none of ``init --force``'s business.
    """
    path = workspace / ".parley" / "credentials.json"
    if not dead_session or not path.exists():
        return
    try:
        stored = json.loads(path.read_text("utf-8")).get("session", "")
    except Exception:
        return
    if stored == dead_session:
        try:
            path.unlink()
        except OSError:
            pass


def _host_token_hint(workspace: Path, host_token: str) -> str:
    state = _hub_state_dir(workspace) or (workspace / ".parley" / "hub")
    return "stored in %s/hub.json -- needed for `parley approve`, `parley invite --rotate`" % state


def _default_parley_name(workspace: Path) -> str:
    base = workspace.resolve().name or "parley"
    return base


def _default_agent_name() -> str:
    for env in ("PARLEY_NAME", "PARLEY_AGENT_NAME"):
        if os.environ.get(env):
            return os.environ[env]
    try:
        user = getpass.getuser()
    except Exception:
        user = "agent"
    host = socket.gethostname().split(".")[0]
    return "%s@%s" % (user, host)


# --------------------------------------------------------------------------- #
# resume
# --------------------------------------------------------------------------- #


def render_resume(
    t: Term,
    *,
    name: str,
    session: str,
    fingerprint: str,
    hub_url: str,
    deck_url: str,
    workspace: str,
    bind: str,
    port: int,
    agents: int,
    head_seq: int,
) -> List[str]:
    """The `resume` banner: the same parley, carrying on.

    Deliberately *not* :func:`render_invite`. That screen is built around a
    watchword a human reads out loud, and resume does not mint one -- the Hub
    stores only the derived root key and a hash of the words (SPEC 11), so the
    old one is unrecoverable by design. Printing an invite screen with an empty
    frame where the watchword goes would be worse than printing no frame at all.
    """
    width = t.layout_width(80)
    dot = t.g["dot"]
    lines: List[str] = []
    lines += t.box(
        [
            t.paint(" ".join("PARLEY"), "bold", "cyan") + "   " + t.bold(name or "untitled parley"),
            t.dim("%s  %s  resumed -- same session, same agents" % (session, dot)),
        ],
        width=width, style="cyan",
    )
    lines.append("")
    facts: List[Tuple[str, str]] = [
        ("Hub", hub_url + t.dim("   bound %s:%d" % (bind, port))),
    ]
    if deck_url:
        facts.append(("Deck", t.paint(deck_url, "underline") + t.dim("   (open in a browser)")))
    facts.append(("Fingerprint", t.paint(fingerprint or "(none)", "bold", "bcyan")))
    facts.append(("Agents", "%d enrolled" % agents))
    facts.append(("Log", "%d event(s)" % head_seq))
    facts.append(("Workspace", workspace))
    lines += ["  " + line for line in t.kv(facts, gap=3)]
    lines.append("")
    lines.append(t.dim("  Everyone already enrolled stays enrolled: their agent keys and the"))
    lines.append(t.dim("  fingerprint are unchanged, so nobody has to re-join."))
    lines.append(t.dim("  No watchword is printed -- resume does not mint one, and the old one is"))
    lines.append(t.dim("  not stored in recoverable form. `parley invite --rotate` issues a new"))
    lines.append(t.dim("  one without disconnecting anybody."))
    lines.append("")
    return lines


def cmd_resume(ctx: Ctx, args: argparse.Namespace) -> Tuple[int, dict]:
    server = _mod("parley.hub.server")
    workspace = _workspace(args)

    try:
        hub = server.resume_parley(
            workspace,
            port=None if args.port is None else int(args.port),
            bind=args.bind,
        )
    except OSError as exc:
        raise CliError(
            "cannot read the Hub state in %s (%s)" % (workspace, exc),
            code="bad_hub_state",
            hint="`parley resume` needs the state directory written by `parley init` -- "
                 "check the path and that this user can read it.",
        )
    except Exception as exc:
        raise _translate(exc)

    try:
        hub.start()
    except Exception as exc:
        err = _translate(exc)
        if err.message.startswith("cannot bind"):
            # The generic hint says "pick another port with init", which is the one
            # thing someone resuming must not do.
            err.code = "bind_failed"
            err.hint = ("Another process is probably already on that port -- quite possibly "
                        "the very Hub you are trying to resume. Stop it first, or resume on "
                        "another port with --port.")
        raise err

    config = hub.config
    policy = dict(getattr(config, "policy", None) or {})
    try:
        hub_url = public_url(hub.url)
    except Exception:
        hub_url = "http://%s:%d" % (config.bind, hub.port)
    try:
        deck_url = public_url(hub.deck_url(with_viewer_token=True))
    except Exception:
        deck_url = hub_url + "/"
    agents = len(hub.store.list_agents())

    payload = {
        "session": config.session,
        "name": config.name,
        "fingerprint": config.fingerprint,
        "hub_url": hub_url,
        "deck_url": deck_url,
        "bind": config.bind,
        "port": hub.port,
        "workspace": str(workspace),
        "state_dir": str(hub.state_dir),
        "agents": agents,
        "head_seq": hub.store.head_seq(),
        "policy": policy,
        "resumed": True,
        "serving": True,
    }

    if ctx.human:
        ctx.out.blank()
        ctx.out.write(render_resume(
            ctx.out,
            name=config.name,
            session=config.session,
            fingerprint=config.fingerprint,
            hub_url=hub_url,
            deck_url=deck_url,
            workspace=str(workspace),
            bind=config.bind,
            port=hub.port,
            agents=agents,
            head_seq=payload["head_seq"],
        ))
        ctx.out.write(ctx.out.dim("  Ctrl-C stops the Hub. Resume it again with `parley resume`."))
        ctx.out.blank()
        ctx.out.flush()
    else:
        _emit_json({"ok": True, "command": "resume", "exit_code": EXIT_OK, "data": payload})
        ctx.streaming = True  # already emitted; do not emit twice

    _serve_until_interrupted(ctx, hub)
    return EXIT_OK, payload


def _default_kind() -> str:
    if os.environ.get("PARLEY_KIND"):
        return os.environ["PARLEY_KIND"]
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE"):
        return "claude-code"
    if os.environ.get("CURSOR_TRACE_ID") or os.environ.get("CURSOR_SESSION_ID"):
        return "cursor"
    if os.environ.get("TERM_PROGRAM") == "vscode":
        return "vscode"
    if os.environ.get("CI"):
        return "ci"
    return "cli"


# --------------------------------------------------------------------------- #
# join
# --------------------------------------------------------------------------- #


def cmd_join(ctx: Ctx, args: argparse.Namespace) -> Tuple[int, dict]:
    crypto = _mod("parley.crypto")
    config = _mod("parley.config")
    workspace = _workspace(args)
    workspace.mkdir(parents=True, exist_ok=True)

    # -- the watchword, forgiven ------------------------------------------- #
    raw_invite = args.invite or os.environ.get("PARLEY_INVITE") or ""
    if raw_invite == "-":
        raw_invite = sys.stdin.readline()
    if not raw_invite:
        if ctx.human and sys.stdin.isatty():
            try:
                raw_invite = input("watchword (as you heard it): ")
            except (EOFError, KeyboardInterrupt):
                raw_invite = ""
        if not raw_invite:
            raise CliError(
                "no watchword given",
                code="usage",
                exit_code=EXIT_USAGE,
                hint='Pass --invite "copper otter climbs the quiet hill" (spacing, case and '
                     'punctuation do not matter), or set PARLEY_INVITE.',
            )
    try:
        watchword = crypto.normalise_watchword(raw_invite)
    except Exception as exc:
        raise CliError("that watchword could not be read (%s)" % exc, code="bad_watchword", exit_code=EXIT_USAGE)
    if not watchword:
        raise CliError(
            "the watchword came out empty after normalisation",
            code="bad_watchword",
            exit_code=EXIT_USAGE,
            hint="It should be five plain words, e.g. copper-otter-climbs-the-quiet-hill. "
                 "Dashes, spaces, capitals and a trailing full stop are all fine.",
        )
    word_count = len([w for w in watchword.split("-") if w])
    if word_count < 3:
        ctx.warn("that watchword is only %d word(s) long; a Parley invite is normally six "
                 "(five random words plus 'the'). Did part of it get cut off?" % word_count)

    # -- find the Hub ------------------------------------------------------- #
    discovered: List[dict] = []
    hub_url = args.hub or os.environ.get("PARLEY_HUB") or ""
    if args.discover or not hub_url:
        if not args.discover and not hub_url:
            raise CliError(
                "no Hub given",
                code="usage",
                exit_code=EXIT_USAGE,
                hint="Pass --hub http://host:7777, or --discover to find one on this LAN.",
            )
        ctx.note("looking for a Hub on this network...")
        discovered = discover_hubs(timeout=min(6.0, ctx.timeout))
        if not discovered:
            raise CliError(
                "no Hub answered the discovery probe on this network",
                code="not_found",
                exit_code=EXIT_NO_HUB,
                hint="Discovery is a UDP broadcast to port %d; it does not cross subnets, VPNs "
                     "or most Wi-Fi guest networks, and it is disabled on a --public Hub. Ask "
                     "the host for the URL and use --hub instead." % DISCOVERY_PORT,
            )
        if len(discovered) > 1 and not hub_url:
            if ctx.human and sys.stdin.isatty():
                ctx.out.write(ctx.out.bold("  More than one parley is running here:"))
                for i, info in enumerate(discovered, 1):
                    ctx.out.write("   %d) %s  %s  %s" % (
                        i, info.get("name", "?"), ctx.out.dim(info.get("url", "")),
                        ctx.out.dim(info.get("fingerprint", "")),
                    ))
                try:
                    choice = int(input("  which one? [1] ") or "1")
                except (ValueError, EOFError, KeyboardInterrupt):
                    choice = 1
                hub_url = discovered[max(0, min(len(discovered) - 1, choice - 1))].get("url", "")
            else:
                raise CliError(
                    "%d Hubs answered discovery; pick one with --hub" % len(discovered),
                    code="ambiguous",
                    exit_code=EXIT_USAGE,
                    detail={"candidates": discovered},
                )
        else:
            hub_url = hub_url or discovered[0].get("url", "")
            ctx.note("found %r at %s" % (discovered[0].get("name", "?"), hub_url))

    try:
        hub_url = config.resolve_hub_url(hub_url)
    except Exception:
        if "://" not in hub_url:
            hub_url = "http://" + hub_url
        hub_url = hub_url.rstrip("/")

    # -- pre-flight: is it a Parley Hub, and is it the right one? ----------- #
    hello = hub_hello(hub_url, timeout=ctx.timeout)
    wire = hello.get("v") or hello.get("version") or ""
    if wire and wire != "PARLEY/1":
        raise CliError(
            "that Hub speaks %r, this client speaks PARLEY/1" % wire,
            code="version_mismatch",
            hint="Upgrade whichever side is older. The wire version is not negotiated.",
        )
    their_fp = hello.get("fingerprint", "")
    expect = args.expect_fingerprint or ""
    if expect and their_fp and crypto.normalise_watchword(expect) != crypto.normalise_watchword(their_fp):
        raise CliError(
            "FINGERPRINT MISMATCH: you expected %r, the Hub at %s says %r" % (expect, hub_url, their_fp),
            code="fingerprint_mismatch",
            exit_code=EXIT_FINGERPRINT,
            hint="Do not continue. Either you have the wrong address, or something is "
                 "relaying you to a different Hub. Confirm the three words with the host "
                 "by voice before trying again.",
            loud=True,
            detail={"expected": expect, "actual": their_fp, "hub_url": hub_url},
        )

    # A previously-known session whose fingerprint changed is a hard error (SPEC 3.5).
    existing_path = workspace / ".parley" / "credentials.json"
    if existing_path.exists():
        try:
            old = config.Credentials.load(workspace)
            if (getattr(old, "session", "") == hello.get("session")
                    and getattr(old, "fingerprint", "") and their_fp
                    and old.fingerprint != their_fp):
                raise CliError(
                    "FINGERPRINT CHANGED for session %s: it was %r, the Hub now says %r"
                    % (old.session, old.fingerprint, their_fp),
                    code="fingerprint_changed",
                    exit_code=EXIT_FINGERPRINT,
                    hint="Parley will not auto-accept this. Either the host rebuilt the "
                         "parley from scratch -- in which case delete "
                         ".parley/credentials.json and join again -- or you are being "
                         "pointed at an impostor Hub. Check by voice first.",
                    loud=True,
                )
            if getattr(old, "session", "") == hello.get("session") and not args.force:
                raise CliError(
                    "this workspace is already enrolled in that parley as %s"
                    % getattr(old, "agent_id", "?"),
                    code="already_joined",
                    exit_code=EXIT_ERROR,
                    hint="Nothing to do -- run `parley run` to start participating. Pass "
                         "--force to discard the existing credentials and enrol again as a "
                         "second agent.",
                )
        except CliError:
            raise
        except Exception:
            pass

    # -- enrol -------------------------------------------------------------- #
    client_mod = _mod("parley.client.client")
    name = args.name or _default_agent_name()
    kind = args.kind or _default_kind()
    try:
        client = client_mod.ParleyClient.enroll(
            hub_url,
            watchword,
            workspace,
            name=name,
            kind=kind,
            model=args.model or os.environ.get("PARLEY_MODEL", ""),
            capabilities=["chat", "sync", "tasks", "psr"],
            sealed=bool(args.seal) or bool(hello.get("requires_seal")),
            expect_fingerprint=their_fp,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        raise _join_failure(exc, hub_url, watchword, hello)

    creds = getattr(client, "creds", None)
    agent_id = getattr(creds, "agent_id", "") or getattr(client, "agent_id", "")
    fingerprint = getattr(creds, "fingerprint", "") or their_fp
    status = "active"
    policy = dict(getattr(creds, "policy", None) or hello.get("policy") or {})
    if policy.get("require_approval"):
        status = "pending"

    payload = {
        "session": hello.get("session") or getattr(creds, "session", ""),
        "parley_name": hello.get("name", ""),
        "agent_id": agent_id,
        "name": name,
        "kind": kind,
        "hub_url": hub_url,
        "fingerprint": fingerprint,
        "status": status,
        "workspace": str(workspace),
        "sealed": bool(getattr(creds, "sealed", False)),
        "policy": policy,
        "discovered": bool(discovered),
    }

    if ctx.human:
        t = ctx.out
        width = t.layout_width(76)
        t.blank()
        t.write(t.box(
            [
                t.paint("joined ", "green", "bold") + t.bold(hello.get("name") or payload["session"]),
                t.dim("as %s  %s  %s" % (name, t.g["dot"], agent_id)),
            ],
            width=width, style="green",
        ))
        t.blank()
        t.write("  " + t.bold("Check the fingerprint out loud with the host:"))
        t.blank()
        t.write("       " + t.paint(fingerprint or "(none)", "bold", "bcyan"))
        t.blank()
        t.write(t.dim("  If the host's terminal shows different words, stop: you are not in the"))
        t.write(t.dim("  same parley. Same words means you are."))
        t.blank()
        if status == "pending":
            t.write("  " + t.warn("Waiting for the host to approve you.") +
                    t.dim(" Ask them to run:  parley approve %s" % agent_id))
            t.blank()
        t.write(t.rule("", width))
        t.write(t.kv([
            ("Hub", hub_url),
            ("Workspace", str(workspace)),
            ("Next", t.bold("parley run") + t.dim("   -- sync files, keep your standing report fresh")),
            ("Then", t.dim('parley status "what you are doing"  %s  parley say "hello"  %s  parley watch'
                           % (t.g["dot"], t.g["dot"]))),
        ], gap=3))
        t.blank()
    return EXIT_OK, payload


def _join_failure(exc: BaseException, hub_url: str, watchword: str, hello: dict) -> CliError:
    """Turn an enrolment failure into advice, not a traceback.

    The important distinction here: the client raises ``FingerprintMismatch`` both
    when the *watchword* derives the wrong fingerprint (a typo -- annoying, exit 3)
    and when the Hub is not the one you expected (alarming, exit 5).  The detail
    keys tell the two apart, and conflating them would either cry wolf or -- much
    worse -- let a real mismatch read as a typo.
    """
    err = _translate(exc)
    if err.code == "fingerprint_mismatch" and "derived_fingerprint" in (err.detail or {}):
        return CliError(
            "that watchword does not open this parley",
            code="bad_watchword",
            exit_code=EXIT_AUTH,
            hint="Parley read it as %r and derived the fingerprint %r, but the Hub at %s "
                 "is %r -- so the words really are different, not just differently typed. "
                 "Ask the host to read them again, slowly. (Case, spaces, dashes and a "
                 "trailing full stop are all normalised away before this check.)"
                 % (watchword, err.detail.get("derived_fingerprint", "?"), hub_url,
                    err.detail.get("hub_fingerprint", "?")),
            detail=dict(err.detail or {}, normalised=watchword, hub_url=hub_url),
        )
    if err.code in ("bad_signature", "unknown_agent"):
        return CliError(
            "the Hub rejected that watchword",
            code="bad_watchword",
            exit_code=EXIT_AUTH,
            hint="Parley read it as %r. Case, spaces, dashes and punctuation are all "
                 "normalised away, so what is left really is a different set of words -- "
                 "ask the host to read them again slowly. (If the words are definitely "
                 "right, run `parley doctor`: a clock more than 300 s off the Hub's fails "
                 "authentication in exactly this way.)" % watchword,
            detail={"normalised": watchword, "hub_url": hub_url},
        )
    if err.code == "enroll_closed":
        return CliError(
            "the Hub refused: enrolment is closed",
            code="enroll_closed",
            exit_code=EXIT_AUTH,
            hint="The watchword expired, ran out of uses, or the host closed the door. "
                 "Ask them for a fresh one: `parley invite --rotate`.",
        )
    if err.code == "pending_approval":
        return CliError(
            "the Hub accepted you but the host must approve you before you can write",
            code="pending_approval",
            exit_code=EXIT_AUTH,
            hint="Ask the host to run `parley approve <your agent id>`.",
        )
    if err.code in ("fingerprint_mismatch", "fingerprint_changed"):
        err.exit_code = EXIT_FINGERPRINT
        err.loud = True
        err.hint = err.hint or (
            "Do not continue. The Hub answering is not the one whose watchword you were "
            "given. Confirm the three words with the host by voice."
        )
        return err
    if err.exit_code == EXIT_NO_HUB:
        err.message = "cannot reach a Hub at %s" % hub_url
        err.hint = ("The address answered /v1/hello a moment ago but not the enrolment "
                    "request -- did the Hub just stop? Try again, then `parley doctor`.")
        return err
    if err.code == "rate_limited":
        err.hint = "Too many enrolment attempts from this address. Wait a minute and retry."
    return err


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def cmd_run(ctx: Ctx, args: argparse.Namespace) -> Tuple[int, dict]:
    workspace = _workspace(args)
    _load_credentials(workspace)  # fail fast and clearly if we are not enrolled
    runtime_mod = _mod("parley.client.runtime")

    kwargs: Dict[str, Any] = {"sync": not args.no_sync, "pigeonhole": True}
    if args.psr_from:
        kwargs["psr_from"] = args.psr_from
    kwargs = _filter_kwargs(runtime_mod.Runtime.__init__, kwargs, ctx)

    try:
        runtime = runtime_mod.Runtime(workspace, **kwargs)
    except Exception as exc:
        raise _translate(exc)

    ctx.note("parley run: streaming events, syncing %s, keeping your standing report fresh. Ctrl-C to stop."
             % ("off" if args.no_sync else str(workspace)))
    if ctx.json:
        _emit_json({"ok": True, "command": "run", "exit_code": EXIT_OK,
                    "data": {"workspace": str(workspace), "sync": not args.no_sync, "state": "running"}})
        ctx.streaming = True
    code = EXIT_OK
    try:
        code = int(runtime.run() or 0)
    except KeyboardInterrupt:
        try:
            runtime.stop()
        except Exception:
            pass
        ctx.note("stopped.")
        code = EXIT_OK
    except Exception as exc:
        raise _translate(exc)
    return code, {"workspace": str(workspace), "exit_code": code}


def _filter_kwargs(func, kwargs: Dict[str, Any], ctx: Ctx) -> Dict[str, Any]:
    """Pass only the keyword arguments *func* actually accepts.

    The layers of Parley are built independently; this keeps a CLI flag from
    becoming a TypeError when an optional parameter has not landed yet.
    """
    import inspect

    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return kwargs
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    kept, dropped = {}, []
    for key, value in kwargs.items():
        if key in params:
            kept[key] = value
        else:
            dropped.append(key)
    if dropped:
        ctx.warn("this build of parley ignores: %s" % ", ".join(sorted(dropped)))
    return kept


# --------------------------------------------------------------------------- #
# say / status / know -- the hot path for an agent
# --------------------------------------------------------------------------- #


def _ref(value: str) -> dict:
    value = value.strip()
    if value.startswith("evt_"):
        return {"kind": "event", "value": value}
    if value.startswith("tsk_"):
        return {"kind": "task", "value": value}
    return {"kind": "file", "value": value}


def _event_payload(event: dict) -> dict:
    return {
        "event_id": event.get("id", ""),
        "seq": event.get("seq"),
        "ts": event.get("ts", ""),
        "type": event.get("type", ""),
        "actor": event.get("actor", ""),
    }


def cmd_say(ctx: Ctx, args: argparse.Namespace) -> Tuple[int, dict]:
    workspace = _workspace(args)
    text = args.message
    if text == "-":
        text = sys.stdin.read()
    text = text.strip()
    if not text:
        raise CliError("nothing to say", code="usage", exit_code=EXIT_USAGE,
                       hint='Pass the message as the first argument, or "-" to read stdin.')
    if len(text.encode("utf-8")) > 16 * 1024:
        raise CliError(
            "that message is %d bytes; the limit is 16 KiB (SPEC 4.3)" % len(text.encode("utf-8")),
            code="too_large", exit_code=EXIT_USAGE,
            hint="Put the long part in a file in the workspace -- it syncs to everyone -- "
                 "and say the summary with --ref <path>.",
        )
    client, _ = _client(workspace)
    to = None
    if args.to:
        to = [p.strip() for p in ",".join(args.to).split(",") if p.strip()]
    refs = [_ref(r) for r in (args.ref or [])]
    try:
        event = client.say(text, to=to, reply_to=args.reply or None, refs=refs or None)
    except Exception as exc:
        raise _translate(exc)
    payload = _event_payload(event)
    payload["text"] = text
    if ctx.human:
        ctx.out.write("%s %s" % (ctx.out.ok(ctx.out.g["pass"]),
                                 ctx.out.dim("said (seq %s)" % payload.get("seq"))))
    return EXIT_OK, payload


def cmd_status(ctx: Ctx, args: argparse.Namespace) -> Tuple[int, dict]:
    workspace = _workspace(args)
    client, creds = _client(workspace)

    if not args.headline:
        # No headline given: report rather than set. Agents poll this.
        try:
            state = client.state()
        except Exception as exc:
            raise _translate(exc)
        me = getattr(creds, "agent_id", "")
        mine = None
        for agent in state.get("agents", []) or []:
            if agent.get("agent_id") == me:
                mine = agent
                break
        payload = {
            "session": state.get("session", ""),
            "name": state.get("name", ""),
            "fingerprint": state.get("fingerprint", ""),
            "head_seq": state.get("head_seq", 0),
            "agents_online": sum(1 for a in state.get("agents", []) or [] if a.get("online")),
            "agents_total": len(state.get("agents", []) or []),
            "open_tasks": sum(1 for t in state.get("tasks", []) or [] if t.get("status") != "done"),
            "me": mine,
        }
        if ctx.human:
            t = ctx.out
            t.write(t.kv([
                ("parley", t.bold(str(payload["name"] or payload["session"]))),
                ("fingerprint", str(payload["fingerprint"])),
                ("agents", "%d online of %d" % (payload["agents_online"], payload["agents_total"])),
                ("head seq", str(payload["head_seq"])),
                ("open tasks", str(payload["open_tasks"])),
            ], gap=3))
            psr = (mine or {}).get("psr") or {}
            if psr:
                t.write("")
                t.write("  " + t.bold("you") + ": " + _psr_line(t, psr))
            else:
                t.write("")
                t.write("  " + t.warn("you have no standing report") +
                        t.dim('  -- set one with: parley status "what you are doing"'))
        return EXIT_OK, payload

    headline = args.headline.strip()
    if len(headline) > 80:
        ctx.warn("headline is %d chars; the Deck shows 80 (SPEC 6.1). It will be cut off." % len(headline))
    if args.state not in PSR_STATES:
        raise CliError(
            "unknown state %r" % args.state, code="usage", exit_code=EXIT_USAGE,
            hint="One of: %s" % ", ".join(PSR_STATES),
        )
    kwargs: Dict[str, Any] = {
        "state": args.state,
        "focus": list(args.focus or []) or None,
        "detail": args.detail or "",
    }
    if args.progress is not None:
        if not 0.0 <= args.progress <= 1.0:
            raise CliError("--progress must be between 0.0 and 1.0", code="usage", exit_code=EXIT_USAGE)
        kwargs["progress"] = args.progress
    if args.task:
        kwargs["task"] = args.task
    if args.needs:
        kwargs["needs"] = list(args.needs)
    if args.eta is not None:
        kwargs["eta_s"] = int(args.eta)
    if args.blocked_on:
        kwargs["blocked_on"] = {"agent": args.blocked_on, "reason": args.detail or "unspecified"}
    try:
        event = client.status(headline, **kwargs)
    except Exception as exc:
        raise _translate(exc)
    payload = _event_payload(event)
    payload["headline"] = headline
    payload["state"] = args.state
    if ctx.human:
        ctx.out.write("%s %s" % (ctx.out.ok(ctx.out.g["pass"]),
                                 ctx.out.dim("standing report updated (seq %s)" % payload.get("seq"))))
    return EXIT_OK, payload


def cmd_know(ctx: Ctx, args: argparse.Namespace) -> Tuple[int, dict]:
    workspace = _workspace(args)
    if args.kind not in KNOWLEDGE_KINDS:
        raise CliError(
            "unknown contribution kind %r" % args.kind, code="usage", exit_code=EXIT_USAGE,
            hint="One of: %s. The kind sets the Ledger weight (SPEC 9)." % ", ".join(KNOWLEDGE_KINDS),
        )
    client, _ = _client(workspace)
    refs = [_ref(r) for r in (args.ref or [])]
    try:
        event = client.know(args.title, args.kind, detail=args.detail or "", refs=refs or None)
    except Exception as exc:
        raise _translate(exc)
    payload = _event_payload(event)
    payload["title"] = args.title
    payload["kind"] = args.kind
    if ctx.human:
        ctx.out.write("%s %s" % (
            ctx.out.ok(ctx.out.g["pass"]),
            ctx.out.dim("recorded a %s contribution (seq %s) -- it is now in the Ledger"
                        % (args.kind, payload.get("seq"))),
        ))
    return EXIT_OK, payload


# --------------------------------------------------------------------------- #
# task
# --------------------------------------------------------------------------- #


def cmd_task(ctx: Ctx, args: argparse.Namespace) -> Tuple[int, dict]:
    workspace = _workspace(args)
    action = args.task_action
    client, creds = _client(workspace)

    if action == "list":
        try:
            state = client.state()
        except Exception as exc:
            raise _translate(exc)
        tasks = list(state.get("tasks", []) or [])
        if args.status:
            tasks = [t for t in tasks if t.get("status") == args.status]
        if args.mine:
            tasks = [t for t in tasks if t.get("claimed_by") == getattr(creds, "agent_id", "")]
        payload = {"tasks": tasks, "count": len(tasks)}
        if ctx.human:
            if not tasks:
                ctx.out.write(ctx.out.dim("  no tasks"))
            else:
                names = _agent_names(state)
                rows = []
                for task in tasks:
                    rows.append([
                        task.get("id", ""),
                        (task.get("status", ""), _status_style(task.get("status", ""))),
                        task.get("title", ""),
                        names.get(task.get("claimed_by") or "", task.get("claimed_by") or ""),
                        _progress_bar(ctx.out, task.get("progress")),
                    ])
                ctx.out.write(ctx.out.table(
                    ["TASK", "STATUS", "TITLE", "CLAIMED BY", "PROGRESS"], rows,
                ))
        return EXIT_OK, payload

    etype, body = _task_event(args, client)
    try:
        event = client.emit(etype, body)
    except Exception as exc:
        raise _translate(exc)
    payload = _event_payload(event)
    payload.update({"task_id": body.get("id", ""), "action": action})
    if ctx.human:
        ctx.out.write("%s %s" % (
            ctx.out.ok(ctx.out.g["pass"]),
            ctx.out.dim("%s %s (seq %s)" % (action, body.get("id", ""), payload.get("seq"))),
        ))
    return EXIT_OK, payload


def _task_event(args: argparse.Namespace, client) -> Tuple[str, dict]:
    action = args.task_action
    if action == "create":
        ids = _mod("parley.ids")
        task_id = args.id or ids.new_task_id()
        body: Dict[str, Any] = {"id": task_id, "title": args.title}
        if args.detail:
            body["detail"] = args.detail
        if args.tag:
            body["tags"] = list(args.tag)
        if args.priority is not None:
            if not 1 <= args.priority <= 5:
                raise CliError("--priority must be 1..5", code="usage", exit_code=EXIT_USAGE)
            body["priority"] = args.priority
        if args.depends_on:
            body["depends_on"] = list(args.depends_on)
        return "task.create", body
    if action == "claim":
        return "task.claim", {"id": args.id}
    if action == "release":
        body = {"id": args.id}
        if args.reason:
            body["reason"] = args.reason
        return "task.release", body
    if action == "update":
        if args.status not in TASK_STATUSES:
            raise CliError("unknown status %r" % args.status, code="usage", exit_code=EXIT_USAGE,
                           hint="One of: %s" % ", ".join(TASK_STATUSES))
        body = {"id": args.id, "status": args.status}
        if args.progress is not None:
            body["progress"] = args.progress
        if args.note:
            body["note"] = args.note
        return "task.update", body
    if action == "done":
        body = {"id": args.id}
        if args.result:
            body["result"] = args.result
        if args.ref:
            body["refs"] = [_ref(r) for r in args.ref]
        return "task.done", body
    raise CliError("unknown task action %r" % action, code="usage", exit_code=EXIT_USAGE)


def _status_style(status: str) -> str:
    return {
        "done": "green", "doing": "cyan", "review": "magenta",
        "blocked": "red", "todo": "grey",
    }.get(status, "")


def _progress_bar(t: Term, value, width: int = 10) -> str:
    if value is None:
        return ""
    try:
        frac = max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return ""
    filled = int(round(frac * width))
    if t.unicode:
        bar = "█" * filled + "░" * (width - filled)
    else:
        bar = "#" * filled + "." * (width - filled)
    return "%s %3.0f%%" % (bar, frac * 100)


# --------------------------------------------------------------------------- #
# watch
# --------------------------------------------------------------------------- #


def cmd_watch(ctx: Ctx, args: argparse.Namespace) -> Tuple[int, dict]:
    workspace = _workspace(args)
    client, creds = _client(workspace)
    protocol = None
    try:
        protocol = _mod("parley.protocol")
    except CliError:
        protocol = None

    prefixes: List[str] = []
    for item in args.types or []:
        prefixes += [p.strip() for p in item.split(",") if p.strip()]

    names: Dict[str, str] = {}
    head = 0
    try:
        state = client.state()
        names = _agent_names(state)
        head = int(state.get("head_seq", 0) or 0)
    except Exception as exc:
        if args.since is None:
            raise _translate(exc)

    since = args.since if args.since is not None else max(0, head - max(0, args.tail))
    ctx.streaming = True
    ctx.note("watching from seq %d%s -- Ctrl-C to stop"
             % (since, (" for " + ",".join(prefixes)) if prefixes else ""))

    count = 0
    try:
        for event in client.stream(since=since):
            etype = str(event.get("type", ""))
            if prefixes and not any(etype.startswith(p) for p in prefixes):
                continue
            count += 1
            if ctx.json:
                sys.stdout.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                sys.stdout.flush()
            else:
                line = _watch_line(ctx.out, event, names, protocol)
                print(line, file=sys.stdout, flush=True)
            if etype == "agent.hello":
                actor = event.get("actor", "")
                nm = (event.get("body") or {}).get("name")
                if actor and nm:
                    names[actor] = nm
            if args.count and count >= args.count:
                break
    except KeyboardInterrupt:
        ctx.note("\nstopped after %d event(s)." % count)
    except Exception as exc:
        raise _translate(exc)
    return EXIT_OK, {"events": count, "since": since}


def _watch_line(t: Term, event: dict, names: Dict[str, str], protocol) -> str:
    seq = event.get("seq")
    ts = str(event.get("ts", ""))
    clock = ts[11:19] if len(ts) >= 19 else ts[:8]
    actor = str(event.get("actor", ""))
    label = names.get(actor, "hub" if actor == "hub" else (actor[:10] or "?"))
    etype = str(event.get("type", ""))
    summary = _body_summary(event, protocol)
    seq_txt = ("%6s" % (seq if seq is not None else "-"))
    name_txt = truncate(label, 12)
    name_txt += " " * max(0, 12 - visible_width(name_txt))
    type_txt = truncate(etype, 18)
    type_txt += " " * max(0, 18 - visible_width(type_txt))
    colour = "grey" if actor == "hub" else ""
    return "%s %s %s %s %s" % (
        t.dim(seq_txt),
        t.dim(clock),
        t.agent_colour(actor, name_txt) if actor != "hub" else t.dim(name_txt),
        t.paint(type_txt, colour) if colour else t.dim(type_txt),
        summary,
    )


def _body_summary(event: dict, protocol) -> str:
    """Just the body half of ``protocol.event_summary``.

    ``event_summary`` returns a complete line -- ``seq ts actor type detail`` --
    which is right for a plain log but would duplicate the aligned, agent-coloured
    columns `watch` draws itself.  Take the module's own body renderer when it is
    exposed, otherwise peel the four fixed leading fields off its output, and only
    then fall back to our own.
    """
    if protocol is not None:
        renderer = getattr(protocol, "_summarise_body", None)
        if callable(renderer):
            try:
                return str(renderer(str(event.get("type", "")), event.get("body") or {}))
            except Exception:
                pass
        try:
            whole = str(protocol.event_summary(event))
            parts = whole.split(None, 4)
            if len(parts) == 5:
                return parts[4]
            if whole:
                return whole
        except Exception:
            pass
    return _fallback_summary(event)


def _fallback_summary(event: dict) -> str:
    body = event.get("body") or {}
    etype = event.get("type", "")
    if etype == "chat.message":
        return str(body.get("text", ""))
    if etype == "status.update":
        return "%s: %s" % (body.get("state", "?"), body.get("headline", ""))
    if etype == "knowledge.contribution":
        return "[%s] %s" % (body.get("kind", "?"), body.get("title", ""))
    if etype.startswith("file."):
        return str(body.get("path", ""))
    if etype.startswith("task."):
        return "%s %s" % (body.get("id", ""), body.get("title", body.get("status", "")))
    if etype == "agent.hello":
        return "%s (%s)" % (body.get("name", "?"), body.get("kind", "?"))
    return json.dumps(body, ensure_ascii=False, default=str)[:160]


# --------------------------------------------------------------------------- #
# roster
# --------------------------------------------------------------------------- #


def _agent_names(state: dict) -> Dict[str, str]:
    return {a.get("agent_id", ""): a.get("name", "") for a in (state.get("agents") or [])}


def _psr_line(t: Term, psr: dict) -> str:
    state = str(psr.get("state", "unknown"))
    colour = {
        "working": "green", "planning": "cyan", "reviewing": "magenta",
        "blocked": "red", "waiting": "yellow", "idle": "grey", "offline": "grey",
    }.get(state, "")
    head = t.paint(state, colour) if colour else state
    out = "%s %s" % (head, psr.get("headline", ""))
    if psr.get("stale"):
        out += t.warn("  (stale)")
    return out


def cmd_roster(ctx: Ctx, args: argparse.Namespace) -> Tuple[int, dict]:
    workspace = _workspace(args)
    client, creds = _client(workspace)
    try:
        state = client.state()
    except Exception as exc:
        raise _translate(exc)
    agents = list(state.get("agents", []) or [])
    payload = {
        "session": state.get("session", ""),
        "name": state.get("name", ""),
        "fingerprint": state.get("fingerprint", ""),
        "head_seq": state.get("head_seq", 0),
        "agents": agents,
    }
    if ctx.human:
        t = ctx.out
        me = getattr(creds, "agent_id", "")
        rows = []
        for agent in agents:
            psr = agent.get("psr") or {}
            online = agent.get("online")
            status = agent.get("status", "active")
            if status == "revoked":
                dot, style = t.g["fail"], "red"
            elif status == "pending":
                dot, style = t.g["warn"], "yellow"
            elif online:
                dot, style = (t.g["pass"] if not t.unicode else "●"), "green"
            else:
                dot, style = (t.g["skip"] if not t.unicode else "○"), "grey"
            label = agent.get("name", "?")
            if agent.get("agent_id") == me:
                label += " (you)"
            rows.append([
                (dot, style),
                (label, ""),
                agent.get("kind", ""),
                agent.get("os", ""),
                (str(psr.get("state", "-")), _psr_state_style(psr)),
                truncate(str(psr.get("headline", "")) or ("no standing report" if online else ""), 46),
                (_age(psr.get("age_s")) + (" stale" if psr.get("stale") else ""),
                 "yellow" if psr.get("stale") else ""),
            ])
        t.write(t.table(
            ["", "AGENT", "KIND", "OS", "STATE", "DOING", "PSR AGE"], rows,
            aligns=["c", "l", "l", "l", "l", "l", "r"],
        ))
        missing = [a.get("name", "?") for a in agents if a.get("online") and not a.get("psr")]
        if missing:
            t.write("")
            t.write(t.warn("  no standing report from: %s" % ", ".join(missing)) +
                    t.dim("  -- they are not following the PSR standard (SPEC 6)"))
    return EXIT_OK, payload


def _psr_state_style(psr: dict) -> str:
    return {
        "working": "green", "planning": "cyan", "reviewing": "magenta",
        "blocked": "red", "waiting": "yellow",
    }.get(str(psr.get("state", "")), "grey")


def _age(seconds) -> str:
    if seconds is None:
        return "-"
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "-"
    if value < 90:
        return "%.0fs" % value
    if value < 5400:
        return "%.0fm" % (value / 60.0)
    return "%.1fh" % (value / 3600.0)


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #


def cmd_ledger(ctx: Ctx, args: argparse.Namespace) -> Tuple[int, dict]:
    workspace = _workspace(args)
    client, _ = _client(workspace)
    try:
        state = client.state()
    except Exception as exc:
        raise _translate(exc)
    ledger = dict(state.get("ledger") or {})
    lines = list(ledger.get("lines") or [])
    names = _agent_names(state)

    if not args.why:
        payload = {"ledger": ledger, "agents": len(lines)}
        if ctx.human:
            _render_ledger_table(ctx.out, lines, ledger)
        return EXIT_OK, payload

    target = _resolve_agent(args.why, lines, names)
    if target is None:
        raise CliError(
            "no agent matching %r in this parley" % args.why,
            code="no_such_agent", exit_code=EXIT_ERROR,
            hint="Run `parley roster` for the names and ids. --why takes either.",
        )
    payload = {"agent": target, "weights": ledger.get("weights", {}),
               "computed_at": ledger.get("computed_at", "")}
    if ctx.human:
        _render_ledger_why(ctx.out, target, ledger, lines)
    return EXIT_OK, payload


def _resolve_agent(needle: str, lines: List[dict], names: Dict[str, str]) -> Optional[dict]:
    needle_l = needle.lower()
    for line in lines:
        if line.get("agent_id", "").lower() == needle_l:
            return line
    for line in lines:
        if (line.get("name") or "").lower() == needle_l:
            return line
    for line in lines:
        if line.get("agent_id", "").lower().startswith(needle_l) or \
                (line.get("name") or "").lower().startswith(needle_l):
            return line
    return None


_COMPONENTS = ("contributions", "authored", "delivery", "influence", "presence")


def _render_ledger_table(t: Term, lines: List[dict], ledger: dict) -> None:
    if not lines:
        t.write(t.dim("  the Ledger is empty -- nobody has contributed a recorded event yet."))
        t.write(t.dim('  Record one with: parley know "what you decided" --kind decision'))
        return
    ordered = sorted(lines, key=lambda l: float(l.get("total") or 0), reverse=True)
    percents = _share_percents(lines)
    rows = []
    for line in ordered:
        comp = line.get("components") or {}
        pct = percents.get(line.get("agent_id", ""), 0.0)
        rows.append([
            line.get("name") or line.get("agent_id", ""),
            ("%.1f" % float(line.get("total") or 0), "bold"),
            "%.1f%%" % pct,
            _share_bar(t, pct / 100.0, line.get("agent_id", "")),
        ] + ["%.1f" % float(comp.get(c) or 0) for c in _COMPONENTS])
    t.write(t.table(
        ["AGENT", "POINTS", "SHARE", "", "CONTRIB", "AUTHORED", "DELIVERY", "INFLUENCE", "PRESENCE"],
        rows,
        aligns=["l", "r", "r", "l", "r", "r", "r", "r", "r"],
    ))
    t.write("")
    t.write(t.dim("  Recorded contribution, not quality (SPEC 9). %d events, computed %s."
                  % (ledger.get("event_count", 0), ledger.get("computed_at", "?"))))
    t.write(t.dim("  Why did someone score that?  parley ledger --why <agent>"))


def _share_percents(lines: List[dict]) -> Dict[str, float]:
    """Shares as percentages, whichever convention the Hub used.

    parley.ledger apportions ``share`` so the values sum to exactly 100.00, but a
    third-party Hub could just as reasonably publish fractions. Decide from the
    total rather than guessing per line.
    """
    raw = {str(l.get("agent_id", "")): float(l.get("share") or 0.0) for l in lines}
    total = sum(raw.values())
    scale = 1.0 if total > 2.0 else 100.0
    return {k: v * scale for k, v in raw.items()}


def _share_bar(t: Term, share: float, agent_id: str, width: int = 16) -> str:
    filled = int(round(max(0.0, min(1.0, share)) * width))
    if t.unicode:
        bar = "█" * filled + "·" * (width - filled)
    else:
        bar = "#" * filled + "." * (width - filled)
    return t.agent_colour(agent_id, bar)


def _render_ledger_why(t: Term, line: dict, ledger: dict, lines: List[dict]) -> None:
    width = t.layout_width(84)
    total = float(line.get("total") or 0)
    grand = sum(float(l.get("total") or 0) for l in lines) or 1.0
    t.write("")
    t.write(t.rule("ledger %s %s" % (t.g["arrow"], line.get("name") or line.get("agent_id", "")), width))
    t.write("")
    t.write("  " + t.bold("%.2f points" % total) +
            t.dim("  %s  %.1f%% of %.2f  %s  %s"
                  % (t.g["dot"], total / grand * 100.0, grand, t.g["dot"], line.get("agent_id", ""))))
    t.write("")

    comp = line.get("components") or {}
    weights = ledger.get("weights") or {}
    rows = []
    for name in _COMPONENTS:
        value = float(comp.get(name) or 0)
        rows.append([
            name,
            ("%.2f" % value, "bold" if value else "grey"),
            "%.0f%%" % (value / total * 100.0) if total else "-",
            _share_bar(t, (value / total) if total else 0.0, line.get("agent_id", ""), 20),
            _component_explanation(name, weights),
        ])
    t.write(t.table(["COMPONENT", "POINTS", "OF TOTAL", "", "DERIVED FROM"], rows,
                    aligns=["l", "r", "r", "l", "l"]))

    evidence = line.get("evidence") or {}
    if evidence:
        t.write("")
        t.write("  " + t.bold("The events behind those numbers"))
        for name in _COMPONENTS:
            items = evidence.get(name) or []
            if not items:
                continue
            t.write("")
            t.write("  " + t.key(name))
            rows = []
            for item in items[:40]:
                rows.append([
                    "seq %s" % item.get("seq", "-"),
                    truncate(str(item.get("label", "")), 58),
                    ("%+.2f" % float(item.get("points") or 0), "green"),
                ])
            t.write(["    " + ln for ln in t.table(["EVENT", "WHAT", "POINTS"], rows,
                                                   aligns=["l", "l", "r"])])
            if len(items) > 40:
                t.write(t.dim("    ... and %d more" % (len(items) - 40)))
    else:
        # The Hub did not ship evidence; ask the ledger module to explain itself.
        explained = _ledger_why_fallback(ledger, line.get("agent_id", ""))
        if explained:
            t.write("")
            for ln in explained.splitlines():
                t.write("  " + ln)
        else:
            t.write("")
            t.write(t.dim("  This Hub did not include per-event evidence in /v1/state."))
    t.write("")
    t.write(t.dim("  Weights come from .parley/ledger.json (SPEC 9); every point above is"))
    t.write(t.dim("  traceable to an event in the log -- that is the whole point of the Ledger."))
    t.write("")


def _component_explanation(name: str, weights: dict) -> str:
    return {
        "contributions": "knowledge.contribution x kind weight",
        "authored": "lines you wrote that are still in the current files",
        "delivery": "task.done you claimed (%s pts each)" % weights.get("task_done_points", 2.0),
        "influence": "others citing your events (%s pts each)" % weights.get("citation_received_points", 0.5),
        "presence": "chat, capped at %s pts" % weights.get("chat_points_cap", 10.0),
    }.get(name, "")


def _ledger_why_fallback(ledger: dict, agent_id: str) -> str:
    """Rebuild a LedgerResult from the wire dict so its own why() can speak."""
    try:
        ledger_mod = _mod("parley.ledger")
        lines = [ledger_mod.LedgerLine(**line) for line in (ledger.get("lines") or [])]
        result = ledger_mod.LedgerResult(
            lines=lines,
            weights=ledger.get("weights") or {},
            computed_at=ledger.get("computed_at") or "",
            event_count=int(ledger.get("event_count") or 0),
        )
        return str(result.why(agent_id) or "")
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# invite / approve  (host only)
# --------------------------------------------------------------------------- #


def cmd_invite(ctx: Ctx, args: argparse.Namespace) -> Tuple[int, dict]:
    workspace = _workspace(args)
    config, state_dir = _hub_config(workspace)
    host_token = getattr(config, "host_token", "")
    bind = getattr(config, "bind", "127.0.0.1")
    port = int(getattr(config, "port", 7777))
    # Two URLs on purpose: admin calls go to the address the Hub is actually
    # listening on (a Hub bound to 127.0.0.1 is not reachable at the LAN
    # address), while what gets printed is the one a colleague can type.
    admin_url = "http://%s:%d" % ("127.0.0.1" if bind in ("0.0.0.0", "::", "") else bind, port)
    hub_url = public_url(admin_url)
    policy = dict(getattr(config, "policy", None) or {})
    session = getattr(config, "session", "")
    fingerprint = getattr(config, "fingerprint", "")
    name = getattr(config, "name", "")

    payload: Dict[str, Any] = {
        "session": session,
        "name": name,
        "fingerprint": fingerprint,
        "hub_url": hub_url,
        "policy": policy,
        "state_dir": str(state_dir),
    }

    if args.rotate:
        result = _admin_request(admin_url, host_token, "/v1/admin/rotate-watchword", {}, ctx.timeout)
        watchword = result.get("watchword") or ""
        if not watchword:
            raise CliError(
                "the Hub rotated the watchword but did not return it",
                code="no_watchword",
                hint="That is a Hub bug -- the new watchword is unrecoverable, so rotate again "
                     "once it is fixed. Existing agents are unaffected (SPEC 3.8).",
            )
        fingerprint = result.get("fingerprint", fingerprint)
        payload.update({"watchword": watchword, "fingerprint": fingerprint, "rotated": True})
        if ctx.human:
            _print_invite_screen(ctx, payload, workspace, bind, port, args, heading="new watchword issued")
            ctx.out.write(ctx.out.dim("  Everyone already in the parley stays in -- agent keys do not"))
            ctx.out.write(ctx.out.dim("  derive from the watchword (SPEC 3.8). Only new joins are affected."))
            ctx.out.blank()
        return EXIT_OK, payload

    if args.deck:
        result = _admin_request(admin_url, host_token, "/v1/admin/viewer-token",
                                {"label": args.label or "cli"}, ctx.timeout)
        token = result.get("viewer_token") or result.get("token") or ""
        if not token:
            raise CliError("the Hub did not return a viewer token", code="no_token")
        deck = public_url(str(result.get("deck_url") or "")) or ("%s/?vt=%s" % (hub_url, token))
        payload.update({"deck_url": deck, "viewer_token": token,
                        "expires_in_s": result.get("expires_in_s")})
        if ctx.human:
            ctx.out.write("  " + ctx.out.bold("Deck link") +
                          ctx.out.dim("   (read-only, share freely)"))
            ctx.out.write("  " + ctx.out.paint(deck, "underline"))
            ctx.out.write(ctx.out.dim("  Read-only. No blobs, no writes, and it never shows the watchword."))
        return EXIT_OK, payload

    if args.reveal:
        # The Hub stores only the derived root key and a hash of the watchword
        # (SPEC/config.HubConfig), so the plain words genuinely cannot be
        # recovered unless the Hub chose to keep them. Ask, then be honest.
        try:
            result = _admin_request(admin_url, host_token, "/v1/admin/reveal-invite", {}, ctx.timeout)
        except CliError:
            result = {}
        watchword = (result or {}).get("watchword") or ""
        if watchword:
            payload.update({"watchword": watchword, "revealed": True})
            if ctx.human:
                _print_invite_screen(ctx, payload, workspace, bind, port, args, heading="current watchword")
            return EXIT_OK, payload
        raise CliError(
            "this Hub cannot reveal the watchword",
            code="watchword_not_recoverable",
            exit_code=EXIT_ERROR,
            hint="By design it stores only the derived root key and a hash of the words "
                 "(SPEC 3.2), so there is nothing to print. Issue a fresh one instead: "
                 "`parley invite --rotate` -- it does not disconnect anybody, because "
                 "agent keys do not derive from the watchword.",
            detail={"session": session, "fingerprint": fingerprint},
        )

    # Plain `parley invite`: everything except the secret.
    if ctx.human:
        t = ctx.out
        t.write(t.kv([
            ("parley", t.bold(name or session)),
            ("session", session),
            ("fingerprint", t.paint(fingerprint, "bcyan")),
            ("hub", hub_url),
            ("enrolment", "open" if policy.get("enroll_open", True) else "closed"),
            ("approval", "required" if policy.get("require_approval") else "automatic"),
            ("sealed", "yes" if policy.get("sealed") else "no"),
        ], gap=3))
        t.write("")
        t.write(t.dim("  The watchword is not stored in recoverable form and is not printed here."))
        t.write(t.dim("  `parley invite --rotate` issues a new one (nobody gets disconnected)."))
        t.write(t.dim("  `parley invite --deck` mints a read-only Deck link."))
    return EXIT_OK, payload


def _print_invite_screen(ctx: Ctx, payload: dict, workspace: Path, bind: str, port: int,
                         args: argparse.Namespace, heading: str) -> None:
    ctx.out.blank()
    ctx.out.write(render_invite(
        ctx.out,
        name=payload.get("name", ""),
        session=payload.get("session", ""),
        watchword=payload.get("watchword", ""),
        fingerprint=payload.get("fingerprint", ""),
        hub_url=payload.get("hub_url", ""),
        deck_url=payload.get("deck_url", ""),
        workspace=str(workspace),
        bind=bind,
        port=port,
        policy=payload.get("policy", {}) or {},
        phonetic=bool(getattr(args, "phonetic", False)),
        heading=heading,
    ))


def cmd_approve(ctx: Ctx, args: argparse.Namespace) -> Tuple[int, dict]:
    workspace = _workspace(args)
    config, _ = _hub_config(workspace)
    bind = getattr(config, "bind", "127.0.0.1")
    port = int(getattr(config, "port", 7777))
    hub_url = "http://%s:%d" % ("127.0.0.1" if bind in ("0.0.0.0", "::", "") else bind, port)
    result = _admin_request(hub_url, getattr(config, "host_token", ""),
                            "/v1/admin/approve", {"agent_id": args.agent_id}, ctx.timeout)
    payload = {"agent_id": args.agent_id, "result": result}
    if ctx.human:
        ctx.out.write("%s %s" % (ctx.out.ok(ctx.out.g["pass"]),
                                 "approved %s -- they can write now" % args.agent_id))
    return EXIT_OK, payload


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def cmd_doctor(ctx: Ctx, args: argparse.Namespace) -> Tuple[int, dict]:
    doctor = _mod("parley.doctor")
    workspace = _workspace(args)
    hub_url = args.hub or os.environ.get("PARLEY_HUB") or ""
    if ctx.human:
        ctx.note("running checks against %s%s ..." % (workspace, (" and " + hub_url) if hub_url else ""))
    checks = doctor.run_checks(workspace, hub_url=hub_url)
    code = doctor.exit_code_for(checks)
    if ctx.human:
        ctx.out.blank()
        ctx.out.write(doctor.render(checks, as_json=False))
        ctx.out.blank()
    return code, doctor.summarise(checks)


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

EPILOGUE = """\
getting started
  parley init                      host a parley; prints a watchword to read out loud
  parley resume                    restart that same parley later; nobody has to re-join
  parley join --discover --invite "copper-otter-climbs-the-quiet-hill"
  parley run                       stay connected: sync files, keep your report fresh
  parley say "..." / parley status "..." / parley know "..." --kind finding
  parley watch --json              tail the log, one JSON object per line
  parley doctor                    anything wrong? run this first; it says what to fix

every command takes --json (single object on stdout; watch streams one per line).
exit codes: 0 ok . 1 error . 2 usage . 3 auth . 4 cannot reach hub . 5 fingerprint.
docs: docs/SPEC.md (the contract), AGENTS.md (how to join as an agent).
"""


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser whose usage errors are machine-readable in --json mode."""

    def error(self, message):  # noqa: D102
        if _JSON_MODE[0]:
            _emit_json({
                "ok": False,
                "command": (self.prog or "parley").replace("parley ", "") or "parley",
                "exit_code": EXIT_USAGE,
                "error": {
                    "code": "usage",
                    "message": message,
                    "retryable": False,
                    "hint": "run `%s --help` for the accepted arguments" % (self.prog or "parley"),
                },
            })
            raise SystemExit(EXIT_USAGE)
        self.print_usage(sys.stderr)
        sys.stderr.write("%s: error: %s\n" % (self.prog, message))
        sys.stderr.write("try `%s --help`\n" % (self.prog or "parley"))
        raise SystemExit(EXIT_USAGE)


def _add_common(parser: argparse.ArgumentParser, suppress: bool) -> None:
    def default(value):
        return argparse.SUPPRESS if suppress else value

    parser.add_argument("--json", action="store_true", default=default(False),
                        help="machine-readable output: one JSON object on stdout, humans on stderr")
    parser.add_argument("--color", "--colour", dest="colour", action="store_const", const=True,
                        default=default(None), help="force ANSI colour even when not a terminal")
    parser.add_argument("--no-color", "--no-colour", dest="colour", action="store_const", const=False,
                        default=default(None), help="never emit ANSI colour")
    parser.add_argument("--ascii", action="store_true", default=default(False),
                        help="plain ASCII only, no box drawing")
    parser.add_argument("--quiet", "-q", action="store_true", default=default(False),
                        help="suppress non-essential output")
    parser.add_argument("--verbose", action="store_true", default=default(False),
                        help="show a traceback when something unexpected fails")
    parser.add_argument("--timeout", type=float, default=default(15.0),
                        help="network timeout in seconds (default 15)")


def build_parser() -> argparse.ArgumentParser:
    common = _Parser(add_help=False)
    _add_common(common, suppress=True)

    parser = _Parser(
        prog="parley",
        description="Parley -- a protocol for agents of any kind, on any OS, to collaborate "
                    "on one project.",
        epilog=EPILOGUE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(parser, suppress=False)
    try:
        from parley.version import __version__, WIRE_VERSION

        version_string = "parley %s (%s)" % (__version__, WIRE_VERSION)
    except Exception:
        version_string = "parley (version unavailable)"
    parser.add_argument("-V", "--version", action="version", version=version_string)

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # -- init -------------------------------------------------------------- #
    p = sub.add_parser("init", parents=[common], help="host a new parley and print the watchword",
                       description="Create a parley, start the Hub, and print the invite. The Hub "
                                   "lives in this process: Ctrl-C ends the parley.")
    p.add_argument("--name", help="name of the parley (default: the workspace folder name)")
    p.add_argument("--workspace", help="project folder to share (default: the current directory)")
    p.add_argument("--port", type=int, default=7777, help="TCP port for the Hub (default 7777)")
    p.add_argument("--bind", default="0.0.0.0", help="address to bind (default 0.0.0.0 = this whole LAN)")
    p.add_argument("--public", action="store_true",
                   help="internet-facing defaults: expiring watchword, approval required, no LAN discovery")
    p.add_argument("--seal", action="store_true", help="encrypt message bodies (use when you have no TLS)")
    p.add_argument("--approve", action="store_true", help="new agents wait for `parley approve`")
    p.add_argument("--words", type=int, default=5, help="words of entropy in the watchword (default 5)")
    p.add_argument("--phonetic", action="store_true", help="also print the watchword in the NATO alphabet")
    p.add_argument("--me", help="your own display name in the parley")
    p.add_argument("--kind", help="your own agent kind (claude-code, cursor, human, ...)")
    p.add_argument("--no-join", action="store_true",
                   help="do not enrol this terminal as a participant, just host")
    p.add_argument("--force", action="store_true",
                   help="discard the parley already in this folder and start a new one -- "
                        "every enrolled agent is locked out and the log is deleted")
    p.set_defaults(func=cmd_init)

    # -- resume ------------------------------------------------------------- #
    p = sub.add_parser("resume", parents=[common],
                       help="restart the Hub on an existing parley, keeping everyone enrolled",
                       description="Restart the Hub on the state directory `parley init` left "
                                   "behind, keeping the session id, root key, fingerprint, event "
                                   "log, blobs and enrolled agents. This -- not `init` -- is what "
                                   "a service supervisor should run: `init` mints a new parley "
                                   "every time and would strand every client behind a "
                                   "fingerprint_mismatch. No watchword is printed, because "
                                   "resume does not mint one.")
    p.add_argument("--workspace", help="the folder the Hub was created in (default: current)")
    p.add_argument("--port", type=int, default=None,
                   help="bind a different port than the stored one (persisted)")
    p.add_argument("--bind", default=None,
                   help="bind a different address than the stored one (persisted)")
    p.set_defaults(func=cmd_resume)

    # -- join -------------------------------------------------------------- #
    p = sub.add_parser("join", parents=[common], help="join a parley with a watchword",
                       description="Enrol in a parley. The watchword is forgiving: case, spaces, "
                                   "dashes and punctuation are all normalised away.")
    p.add_argument("--hub", help="Hub URL, e.g. http://192.168.1.20:7777")
    p.add_argument("--discover", action="store_true", help="find a Hub on this LAN instead of typing an address")
    p.add_argument("--invite", help='the watchword, however you heard it (or "-" to read stdin)')
    p.add_argument("--name", help="your display name (default: user@host)")
    p.add_argument("--kind", help="what kind of agent you are (default: auto-detected)")
    p.add_argument("--model", help="model identifier, if you are a model")
    p.add_argument("--workspace", help="folder to sync into (default: the current directory)")
    p.add_argument("--seal", action="store_true", help="encrypt message bodies")
    p.add_argument("--expect-fingerprint", help="refuse to join unless the Hub's three words match this")
    p.add_argument("--force", action="store_true", help="enrol again even if this folder already has credentials")
    p.set_defaults(func=cmd_join)

    # -- run --------------------------------------------------------------- #
    p = sub.add_parser("run", parents=[common], help="stay connected: sync, stream, heartbeat",
                       description="The long-running daemon. Streams the log into .parley/inbox.jsonl, "
                                   "publishes .parley/outbox.jsonl, syncs the workspace and keeps your "
                                   "standing report fresh. Ctrl-C stops it cleanly.")
    p.add_argument("--workspace", help="the parley workspace (default: the current directory)")
    p.add_argument("--no-sync", action="store_true", help="chat and status only; do not sync files")
    p.add_argument("--psr-from", metavar="FILE", help="file to read your standing report from (default .parley/me.json)")
    p.set_defaults(func=cmd_run)

    # -- say --------------------------------------------------------------- #
    p = sub.add_parser("say", parents=[common], help="send a chat message",
                       description="Say something to the parley.")
    p.add_argument("message", help='the message (or "-" to read stdin)')
    p.add_argument("--to", action="append", help="agent id(s) to address; repeatable or comma-separated")
    p.add_argument("--reply", metavar="EVT", help="event id this replies to")
    p.add_argument("--ref", action="append",
                   help="cite a file path, evt_ id or tsk_ id; repeatable. Citations feed the Ledger.")
    p.add_argument("--workspace", help="the parley workspace")
    p.set_defaults(func=cmd_say)

    # -- status ------------------------------------------------------------ #
    p = sub.add_parser("status", parents=[common], help="publish your standing report (PSR)",
                       description="Set what you are doing (SPEC 6). With no headline, prints the "
                                   "current state of the parley and your own report instead.")
    p.add_argument("headline", nargs="?", help="<= 80 chars, present tense, no trailing period")
    p.add_argument("--state", default="working", choices=list(PSR_STATES), help="default: working")
    p.add_argument("--focus", action="append", help="workspace-relative path you are working in; repeatable")
    p.add_argument("--detail", help="a sentence of extra context")
    p.add_argument("--progress", type=float, help="0.0 to 1.0")
    p.add_argument("--task", help="task id this relates to")
    p.add_argument("--needs", action="append", help="what you are waiting for; repeatable")
    p.add_argument("--eta", type=float, metavar="SECONDS", help="your estimate to completion")
    p.add_argument("--blocked-on", metavar="AGENT", help="agent id you are blocked on")
    p.add_argument("--workspace", help="the parley workspace")
    p.set_defaults(func=cmd_status)

    # -- know -------------------------------------------------------------- #
    p = sub.add_parser("know", parents=[common], help="record a knowledge contribution",
                       description="Record a decision, finding or design so it is in the log and the "
                                   "Ledger (SPEC 4.7, 9).")
    p.add_argument("title", help="one line: what you learned or decided")
    p.add_argument("--kind", required=True, choices=list(KNOWLEDGE_KINDS))
    p.add_argument("--detail", help="the reasoning -- this is what makes it useful later")
    p.add_argument("--ref", action="append", help="file path, evt_ or tsk_ id; repeatable")
    p.add_argument("--workspace", help="the parley workspace")
    p.set_defaults(func=cmd_know)

    # -- task -------------------------------------------------------------- #
    p = sub.add_parser("task", parents=[common], help="create, claim and finish tasks")
    p.add_argument("--workspace", help="the parley workspace")
    tsub = p.add_subparsers(dest="task_action", metavar="<action>")

    tp = tsub.add_parser("create", parents=[common], help="create a task")
    tp.add_argument("title")
    tp.add_argument("--detail")
    tp.add_argument("--tag", action="append")
    tp.add_argument("--priority", type=int, help="1 (highest) to 5")
    tp.add_argument("--depends-on", action="append", metavar="TASK_ID")
    tp.add_argument("--id", help="use this task id instead of generating one")
    tp.add_argument("--workspace", default=argparse.SUPPRESS, help="the parley workspace")

    tp = tsub.add_parser("claim", parents=[common], help="claim a task")
    tp.add_argument("id")
    tp.add_argument("--workspace", default=argparse.SUPPRESS, help="the parley workspace")

    tp = tsub.add_parser("release", parents=[common], help="give a task back")
    tp.add_argument("id")
    tp.add_argument("--reason")
    tp.add_argument("--workspace", default=argparse.SUPPRESS, help="the parley workspace")

    tp = tsub.add_parser("update", parents=[common], help="change a task's status or progress")
    tp.add_argument("id")
    tp.add_argument("--status", required=True, choices=list(TASK_STATUSES))
    tp.add_argument("--progress", type=float)
    tp.add_argument("--note")
    tp.add_argument("--workspace", default=argparse.SUPPRESS, help="the parley workspace")

    tp = tsub.add_parser("done", parents=[common], help="mark a task finished")
    tp.add_argument("id")
    tp.add_argument("--result")
    tp.add_argument("--ref", action="append")
    tp.add_argument("--workspace", default=argparse.SUPPRESS, help="the parley workspace")

    tp = tsub.add_parser("list", parents=[common], help="list the tasks")
    tp.add_argument("--status", choices=list(TASK_STATUSES))
    tp.add_argument("--mine", action="store_true", help="only tasks you have claimed")
    tp.add_argument("--workspace", default=argparse.SUPPRESS, help="the parley workspace")
    p.set_defaults(func=cmd_task, task_action=None)

    # -- watch ------------------------------------------------------------- #
    p = sub.add_parser("watch", parents=[common], help="tail the parley log",
                       description="Follow the log. With --json, one JSON object per line, flushed "
                                   "immediately -- safe to read from a pipe.")
    p.add_argument("--types", action="append", metavar="PREFIX",
                   help="only these types; prefix match, comma-separated or repeatable (e.g. chat.,task.)")
    p.add_argument("--since", type=int, help="start at this seq (0 = the whole log)")
    p.add_argument("--tail", type=int, default=20, help="events of backlog when --since is absent (default 20)")
    p.add_argument("--count", type=int, metavar="N", help="stop after N matching events")
    p.add_argument("--workspace", help="the parley workspace")
    p.set_defaults(func=cmd_watch)

    # -- roster ------------------------------------------------------------ #
    p = sub.add_parser("roster", parents=[common], help="who is here and what they are doing")
    p.add_argument("--workspace", help="the parley workspace")
    p.set_defaults(func=cmd_roster)

    # -- ledger ------------------------------------------------------------ #
    p = sub.add_parser("ledger", parents=[common], help="contribution scores, and why",
                       description="The Ledger measures recorded contribution, not quality (SPEC 9). "
                                   "Every point is traceable to an event.")
    p.add_argument("--why", metavar="AGENT", help="full breakdown for one agent (id or name)")
    p.add_argument("--workspace", help="the parley workspace")
    p.set_defaults(func=cmd_ledger)

    # -- invite ------------------------------------------------------------ #
    p = sub.add_parser("invite", parents=[common], help="show or rotate the invite (host only)",
                       description="Host-only. Needs the host token from the machine that ran "
                                   "`parley init`.")
    p.add_argument("--reveal", action="store_true", help="print the current watchword, if this Hub kept it")
    p.add_argument("--rotate", action="store_true", help="issue a new watchword (nobody is disconnected)")
    p.add_argument("--deck", action="store_true", help="mint a read-only Deck link")
    p.add_argument("--label", help="label for the minted viewer token")
    p.add_argument("--phonetic", action="store_true", help="also print the watchword in the NATO alphabet")
    p.add_argument("--workspace", help="the workspace the Hub was created in")
    p.set_defaults(func=cmd_invite)

    # -- approve ----------------------------------------------------------- #
    p = sub.add_parser("approve", parents=[common], help="let a pending agent in (host only)")
    p.add_argument("agent_id")
    p.add_argument("--workspace", help="the workspace the Hub was created in")
    p.set_defaults(func=cmd_approve)

    # -- doctor ------------------------------------------------------------ #
    p = sub.add_parser("doctor", parents=[common], help="diagnose everything, and say how to fix it",
                       description="Runs every check in SPEC 13 for real: clock skew against the Hub, "
                                   "a live SSE event, the long-poll fallback, a blob round trip, and "
                                   "whether this Hub is exposed to the internet without TLS.")
    p.add_argument("--hub", help="check this Hub instead of the one in your credentials")
    p.add_argument("--workspace", help="the parley workspace")
    p.set_defaults(func=cmd_doctor)

    return parser


def _overview_payload() -> dict:
    """What `parley --json` with no command returns: a machine-readable inventory."""
    try:
        from parley.version import __version__, WIRE_VERSION
    except Exception:
        __version__, WIRE_VERSION = "", "PARLEY/1"
    commands = []
    for action in build_parser()._subparsers._group_actions:  # type: ignore[union-attr]
        for name, sub in getattr(action, "choices", {}).items():
            commands.append({"name": name, "help": (sub.description or "").strip()})
        break
    return {
        "version": __version__,
        "wire_version": WIRE_VERSION,
        "commands": commands,
        "exit_codes": {"0": "ok", "1": "error", "2": "usage", "3": "auth/credential",
                       "4": "cannot reach hub", "5": "fingerprint mismatch"},
        "json_contract": {
            "envelope": {"ok": "bool", "command": "str", "exit_code": "int",
                         "data": "object (on success)", "error": "object (on failure)"},
            "error": {"code": "str", "message": "str", "hint": "str", "retryable": "bool"},
            "streaming": "`watch --json` emits one event object per line instead of an "
                         "envelope; `init --json` and `run --json` emit their envelope "
                         "immediately and then keep running.",
            "stdout": "JSON only. All human-readable output goes to stderr in --json mode.",
        },
        "docs": ["docs/SPEC.md", "docs/INTERNAL-API.md", "AGENTS.md"],
    }


def print_overview(stream=None) -> None:
    """What bare `parley` prints: orientation, not an argparse error."""
    t = Term(stream or sys.stdout)
    width = t.layout_width(76)
    t.write("")
    t.write(t.box(
        [t.paint(" ".join("PARLEY"), "bold", "cyan") +
         t.dim("   agents of any kind, on any OS, working on one project")],
        width=width, style="cyan",
    ))
    t.write("")
    t.write("  " + t.bold("Host a parley") + t.dim("  (you read the watchword out to the others)"))
    t.write("    parley init")
    t.write("    parley resume" + t.dim("                    restart it later, same session, nobody re-joins"))
    t.write("")
    t.write("  " + t.bold("Join one"))
    t.write('    parley join --discover --invite "copper-otter-climbs-the-quiet-hill"')
    t.write('    parley join --hub http://192.168.1.20:7777 --invite "..."')
    t.write("")
    t.write("  " + t.bold("Then, in that folder"))
    t.write("    parley run" + t.dim("                       stay connected and sync"))
    t.write('    parley status "what you are doing"' + t.dim("  the standing report everyone reads"))
    t.write('    parley say "..."' + t.dim("                 talk to the parley"))
    t.write("    parley watch --json" + t.dim("              tail the log, one JSON object per line"))
    t.write("    parley roster" + t.dim("                    who is here"))
    t.write("")
    t.write("  " + t.bold("If anything is wrong"))
    t.write("    parley doctor" + t.dim("                    it usually tells you the answer"))
    t.write("")
    t.write(t.dim("  parley --help for everything. Every command takes --json."))
    t.write(t.dim("  exit codes: 0 ok %s 1 error %s 2 usage %s 3 auth %s 4 no hub %s 5 fingerprint"
                  % tuple([t.g["dot"]] * 5)))
    t.write("")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def _render_error(ctx: Ctx, command: str, err: CliError) -> None:
    if ctx.json:
        _emit_json({
            "ok": False,
            "command": command,
            "exit_code": err.exit_code,
            "error": err.to_dict(),
        })
        return
    t = ctx.err
    if err.loud:
        width = t.layout_width(76)
        t.write("")
        t.write(t.box(
            [t.paint("!! " + line, "red", "bold") for line in _wrap(err.message, width - 9)] +
            [""] +
            [t.paint(line, "red") for line in _wrap(err.hint, width - 6)],
            width=width, double=True, style="red",
        ))
        t.write("")
        return
    t.write(t.paint("error", "red", "bold") + ": " + err.message)
    if err.hint:
        for line in _wrap(err.hint, max(40, t.layout_width(86) - 2)):
            t.write(t.dim("  " + line))
    if ctx.verbose and err.detail:
        t.write(t.dim("  detail: " + json.dumps(err.detail, ensure_ascii=False, default=str)))


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _JSON_MODE[0] = "--json" in argv

    parser = build_parser()
    if not argv:
        print_overview()
        return EXIT_OK

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help, --version, or a usage error
        return int(exc.code or 0)

    if not getattr(args, "command", None):
        if _JSON_MODE[0]:
            # `parley --json` with no command: hand an agent the inventory rather
            # than prose it would have to parse.
            _emit_json({"ok": True, "command": "parley", "exit_code": EXIT_OK,
                        "data": _overview_payload()})
        else:
            print_overview()
        return EXIT_OK

    ctx = Ctx(args)
    command = str(args.command)

    if command == "task" and not getattr(args, "task_action", None):
        err = CliError(
            "parley task needs an action",
            code="usage", exit_code=EXIT_USAGE,
            hint="One of: create, claim, release, update, done, list. "
                 "Try `parley task --help`.",
        )
        _render_error(ctx, command, err)
        return EXIT_USAGE

    try:
        code, payload = args.func(ctx, args)
    except CliError as err:
        _render_error(ctx, command, err)
        return err.exit_code
    except KeyboardInterrupt:
        if ctx.json:
            _emit_json({"ok": True, "command": command, "exit_code": EXIT_OK,
                        "data": {"interrupted": True}})
        else:
            ctx.err.write(ctx.err.dim("\ninterrupted."))
        return EXIT_OK
    except BrokenPipeError:  # `parley watch | head`
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except Exception:
            pass
        return EXIT_OK
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:
        err = _translate(exc)
        if ctx.verbose:
            import traceback

            traceback.print_exc()
        _render_error(ctx, command, err)
        return err.exit_code

    if ctx.json and not ctx.streaming:
        _emit_json({"ok": code == EXIT_OK, "command": command, "exit_code": code, "data": payload})
    try:
        sys.stdout.flush()
    except Exception:
        pass
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
