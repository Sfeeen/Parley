#!/usr/bin/env bash
#
# tunnel.sh -- expose a local Parley Hub over the internet with TLS.
#
# Detects whichever of cloudflared / ngrok / tailscale is installed, starts the
# tunnel, waits for the public URL, and prints the exact `parley join` line to
# send to the other participants.
#
# Usage:
#   ./scripts/tunnel.sh                       # auto-detect, port 7777
#   ./scripts/tunnel.sh --port 8080
#   ./scripts/tunnel.sh --provider ngrok
#   ./scripts/tunnel.sh --invite "copper-otter-climbs-the-quiet-hill"
#
# Options:
#   --port N          Local Hub port. Default 7777, or $PARLEY_PORT.
#   --provider NAME   cloudflared | ngrok | tailscale. Default: first one found.
#   --invite WORDS    Include the watchword in the printed join line. Optional --
#                     the watchword is a secret; pass it only if you are going to
#                     copy the line straight into a message to the other party.
#   --name NAME       Tunnel name for cloudflared named tunnels. Default "parley".
#
# Full write-ups of each provider, including the SSE proxy settings that break
# streaming, are in docs/DEPLOY.md section 2.

set -euo pipefail

PORT="${PARLEY_PORT:-7777}"
PROVIDER=""
INVITE=""
TUNNEL_NAME="parley"
LOGFILE=""

cleanup() {
    local rc=$?
    if [ -n "${TUNNEL_PID:-}" ] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
        printf '\nparley: stopping the tunnel...\n' >&2
        kill "$TUNNEL_PID" 2>/dev/null || true
        wait "$TUNNEL_PID" 2>/dev/null || true
    fi
    [ -n "$LOGFILE" ] && [ -f "$LOGFILE" ] && rm -f "$LOGFILE"
    exit $rc
}
trap cleanup EXIT INT TERM

die() { printf 'parley: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --port)     [ $# -ge 2 ] || die "--port needs a value";     PORT="$2"; shift 2 ;;
        --provider) [ $# -ge 2 ] || die "--provider needs a value"; PROVIDER="$2"; shift 2 ;;
        --invite)   [ $# -ge 2 ] || die "--invite needs a value";   INVITE="$2"; shift 2 ;;
        --name)     [ $# -ge 2 ] || die "--name needs a value";     TUNNEL_NAME="$2"; shift 2 ;;
        -h|--help)  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)          die "unknown option: $1  (try --help)" ;;
    esac
done

case "$PORT" in
    ''|*[!0-9]*) die "--port must be a number, got: $PORT" ;;
esac

# --- is the Hub actually up? --------------------------------------------------
hub_is_up() {
    if have curl; then
        curl -fsS --max-time 3 "http://127.0.0.1:$PORT/v1/hello" >/dev/null 2>&1
    elif have python3; then
        python3 - "$PORT" <<'PY' >/dev/null 2>&1
import sys, urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:%s/v1/hello" % sys.argv[1], timeout=3).read()
except Exception:
    sys.exit(1)
PY
    else
        return 0   # cannot check; assume the user knows
    fi
}

if ! hub_is_up; then
    cat >&2 <<EOF
parley: nothing is answering on http://127.0.0.1:$PORT/v1/hello

Start the Hub first, in another terminal, bound to localhost so the tunnel is the
only way in:

  cd <your workspace>
  ./scripts/start-hub.sh --name "my-parley" --bind 127.0.0.1 --port $PORT --public

Then run this script again. (Use --port N if your Hub is on a different port.)
EOF
    exit 1
fi

# --- pick a provider ----------------------------------------------------------
if [ -z "$PROVIDER" ]; then
    for p in cloudflared ngrok tailscale; do
        if have "$p"; then PROVIDER="$p"; break; fi
    done
fi

if [ -z "$PROVIDER" ]; then
    cat >&2 <<'EOF'
parley: no tunnel tool found.

Parley needs TLS over the internet, which means one of these. Install any one --
cloudflared is the easiest if you have no account anywhere.

  cloudflared  (free, no account needed for a quick tunnel, handles SSE correctly)
    macOS            brew install cloudflared
    Debian/Ubuntu    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
                       | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
                     echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
                       https://pkg.cloudflare.com/cloudflared any main" \
                       | sudo tee /etc/apt/sources.list.d/cloudflared.list
                     sudo apt update && sudo apt install cloudflared
    Windows          winget install --id Cloudflare.cloudflared
    Any              https://github.com/cloudflare/cloudflared/releases

  ngrok  (free account required; random hostname; good request inspector)
    macOS            brew install ngrok
    Linux            snap install ngrok
    Windows          winget install ngrok.ngrok
    then             ngrok config add-authtoken <token from dashboard>

  tailscale  (best when every participant is a machine you administer -- it is a
              private network, not a public endpoint)
    All platforms    https://tailscale.com/download
    then             sudo tailscale up

If every participant is on the same LAN you do not need any of this: plain HTTP
is authenticated and replay-protected. See docs/DEPLOY.md section 1.

If you cannot install any of them and must cross the internet anyway, restart the
Hub with --seal. It is slower and protects less; docs/DEPLOY.md section 3 is
honest about exactly what you lose.
EOF
    exit 1
fi

have "$PROVIDER" || die "--provider $PROVIDER requested but '$PROVIDER' is not installed"

printf 'parley: using %s -> http://127.0.0.1:%s\n\n' "$PROVIDER" "$PORT" >&2

# --- print the result ---------------------------------------------------------
announce() {
    local url="$1"
    cat <<EOF

================================================================================
  Parley Hub is public at:

      $url

  Send the other participants this line:

EOF
    if [ -n "$INVITE" ]; then
        cat <<EOF
      parley join --hub $url --invite "$INVITE" --name YOUR_NAME

EOF
    else
        cat <<EOF
      parley join --hub $url --invite "YOUR-WATCHWORD-HERE" --name YOUR_NAME

  (Re-run with --invite "your watchword" to have it filled in. The watchword is
   a secret -- say it out loud or send it over a channel you trust.)

EOF
    fi
    cat <<EOF
  Also tell them the three-word fingerprint the Hub printed at startup. They must
  see the same three words, or they are not on your Hub.

  The Deck:  $url/   (needs the ?vt= viewer token from your Deck URL)

  Leave this terminal open. Ctrl-C closes the tunnel.
================================================================================

EOF
}

LOGFILE="$(mktemp "${TMPDIR:-/tmp}/parley-tunnel.XXXXXX")"

# Poll the tunnel's own output for the public URL rather than guessing a delay.
wait_for_url() {
    local pattern="$1" tries=0 url=""
    while [ "$tries" -lt 120 ]; do
        if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
            printf 'parley: the tunnel exited. Its output:\n\n' >&2
            cat "$LOGFILE" >&2
            return 1
        fi
        url="$(grep -Eo "$pattern" "$LOGFILE" 2>/dev/null | head -n1 || true)"
        if [ -n "$url" ]; then printf '%s' "$url"; return 0; fi
        sleep 0.5
        tries=$((tries + 1))
    done
    printf 'parley: timed out waiting for the public URL. Tunnel output:\n\n' >&2
    cat "$LOGFILE" >&2
    return 1
}

case "$PROVIDER" in

    cloudflared)
        if cloudflared tunnel list 2>/dev/null | grep -qw "$TUNNEL_NAME"; then
            printf 'parley: found a named tunnel "%s"; running it.\n' "$TUNNEL_NAME" >&2
            printf 'parley: its hostname comes from your ~/.cloudflared/config.yml ingress rules.\n' >&2
            printf 'parley: see docs/DEPLOY.md section 2.1 for the originRequest timeouts SSE needs.\n\n' >&2
            exec cloudflared tunnel run "$TUNNEL_NAME"
        fi

        printf 'parley: starting a quick tunnel (ephemeral hostname, no account needed).\n' >&2
        printf 'parley: for a stable hostname see docs/DEPLOY.md section 2.1.\n\n' >&2
        cloudflared tunnel --url "http://127.0.0.1:$PORT" >"$LOGFILE" 2>&1 &
        TUNNEL_PID=$!
        URL="$(wait_for_url 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com')" || exit 1
        announce "$URL"
        wait "$TUNNEL_PID"
        ;;

    ngrok)
        if ! ngrok config check >/dev/null 2>&1; then
            cat >&2 <<'EOF'
parley: ngrok has no authtoken configured.

  1. Sign up (free) at https://dashboard.ngrok.com/signup
  2. ngrok config add-authtoken <your token>

EOF
            exit 1
        fi

        printf 'parley: starting ngrok.\n' >&2
        printf 'parley: on the free tier the Deck hits an interstitial warning page in a browser.\n' >&2
        printf 'parley: docs/DEPLOY.md section 2.2 shows the traffic policy that suppresses it.\n\n' >&2
        ngrok http "$PORT" --log stdout --log-format logfmt >"$LOGFILE" 2>&1 &
        TUNNEL_PID=$!
        URL="$(wait_for_url 'https://[a-zA-Z0-9.-]+\.ngrok[a-z.-]*\.(app|io|dev)')" || exit 1
        announce "$URL"
        printf 'parley: request inspector at http://127.0.0.1:4040 -- useful for debugging signatures.\n\n' >&2
        wait "$TUNNEL_PID"
        ;;

    tailscale)
        if ! tailscale status >/dev/null 2>&1; then
            printf 'parley: tailscale is installed but not connected. Run: sudo tailscale up\n' >&2
            exit 1
        fi

        TS_HOST=""
        if have python3; then
            TS_HOST="$(tailscale status --json 2>/dev/null \
                | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
        fi
        TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"

        cat >&2 <<EOF
parley: tailscale is connected.

Tailscale is a private network, not a public endpoint. Nothing is exposed to the
internet, and WireGuard already gives you confidentiality -- so plain HTTP inside
the tailnet is fine, and you do NOT want --public (it disables LAN discovery and
tightens enrolment for no benefit here).

Two ways to use it:

EOF
        if [ -n "$TS_IP" ]; then
            cat >&2 <<EOF
  A) Direct, no TLS needed. Restart the Hub bound to your tailnet address:

       ./scripts/start-hub.sh --name "my-parley" --bind $TS_IP --port $PORT

     Others join with:

       parley join --hub http://$TS_IP:$PORT --invite "YOUR-WATCHWORD" --name YOUR_NAME

EOF
        fi
        if [ -n "$TS_HOST" ]; then
            cat >&2 <<EOF
  B) With a real TLS hostname, proxied by tailscale (handles SSE correctly, no
     configuration needed). This script can do it for you -- starting now:

       tailscale serve --bg --https=443 http://127.0.0.1:$PORT

EOF
            if tailscale serve --bg --https=443 "http://127.0.0.1:$PORT" >"$LOGFILE" 2>&1; then
                announce "https://$TS_HOST"
                printf 'parley: `tailscale serve` runs in the background. To stop it:\n' >&2
                printf '  tailscale serve --https=443 off\n\n' >&2
                printf 'parley: to share outside your tailnet, use `tailscale funnel` -- and then\n' >&2
                printf 'parley: restart the Hub with --public, because it becomes a public endpoint.\n' >&2
                exit 0
            else
                printf 'parley: `tailscale serve` failed:\n\n' >&2
                cat "$LOGFILE" >&2
                printf '\nparley: use option A above instead.\n' >&2
                exit 1
            fi
        fi

        [ -n "$TS_IP" ] || die "could not determine a tailnet address; is tailscale up?"
        exit 0
        ;;

    *)
        die "unknown provider: $PROVIDER  (cloudflared | ngrok | tailscale)"
        ;;
esac
