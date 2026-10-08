#!/usr/bin/env bash
#
# start-hub.sh -- start a new parley, hosting the Hub.
#
# Thin, robust wrapper around `python3 -m parley init`. It finds a usable Python 3,
# makes the clone importable without an install step, and runs the Hub with the
# current directory (or --workspace) as the synced workspace.
#
# Every argument is passed straight through to `parley init`, so:
#
#   ./scripts/start-hub.sh --name "my-parley"
#   ./scripts/start-hub.sh --name "my-parley" --bind 127.0.0.1 --public --seal
#   ./scripts/start-hub.sh --workspace ~/work/ws --port 8080
#
# See docs/DEPLOY.md for the flags and what they mean.

set -euo pipefail

MIN_PY_MAJOR=3
MIN_PY_MINOR=9

# --- locate the repository, independently of where we were invoked from -------
# Resolve symlinks so `ln -s .../start-hub.sh ~/bin/parley-hub` still works.
_src="${BASH_SOURCE[0]}"
while [ -L "$_src" ]; do
    _dir="$(cd -P "$(dirname "$_src")" && pwd)"
    _src="$(readlink "$_src")"
    [[ "$_src" != /* ]] && _src="$_dir/$_src"
done
SCRIPT_DIR="$(cd -P "$(dirname "$_src")" && pwd)"
REPO_ROOT="$(cd -P "$SCRIPT_DIR/.." && pwd)"

die() { printf 'parley: %s\n' "$*" >&2; exit 1; }

# --- find a usable Python 3 ---------------------------------------------------
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
    # Windows launcher, available under Git Bash / MSYS.
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

# --- sanity-check the clone ---------------------------------------------------
[ -d "$REPO_ROOT/parley" ] || die "this does not look like a Parley clone: $REPO_ROOT/parley is missing"

# --- warn about the workspace == clone mistake --------------------------------
# The workspace is replicated to every participant. Syncing the clone itself is
# almost never what anyone wants.
workspace="$PWD"
_want_ws=0
for arg in "$@"; do
    if [ "$_want_ws" -eq 1 ]; then
        workspace="$arg"
        _want_ws=0
        continue
    fi
    case "$arg" in
        --workspace)   _want_ws=1 ;;
        --workspace=*) workspace="${arg#--workspace=}" ;;
    esac
done
if [ -e "$workspace/parley/hub" ] && [ -e "$workspace/docs/SPEC.md" ]; then
    cat >&2 <<EOF
parley: WARNING -- the workspace looks like the Parley clone itself.

  workspace: $workspace

Everything in the workspace is replicated to every participant. Syncing Parley's
own source code is almost certainly not what you want.

Use a separate directory:
  mkdir -p ~/work/parley-ws && cd ~/work/parley-ws
  $0 $*

Continuing in 5 seconds; Ctrl-C to abort.
EOF
    sleep 5
fi

export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

exec $PYTHON -m parley init "$@"
