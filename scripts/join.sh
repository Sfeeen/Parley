#!/usr/bin/env bash
#
# join.sh -- join a parley and start the local daemon.
#
# Two phases:
#   1. `parley join`  -- enrol, write <workspace>/.parley/credentials.json
#   2. `parley run`   -- the daemon: sync + heartbeat + PSR freshness + pigeonhole
#
# Usage:
#   ./scripts/join.sh --hub http://192.168.1.20:7777 --invite "copper-otter-..." --name Bram
#   ./scripts/join.sh --discover --invite "copper-otter-..." --name Bram
#   ./scripts/join.sh --local                 # already enrolled (e.g. you are the host): daemon only
#   ./scripts/join.sh --no-run --hub ... --invite ...   # enrol, do not start the daemon
#
# Any flag other than --local / --no-run is passed straight through to `parley join`.
# See docs/QUICKSTART.md and AGENTS.md.

set -euo pipefail

MIN_PY_MAJOR=3
MIN_PY_MINOR=9

_src="${BASH_SOURCE[0]}"
while [ -L "$_src" ]; do
    _dir="$(cd -P "$(dirname "$_src")" && pwd)"
    _src="$(readlink "$_src")"
    [[ "$_src" != /* ]] && _src="$_dir/$_src"
done
SCRIPT_DIR="$(cd -P "$(dirname "$_src")" && pwd)"
REPO_ROOT="$(cd -P "$SCRIPT_DIR/.." && pwd)"

die() { printf 'parley: %s\n' "$*" >&2; exit 1; }

find_python() {
    local candidate
    for candidate in "${PARLEY_PYTHON:-}" python3 python; do
        [ -n "$candidate" ] || continue
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($MIN_PY_MAJOR, $MIN_PY_MINOR) else 1)" 2>/dev/null; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    if command -v py >/dev/null 2>&1; then
        if py -3 -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($MIN_PY_MAJOR, $MIN_PY_MINOR) else 1)" 2>/dev/null; then
            printf '%s' "py -3"
            return 0
        fi
    fi
    return 1
}

if ! PYTHON="$(find_python)"; then
    cat >&2 <<EOF
parley: no usable Python found.

Parley needs Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR} or newer. It has no other dependencies --
there is nothing to pip install.

Tried: \$PARLEY_PYTHON, python3, python, py -3.

Install Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+ and try again:
  Debian/Ubuntu   sudo apt install python3
  Fedora/RHEL     sudo dnf install python3
  macOS           brew install python@3.12   (or use the python.org installer)
  Windows         winget install Python.Python.3.12

Or point PARLEY_PYTHON at a specific interpreter:
  PARLEY_PYTHON=/usr/local/bin/python3.11 $0 ...
EOF
    exit 1
fi

[ -d "$REPO_ROOT/parley" ] || die "this does not look like a Parley clone: $REPO_ROOT/parley is missing"

# --- split our own flags out of the pass-through arguments --------------------
LOCAL_ONLY=0
RUN_AFTER=1
JOIN_ARGS=()
WORKSPACE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --local)   LOCAL_ONLY=1; shift ;;
        --no-run)  RUN_AFTER=0; shift ;;
        --workspace)
            [ $# -ge 2 ] || die "--workspace needs a directory"
            WORKSPACE="$2"
            JOIN_ARGS+=("$1" "$2")
            shift 2
            ;;
        --workspace=*)
            WORKSPACE="${1#--workspace=}"
            JOIN_ARGS+=("$1")
            shift
            ;;
        -h|--help)
            sed -n '2,20p' "$_src" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            JOIN_ARGS+=("$1")
            shift
            ;;
    esac
done

[ -n "$WORKSPACE" ] || WORKSPACE="$PWD"
mkdir -p "$WORKSPACE" || die "cannot create workspace: $WORKSPACE"

if [ -e "$WORKSPACE/parley/hub" ] && [ -e "$WORKSPACE/docs/SPEC.md" ]; then
    cat >&2 <<EOF
parley: WARNING -- the workspace looks like the Parley clone itself.

  workspace: $WORKSPACE

Everything in the workspace is replicated to every participant. Use a separate
directory for the work you are actually collaborating on.

Continuing in 5 seconds; Ctrl-C to abort.
EOF
    sleep 5
fi

export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

# --- phase 1: enrol -----------------------------------------------------------
if [ "$LOCAL_ONLY" -eq 0 ]; then
    if [ ${#JOIN_ARGS[@]} -eq 0 ]; then
        cat >&2 <<EOF
parley: nothing to join.

You need an invite (the watchword) and a way to reach the Hub:

  $0 --hub http://192.168.1.20:7777 --invite "copper-otter-climbs-the-quiet-hill" --name Bram

On the same LAN you can skip the URL and let the client find the Hub:

  $0 --discover --invite "copper-otter-climbs-the-quiet-hill" --name Bram

If you are already enrolled (for example you are the host), start the daemon only:

  $0 --local
EOF
        exit 2
    fi

    printf 'parley: enrolling...\n' >&2
    set +e
    $PYTHON -m parley join "${JOIN_ARGS[@]}"
    rc=$?
    set -e
    if [ "$rc" -ne 0 ]; then
        case "$rc" in
            3) printf 'parley: enrolment failed (auth). Check the watchword.\n' >&2 ;;
            4) printf 'parley: cannot reach the Hub. Check the URL and the firewall; see docs/TROUBLESHOOTING.md section 1.\n' >&2 ;;
            5) printf 'parley: FINGERPRINT MISMATCH. You reached a different Hub than the one you were invited to. Stop and tell a human.\n' >&2 ;;
        esac
        exit "$rc"
    fi

    printf '\nparley: compare the three-word fingerprint above with what the host read out.\n' >&2
    printf 'parley: if it does not match, stop now.\n\n' >&2
fi

# --- phase 2: the daemon ------------------------------------------------------
if [ "$RUN_AFTER" -eq 0 ]; then
    printf 'parley: enrolled. Start the daemon when you are ready:\n' >&2
    printf '  cd %s && PYTHONPATH=%s %s -m parley run\n' "$WORKSPACE" "$REPO_ROOT" "$PYTHON" >&2
    exit 0
fi

RUN_ARGS=(run)
[ -n "$WORKSPACE" ] && RUN_ARGS+=(--workspace "$WORKSPACE")
# The daemon re-emits the PSR written to .parley/me.json, which is how an agent
# satisfies the freshness contract without a timer in its own loop (SPEC 6.1, 10).
RUN_ARGS+=(--psr-from "$WORKSPACE/.parley/me.json")

printf 'parley: starting the daemon (Ctrl-C to leave the parley cleanly).\n' >&2
exec $PYTHON -m parley "${RUN_ARGS[@]}"
