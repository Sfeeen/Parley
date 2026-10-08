#!/usr/bin/env python3
"""Parley bootstrap -- the first thing to run after cloning.

Zero dependencies beyond the Python standard library, and deliberately written
against Python 3.6 syntax (no f-strings, no walrus, no dataclasses) so that it can
still *tell you* your Python is too old instead of dying with a SyntaxError. That
is the entire reason this file looks older than the rest of the codebase.

What it does
------------
  --check          verify the Python version and that the clone is intact, then exit
  (no arguments)   interactive: walks you through starting or joining a parley
  --init / --join  non-interactive: same thing, from flags

Examples
--------
  python3 scripts/bootstrap.py --check
  python3 scripts/bootstrap.py
  python3 scripts/bootstrap.py --init --name "my-parley" --workspace ~/work/ws
  python3 scripts/bootstrap.py --join --hub http://192.168.1.20:7777 \
      --invite "copper-otter-climbs-the-quiet-hill" --name Bram --workspace ~/work/ws
  python3 scripts/bootstrap.py --join --discover --invite "copper-otter-..." --name Bram

See AGENTS.md for the full agent-facing procedure and docs/QUICKSTART.md for the
human one.
"""

from __future__ import print_function

import argparse
import os
import subprocess
import sys

MIN_PYTHON = (3, 9)
WIRE_VERSION = "PARLEY/1"

# Files that must exist for the clone to be usable. Chosen to catch a partial
# clone, a stray download of a single file, or running this from the wrong place.
REQUIRED_PATHS = [
    "parley",
    "parley/__init__.py",
    "parley/cli.py",
    "parley/crypto.py",
    "parley/protocol.py",
    "parley/hub",
    "parley/client",
    "docs/SPEC.md",
    "AGENTS.md",
]

# Not fatal, but worth saying something about.
EXPECTED_PATHS = [
    "docs/PROTOCOL.md",
    "docs/QUICKSTART.md",
    "docs/DEPLOY.md",
    "docs/TROUBLESHOOTING.md",
    "examples/generic-agent",
    "tests",
]


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

def _colour_enabled():
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    return os.environ.get("TERM", "") != "dumb"


_COLOUR = _colour_enabled()


def _c(code, text):
    if not _COLOUR:
        return text
    return "\033[" + code + "m" + text + "\033[0m"


def ok(text):
    print("  " + _c("32", "ok") + "    " + text)


def warn(text):
    print("  " + _c("33", "warn") + "  " + text)


def fail(text):
    print("  " + _c("31", "FAIL") + "  " + text)


def heading(text):
    print("")
    print(_c("1", text))
    print(_c("2", "-" * len(text)))


def die(message, code=1):
    sys.stderr.write("parley: " + message + "\n")
    raise SystemExit(code)


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #

def repo_root():
    """The clone root, derived from this file's location, not from the cwd."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check_python():
    have = sys.version_info[:2]
    want = MIN_PYTHON
    pretty = "%d.%d.%d" % sys.version_info[:3]
    if have < want:
        fail("Python %s -- Parley needs %d.%d or newer" % (pretty, want[0], want[1]))
        print("")
        print("    Parley has no dependencies; there is nothing to pip install.")
        print("    You only need a newer interpreter:")
        print("")
        print("      Debian/Ubuntu   sudo apt install python3")
        print("      Fedora/RHEL     sudo dnf install python3")
        print("      macOS           brew install python@3.12")
        print("      Windows         winget install Python.Python.3.12")
        print("")
        print("    If a newer Python is already installed, run bootstrap with it:")
        print("      python3.12 scripts/bootstrap.py --check")
        print("")
        print("    If you cannot install one at all, you can still participate in")
        print("    Pigeonhole mode -- see AGENTS.md section 9. It needs no Python.")
        return False
    ok("Python %s (minimum %d.%d)" % (pretty, want[0], want[1]))
    return True


def check_stdlib():
    """R1 says stdlib only -- but some distributions ship a stripped stdlib."""
    needed = [
        "hashlib", "hmac", "secrets", "sqlite3", "json", "http.server",
        "urllib.request", "pathlib", "threading", "socket", "gzip", "unicodedata",
    ]
    missing = []
    for mod in needed:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        fail("missing stdlib modules: " + ", ".join(missing))
        print("")
        print("    Your Python installation is incomplete. On Debian/Ubuntu the")
        print("    usual culprit is a missing python3-full or libsqlite3 package:")
        print("      sudo apt install python3-full")
        return False
    ok("standard library complete (%d modules checked)" % len(needed))
    return True


def check_repo(root):
    missing = [p for p in REQUIRED_PATHS if not os.path.exists(os.path.join(root, p))]
    if missing:
        fail("clone is incomplete; missing: " + ", ".join(missing))
        print("")
        print("    Expected a full clone at: " + root)
        print("    Re-clone it:")
        print("      git clone https://github.com/Sfeeen/Parley.git")
        return False
    ok("clone looks intact: " + root)

    soft = [p for p in EXPECTED_PATHS if not os.path.exists(os.path.join(root, p))]
    if soft:
        warn("optional paths missing: " + ", ".join(soft))
    return True


def check_importable(root):
    """The real test: can Python actually import the package from here?"""
    env = dict(os.environ)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    try:
        out = subprocess.check_output(
            [sys.executable, "-c",
             "import parley.version as v; print(v.__version__ + ' ' + v.WIRE_VERSION)"],
            env=env, stderr=subprocess.STDOUT,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = getattr(exc, "output", b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        fail("cannot import the parley package")
        for line in (detail or str(exc)).strip().splitlines()[-6:]:
            print("        " + line)
        print("")
        print("    The package is still being built, or the clone is damaged.")
        return False

    text = out.decode("utf-8", "replace").strip()
    parts = text.split()
    version = parts[0] if parts else "?"
    wire = parts[1] if len(parts) > 1 else "?"
    if wire != WIRE_VERSION:
        warn("parley %s speaks %s; this bootstrap knows %s" % (version, wire, WIRE_VERSION))
    else:
        ok("parley %s, wire %s" % (version, wire))
    return True


def run_checks(root, quiet=False):
    if not quiet:
        heading("Checks")
    results = [check_python()]
    if results[0]:
        results.append(check_stdlib())
        results.append(check_repo(root))
        if results[-1]:
            results.append(check_importable(root))
    return all(results)


# --------------------------------------------------------------------------- #
# running the CLI
# --------------------------------------------------------------------------- #

def run_parley(root, workspace, args):
    """Invoke `python -m parley ...` with the clone importable and the workspace as cwd."""
    env = dict(os.environ)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [sys.executable, "-m", "parley"] + list(args)

    print("")
    print(_c("2", "  $ cd " + workspace))
    print(_c("2", "  $ PYTHONPATH=" + root + " " + " ".join(cmd)))
    print("")
    try:
        return subprocess.call(cmd, cwd=workspace, env=env)
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        die("could not run the parley CLI: %s" % exc)


def explain_exit(code):
    messages = {
        2: "usage error -- check the flags",
        3: "authentication failed -- is the watchword right?",
        4: "cannot reach the Hub -- check the URL and the firewall "
           "(docs/TROUBLESHOOTING.md section 1)",
        5: "FINGERPRINT MISMATCH -- you reached a different Hub than the one you "
           "were invited to. Stop and tell a human.",
    }
    if code in messages:
        print("")
        fail(messages[code])


# --------------------------------------------------------------------------- #
# workspace handling
# --------------------------------------------------------------------------- #

def prepare_workspace(root, path):
    """Expand, create and sanity-check a workspace directory."""
    ws = os.path.abspath(os.path.expanduser(path))

    if os.path.normcase(ws) == os.path.normcase(os.path.abspath(root)):
        die("the workspace must not be the Parley clone itself.\n"
            "       Everything in the workspace is replicated to every participant.\n"
            "       Pick a separate directory, e.g. ~/work/parley-ws")

    if not os.path.isdir(ws):
        try:
            os.makedirs(ws)
        except OSError as exc:
            die("cannot create workspace %s: %s" % (ws, exc))
        print("  created workspace: " + ws)
    else:
        print("  workspace: " + ws)

    if not os.access(ws, os.W_OK):
        die("workspace is not writable: " + ws)

    return ws


def has_credentials(workspace):
    return os.path.isfile(os.path.join(workspace, ".parley", "credentials.json"))


# --------------------------------------------------------------------------- #
# interactive flow
# --------------------------------------------------------------------------- #

def ask(prompt, default=""):
    suffix = " [" + default + "]: " if default else ": "
    try:
        answer = input(prompt + suffix).strip()
    except (EOFError, KeyboardInterrupt):
        print("")
        die("cancelled", 130)
    return answer or default


def ask_choice(prompt, choices):
    while True:
        answer = ask(prompt).strip().lower()
        if answer in choices:
            return answer
        print("  please answer one of: " + ", ".join(sorted(choices)))


def interactive(root):
    print("")
    print(_c("1", "Parley bootstrap"))
    print("")
    print("A parley is one collaboration session. Exactly one participant hosts")
    print("the Hub; everyone else joins it with a spoken watchword.")

    if not run_checks(root):
        print("")
        die("checks failed -- fix the above and run this again")

    heading("Starting or joining?")
    print("")
    print("  1  Start a new parley  (you host the Hub and hand out the watchword)")
    print("  2  Join an existing one  (someone gave you a watchword)")
    print("")
    choice = ask_choice("Choose 1 or 2", {"1", "2", "start", "join"})
    if choice in ("1", "start"):
        return interactive_init(root)
    return interactive_join(root)


def interactive_init(root):
    heading("Start a new parley")

    name = ask("Name for this parley", "parley")
    ws = prepare_workspace(root, ask(
        "Workspace folder (the shared folder -- NOT this clone)",
        os.path.join(os.path.expanduser("~"), "work", "parley-ws")))

    print("")
    print("  Who needs to reach the Hub?")
    print("    lan       -- everyone is on this network   (plain HTTP, zero config)")
    print("    internet  -- someone is not                (bind localhost, use a tunnel)")
    print("")
    where = ask_choice("  lan or internet", {"lan", "internet"})

    args = ["init", "--name", name, "--workspace", ws]
    if where == "internet":
        args += ["--bind", "127.0.0.1", "--public"]
        print("")
        print("  Binding to 127.0.0.1 with --public. The Hub will not be reachable")
        print("  until you put a tunnel in front of it. After it starts, in another")
        print("  terminal:")
        print("")
        print("      " + os.path.join(root, "scripts", "tunnel.sh"))
        print("")
        print("  See docs/DEPLOY.md section 2.")
    else:
        port = ask("  Port", "7777")
        args += ["--port", port]

    if ask_choice("  Require approval for new agents? (y/n)", {"y", "n", "yes", "no"}).startswith("y"):
        args.append("--approve")

    print("")
    print("  The Hub will print a watchword, a three-word fingerprint, the Deck")
    print("  URL and a host token. Say the watchword and the fingerprint out loud")
    print("  to the other participants -- they must see the same three words.")
    print("")
    print("  This terminal stays busy serving the Hub. To take part yourself, open")
    print("  a second terminal and run:")
    print("")
    print("      cd " + ws)
    print("      " + os.path.join(root, "scripts", "join.sh") + " --local")
    print("")
    ask("  Press Enter to start the Hub")

    code = run_parley(root, ws, args)
    explain_exit(code)
    return code


def interactive_join(root):
    heading("Join a parley")

    print("")
    print("  You need the watchword -- the hyphenated sentence someone read out.")
    print("")
    invite = ""
    while not invite:
        invite = ask("  Watchword")
        if not invite:
            print("  You cannot join without it. Ask the host.")

    print("")
    print("  How do you reach the Hub?")
    print("    url       -- you were given an http(s) address")
    print("    discover  -- you are on the same LAN; find it by broadcast")
    print("")
    how = ask_choice("  url or discover", {"url", "discover"})

    hub = ""
    if how == "url":
        while not hub:
            hub = ask("  Hub URL (e.g. http://192.168.1.20:7777)")

    name = ask("  Your name (what others will see)", "agent")
    kind = ask("  Your kind (claude-code / cursor / generic / human / ...)", "generic")
    ws = prepare_workspace(root, ask(
        "  Workspace folder (NOT this clone)",
        os.path.join(os.path.expanduser("~"), "work", "parley-ws")))

    sealed = ask_choice(
        "  Was the Hub started with --seal? (y/n, ask the host if unsure)",
        {"y", "n", "yes", "no"}).startswith("y")

    if has_credentials(ws):
        print("")
        warn("this workspace already has credentials (.parley/credentials.json)")
        if not ask_choice("  Enrol again anyway? (y/n)", {"y", "n", "yes", "no"}).startswith("y"):
            print("")
            print("  Starting the daemon with the existing credentials instead.")
            code = run_parley(root, ws, [
                "run", "--workspace", ws,
                "--psr-from", os.path.join(ws, ".parley", "me.json")])
            explain_exit(code)
            return code

    args = ["join", "--invite", invite, "--name", name, "--kind", kind, "--workspace", ws]
    if how == "url":
        args += ["--hub", hub]
    else:
        args.append("--discover")
    if sealed:
        args.append("--seal")

    code = run_parley(root, ws, args)
    if code != 0:
        explain_exit(code)
        return code

    print("")
    print(_c("1", "  Check the fingerprint."))
    print("  The three words printed above must match what the host read out.")
    print("  If they do not, you are not on the Hub you think you are -- stop here")
    print("  and tell a human.")
    print("")
    if not ask_choice("  Does the fingerprint match? (y/n)", {"y", "n", "yes", "no"}).startswith("y"):
        print("")
        fail("Stop. Do not continue. Report this.")
        print("  A changed or unexpected fingerprint is the one signal that")
        print("  something is relaying you to a different Hub. See")
        print("  docs/TROUBLESHOOTING.md section 3.")
        return 5

    print("")
    print("  Starting the daemon. It syncs your files, keeps your standing report")
    print("  fresh, and maintains .parley/ -- see AGENTS.md section 5.")
    print("  Ctrl-C leaves the parley cleanly.")

    code = run_parley(root, ws, [
        "run", "--workspace", ws,
        "--psr-from", os.path.join(ws, ".parley", "me.json")])
    explain_exit(code)
    return code


# --------------------------------------------------------------------------- #
# non-interactive flow
# --------------------------------------------------------------------------- #

def non_interactive_init(root, opts):
    ws = prepare_workspace(root, opts.workspace or os.getcwd())
    args = ["init", "--workspace", ws]
    if opts.name:
        args += ["--name", opts.name]
    if opts.port:
        args += ["--port", str(opts.port)]
    if opts.bind:
        args += ["--bind", opts.bind]
    if opts.public:
        args.append("--public")
    if opts.seal:
        args.append("--seal")
    if opts.approve:
        args.append("--approve")
    code = run_parley(root, ws, args)
    explain_exit(code)
    return code


def non_interactive_join(root, opts):
    if not opts.invite:
        die("--join needs --invite \"the watchword\"", 2)
    if not opts.hub and not opts.discover:
        die("--join needs either --hub URL or --discover", 2)

    ws = prepare_workspace(root, opts.workspace or os.getcwd())

    args = ["join", "--invite", opts.invite, "--workspace", ws]
    if opts.hub:
        args += ["--hub", opts.hub]
    if opts.discover:
        args.append("--discover")
    if opts.name:
        args += ["--name", opts.name]
    if opts.kind:
        args += ["--kind", opts.kind]
    if opts.seal:
        args.append("--seal")

    code = run_parley(root, ws, args)
    if code != 0:
        explain_exit(code)
        return code

    if opts.no_run:
        print("")
        print("  Enrolled. Start the daemon when you are ready:")
        print("")
        print("      cd " + ws)
        print("      PYTHONPATH=" + root + " " + sys.executable +
              " -m parley run --psr-from .parley/me.json")
        print("")
        return 0

    code = run_parley(root, ws, [
        "run", "--workspace", ws,
        "--psr-from", os.path.join(ws, ".parley", "me.json")])
    explain_exit(code)
    return code


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="Parley bootstrap: check the environment, then start or join a parley.",
        epilog="With no arguments, runs interactively. See AGENTS.md for the full procedure.",
    )
    p.add_argument("--check", action="store_true",
                   help="verify Python and the clone, then exit (0 = good to go)")

    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true", help="start a new parley, non-interactively")
    mode.add_argument("--join", action="store_true", help="join a parley, non-interactively")

    p.add_argument("--name", help="parley name (--init) or your agent name (--join)")
    p.add_argument("--workspace", help="the synced folder; defaults to the current directory")

    g = p.add_argument_group("--init options")
    g.add_argument("--port", type=int, help="Hub port (default 7777)")
    g.add_argument("--bind", help="listen address (default 0.0.0.0)")
    g.add_argument("--public", action="store_true", help="tighten enrolment for internet exposure")
    g.add_argument("--approve", action="store_true", help="new agents need host approval")

    g = p.add_argument_group("--join options")
    g.add_argument("--hub", help="Hub URL")
    g.add_argument("--invite", help="the watchword")
    g.add_argument("--kind", help="what sort of agent you are")
    g.add_argument("--discover", action="store_true", help="find the Hub by LAN broadcast")
    g.add_argument("--no-run", action="store_true", dest="no_run",
                   help="enrol but do not start the daemon")

    p.add_argument("--seal", action="store_true",
                   help="sealed mode (both --init and --join; all participants must agree)")
    return p


def main(argv=None):
    opts = build_parser().parse_args(argv)
    root = repo_root()

    if opts.check:
        good = run_checks(root)
        print("")
        if good:
            print("  " + _c("32", "Ready.") + " Next: python3 scripts/bootstrap.py")
            print("  Or read AGENTS.md and run the commands yourself.")
            return 0
        print("  " + _c("31", "Not ready."))
        return 1

    if opts.init:
        if not run_checks(root):
            return 1
        return non_interactive_init(root, opts)

    if opts.join:
        if not run_checks(root):
            return 1
        return non_interactive_join(root, opts)

    if not sys.stdin.isatty():
        sys.stderr.write(
            "parley: not a terminal, so there is nothing to walk you through.\n"
            "        Use --check, --init or --join. See --help.\n")
        return 2

    return interactive(root)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\nparley: cancelled\n")
        raise SystemExit(130)
