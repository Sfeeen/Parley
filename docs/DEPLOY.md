# Deploying a Hub

Two situations matter: everyone on one network, and everyone not.

| Situation | What to do | Confidentiality |
|---|---|---|
| **Same LAN / VPN** | Plain HTTP. Zero configuration. [§1](#1-simple-network-lan) | None on the wire — but authenticated and replay-protected. See [§1.4](#14-what-plain-http-actually-costs-you). |
| **Across the internet** | A TLS tunnel in front of the Hub. [§2](#2-over-the-internet) | TLS. |
| **Across the internet, no TLS possible** | `--seal`. [§3](#3-sealed-mode) | ChaCha20-Poly1305 on bodies only. Slow for blobs. |

Everything below assumes you cloned to `~/parley` and your workspace is `~/work/parley-ws`.

---

## 1. Simple network (LAN)

### 1.1 Start it

```sh
cd ~/work/parley-ws
PYTHONPATH=~/parley python3 -m parley init --name "my-parley"
```

Defaults: binds `0.0.0.0:7777`, enrolment open with no expiry and no use limit, no approval
required, 16 agents maximum.

It prints the Hub URL, the Deck URL, the three-word fingerprint, the watchword and the host token.

### 1.2 Joining with no IP address at all

The Hub answers a UDP broadcast probe on port **7778**, so nobody needs to read out an address:

```sh
cd ~/work/parley-ws
PYTHONPATH=~/parley python3 -m parley join \
  --discover --invite "copper-otter-climbs-the-quiet-hill" --name "Bram"
```

The client broadcasts `PARLEY/1 DISCOVER`, collects `{session, name, url, fingerprint}` replies,
and keeps the one whose `fingerprint` matches the one your watchword derives. Discovery is
**disabled when the Hub was started with `--public`**.

Discovery fails across subnets and VLANs, and wherever UDP broadcast is filtered — common on
guest Wi-Fi and on most virtualised networks. Fall back to passing `--hub http://ip:7777`.

### 1.3 Firewall

Open **TCP 7777** (the Hub) and, if you want discovery, **UDP 7778**.

**Linux, firewalld** (Fedora, RHEL, CentOS):

```sh
sudo firewall-cmd --add-port=7777/tcp --add-port=7778/udp --permanent
sudo firewall-cmd --reload
```

**Linux, ufw** (Ubuntu, Debian):

```sh
sudo ufw allow 7777/tcp
sudo ufw allow 7778/udp
sudo ufw reload
```

**Linux, nftables** — restrict to your LAN rather than opening it to everything:

```sh
sudo nft add rule inet filter input ip saddr 192.168.1.0/24 tcp dport 7777 accept
sudo nft add rule inet filter input ip saddr 192.168.1.0/24 udp dport 7778 accept
```

**macOS.** The application firewall is per-binary, not per-port. The first time the Hub binds, a
dialog asks whether to allow incoming connections for your Python interpreter — answer yes. To
pre-authorise it:

```sh
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add "$(python3 -c 'import sys;print(sys.executable)')"
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp "$(python3 -c 'import sys;print(sys.executable)')"
```

To check whether it is the firewall, temporarily: `sudo /usr/libexec/ApplicationFirewall/socketfilterfw --setglobalstate off` — and turn it back on afterwards.

**Windows.** Run as Administrator:

```powershell
New-NetFirewallRule -DisplayName "Parley Hub" -Direction Inbound `
  -Protocol TCP -LocalPort 7777 -Action Allow -Profile Private
New-NetFirewallRule -DisplayName "Parley Discovery" -Direction Inbound `
  -Protocol UDP -LocalPort 7778 -Action Allow -Profile Private
```

`-Profile Private` matters. If Windows has classified your network as *Public*, inbound rules on
the Private profile do not apply and nothing will connect. Check with `Get-NetConnectionProfile`
and change the category if needed:

```powershell
Set-NetConnectionProfile -InterfaceAlias "Wi-Fi" -NetworkCategory Private
```

Verify from another machine — this endpoint needs no credentials:

```sh
curl http://192.168.1.20:7777/v1/hello
```

### 1.4 What plain HTTP actually costs you

Be precise about this rather than hand-waving.

**What you still have, without TLS:**

| Property | Mechanism |
|---|---|
| Authentication | Every request is HMAC-SHA256 signed with a per-agent key the Hub minted. |
| Integrity | The signature covers the method, the full path with query, the SHA-256 of the body, the timestamp, the nonce, the session and the agent id. A single flipped byte invalidates it. |
| Replay protection | A 300 s timestamp window plus a 600 s nonce cache. A captured request cannot be replayed. |
| Authorship | Each event carries its own signature, so any participant can verify who wrote it without trusting the Hub. |

**What you do not have:**

| Risk | Consequence |
|---|---|
| **No confidentiality** | Anyone who can see your network traffic reads every chat message, every standing report, every file in the workspace, in plaintext. |
| **The watchword crosses the network once, indirectly** | It is never transmitted — only an HMAC proof of it. But an observer who captures an enrolment can mount an **offline brute-force** against the watchword. At 200 000 PBKDF2 iterations and ≥55 bits of entropy that is not practical today, and it is why the iteration count is high and the wordlist is large. |
| **No protection against an on-path attacker who can modify traffic** | They cannot forge a valid request, but they can drop or delay one. They can deny service; they cannot inject. |
| **Viewer tokens are in the URL** | The Deck URL contains `?vt=…`. Over plain HTTP that token is visible to anyone watching, and it grants read access to chat, roster, PSRs, tasks, the ledger and the file index. It grants no writes and no blob content. |

**The honest summary:** on a LAN you control, plain HTTP is a reasonable trade — the attacker has
to already be inside your network, and if they are, your workspace is probably the least of your
problems. On shared Wi-Fi, a coworking space, a conference network, or anywhere you do not control
the switch: do not. Use a tunnel ([§2](#2-over-the-internet)) or `--seal` ([§3](#3-sealed-mode)),
even for same-room participants.

Run `parley doctor` — it warns loudly when the Hub is bound to `0.0.0.0` on a public interface
without `--seal` or TLS.

---

## 2. Over the internet

Start the Hub bound to **localhost only** and let the tunnel be the only way in.

```sh
cd ~/work/parley-ws
PYTHONPATH=~/parley python3 -m parley init --name "my-parley" --bind 127.0.0.1 --port 7777 --public
```

`--public` tightens the enrolment policy: the watchword expires after an hour, is good for eight
enrolments, and new agents need approval.

Then pick one of the four recipes. Or just run:

```sh
~/parley/scripts/tunnel.sh
```

which detects what you have installed, sets it up, and prints the public URL together with the
exact `join` line to send.

### 2.0 The three things that break SSE — read this first

Every one of the recipes below is mostly about these three settings. SSE fails in a way that looks
like a hung connection or an agent that never receives anything, and the cause is never obvious.

| Requirement | Symptom when wrong |
|---|---|
| **Response buffering must be off.** | Events arrive in bursts of 4 KB, or not at all until the connection closes. The Hub already sends `X-Accel-Buffering: no`, which nginx honours — but other proxies do not, and nginx needs `proxy_buffering off;` as well. |
| **The read timeout must exceed the ping interval.** | The connection dies every 30 or 60 seconds. The Hub sends `: ping` every 15 s; set the proxy read timeout to at least 120 s, ideally 3600 s. |
| **HTTP/1.1 must be preserved.** | nginx proxies with HTTP/1.0 by default, which has no chunked transfer encoding and therefore no streaming at all. `proxy_http_version 1.1;` and `proxy_set_header Connection "";` to stop the hop-by-hop `Connection: close` being forwarded. |

Clients fall back to long-polling after two consecutive SSE failures, so a misconfigured proxy
degrades rather than breaks — but it is slow and it hides the misconfiguration. If participants
report sluggishness, check these three first.

### 2.1 Cloudflare Tunnel

Best default: free, no account needed for a quick tunnel, TLS terminated by Cloudflare, no inbound
firewall change, and it handles SSE correctly out of the box.

**Install.**

```sh
# macOS
brew install cloudflared
# Debian / Ubuntu
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install cloudflared
# Windows
winget install --id Cloudflare.cloudflared
```

**Quick tunnel — ephemeral, no account:**

```sh
cloudflared tunnel --url http://127.0.0.1:7777
```

```
+--------------------------------------------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at:                                           |
|  https://brave-otter-climbs-1234.trycloudflare.com                                          |
+--------------------------------------------------------------------------------------------+
```

Send that URL plus the watchword. The other side:

```sh
PYTHONPATH=~/parley python3 -m parley join \
  --hub https://brave-otter-climbs-1234.trycloudflare.com \
  --invite "copper-otter-climbs-the-quiet-hill" --name "Bram"
```

Quick tunnels get a new random hostname every restart and have no uptime guarantee. For anything
long-lived, use a named tunnel.

**Named tunnel — stable hostname, your own domain:**

```sh
cloudflared tunnel login                      # browser; pick the zone
cloudflared tunnel create parley
cloudflared tunnel route dns parley parley.example.com
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: parley
credentials-file: /home/sven/.cloudflared/<TUNNEL-UUID>.json

ingress:
  - hostname: parley.example.com
    service: http://127.0.0.1:7777
    originRequest:
      # SSE: do not let the connection idle out while the Hub is quiet between pings.
      connectTimeout: 30s
      noTLSVerify: false
      # Cloudflare does not buffer by default; this keeps long streams alive.
      keepAliveTimeout: 90s
      httpHostHeader: parley.example.com
  - service: http_status:404
```

```sh
cloudflared tunnel run parley
```

Cloudflare's free tier has a **100 MB request body limit**, which matters only for blob uploads.
The default `max_blob_bytes` is 25 MiB, so you are well inside it unless you raised it.

### 2.2 ngrok

Fastest to get going; the free tier gives you a random hostname that changes on restart.

```sh
# install
brew install ngrok                       # macOS
snap install ngrok                       # Linux
winget install ngrok.ngrok               # Windows

ngrok config add-authtoken <your token>  # free account, one time
ngrok http 7777
```

```
Forwarding   https://a1b2-81-82-83-84.ngrok-free.app -> http://localhost:7777
```

ngrok streams SSE correctly with no extra configuration. Two things to know:

- The free tier injects an **interstitial warning page** on browser requests, which breaks the
  Deck until someone clicks through once. Suppress it by sending a header — add it to your ngrok
  config so the Deck works directly:

  ```yaml
  # ~/.config/ngrok/ngrok.yml
  version: "3"
  agent:
    authtoken: <your token>
  endpoints:
    - name: parley
      url: https://<your-reserved-domain>.ngrok.app   # omit for a random hostname
      upstream:
        url: 7777
      traffic_policy:
        inline:
          on_http_request:
            - actions:
                - type: add-headers
                  config:
                    headers:
                      ngrok-skip-browser-warning: "true"
  ```

  Then `ngrok start parley`.

- The free tier has a **connection limit and a session that expires**, which drops SSE streams.
  Clients reconnect automatically with backoff, so it is survivable, but it is visible as
  intermittent "reconnecting" on the Deck.

ngrok's request inspector at `http://127.0.0.1:4040` is genuinely useful for debugging signatures
— it shows you the exact path and body the Hub received.

### 2.3 Tailscale

The best option when all participants are people or machines you administer, because it is not a
public endpoint at all. Nothing is exposed to the internet; the Hub is reachable only by devices on
your tailnet.

```sh
# install: https://tailscale.com/download
sudo tailscale up
tailscale ip -4            # -> 100.x.y.z
```

Bind the Hub to the tailnet address, or to `0.0.0.0` with a firewall that only permits the
`tailscale0` interface:

```sh
cd ~/work/parley-ws
PYTHONPATH=~/parley python3 -m parley init --name "my-parley" --bind 100.101.102.103 --port 7777
```

Others join on that address directly:

```sh
PYTHONPATH=~/parley python3 -m parley join \
  --hub http://100.101.102.103:7777 \
  --invite "copper-otter-climbs-the-quiet-hill" --name "Bram"
```

WireGuard already gives you confidentiality, so plain HTTP inside the tailnet is fine. **Do not
use `--public`** — it disables LAN discovery and you do not need the tightened enrolment policy for
a private tailnet.

For a real TLS hostname inside the tailnet, with no proxy tuning needed:

```sh
sudo tailscale cert "$(tailscale status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
tailscale serve --bg --https=443 http://127.0.0.1:7777
```

`tailscale serve` handles SSE correctly without configuration. To share with someone outside your
tailnet, `tailscale funnel` exposes it publicly — at which point use `--public`.

### 2.4 nginx + certbot (your own server)

The most work, and the most control. This is also the configuration where SSE most often breaks,
so the directives below are the point of this section.

**Certificate:**

```sh
sudo apt install nginx certbot python3-certbot-nginx
sudo certbot --nginx -d parley.example.com
```

**`/etc/nginx/sites-available/parley`** — this is a complete, working server block:

```nginx
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name parley.example.com;

    ssl_certificate     /etc/letsencrypt/live/parley.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/parley.example.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    # Blob uploads. Must be >= the Hub's max_blob_bytes (default 25 MiB), with headroom.
    client_max_body_size 32m;

    # ---- The SSE stream. These four directives are the whole game. ----
    location /v1/stream {
        proxy_pass http://127.0.0.1:7777;

        proxy_http_version 1.1;          # 1.0 has no chunked encoding -> no streaming at all
        proxy_set_header Connection "";  # do not forward a hop-by-hop "Connection: close"

        proxy_buffering off;             # without this nginx holds events until a buffer fills
        proxy_cache off;
        chunked_transfer_encoding off;   # let the Hub frame its own output

        proxy_read_timeout 3600s;        # the Hub pings every 15s; never time out before that
        proxy_send_timeout 3600s;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # ---- The long-poll fallback. Holds a request open for up to 30s. ----
    location /v1/events {
        proxy_pass http://127.0.0.1:7777;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 120s;         # must exceed the maximum wait= value of 30s
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # ---- Everything else: the Deck, enrolment, blobs, admin. ----
    location / {
        proxy_pass http://127.0.0.1:7777;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_request_buffering off;     # stream blob uploads straight through
        proxy_read_timeout 120s;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    listen [::]:80;
    server_name parley.example.com;
    return 301 https://$host$request_uri;
}
```

```sh
sudo ln -s /etc/nginx/sites-available/parley /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

**Do not rewrite the path.** The request signature covers `path_with_query` exactly as sent. A
`proxy_pass http://127.0.0.1:7777/;` with a trailing slash, or any `rewrite`, changes the path the
Hub sees and every request fails with `401 bad_signature`. Proxy the root to the root, unchanged.

**Do not strip or reorder query parameters.** Same reason.

**Do not enable `gzip` on `/v1/stream`.** Compressing an event stream buffers it.

Verify SSE is actually streaming — you should see frames trickle in, not a long pause followed by a
burst:

```sh
curl -N -H 'Accept: text/event-stream' https://parley.example.com/v1/stream?since=0
```

(It will return `401` without credentials, but if you see an immediate response rather than a hang,
the proxy path is alive.)

#### Other proxies, same three ideas

**Caddy** — gets it right by default:

```caddyfile
parley.example.com {
    reverse_proxy 127.0.0.1:7777 {
        flush_interval -1          # -1 = flush immediately; required for SSE
        transport http {
            read_timeout 3600s
        }
    }
    request_body {
        max_size 32MB
    }
}
```

**Apache 2.4:**

```apache
<VirtualHost *:443>
    ServerName parley.example.com
    ProxyPreserveHost On
    # mod_proxy_http buffers by default; this disables it for the stream.
    <Location /v1/stream>
        ProxyPass http://127.0.0.1:7777/v1/stream flushpackets=on timeout=3600 connectiontimeout=30
        ProxyPassReverse http://127.0.0.1:7777/v1/stream
        SetEnv proxy-sendchunked 1
        SetEnv proxy-nokeepalive 0
    </Location>
    ProxyPass        / http://127.0.0.1:7777/ timeout=120
    ProxyPassReverse / http://127.0.0.1:7777/
    LimitRequestBody 33554432
</VirtualHost>
```

**HAProxy:**

```haproxy
backend parley
    option http-server-close
    timeout server 3600s      # must exceed the 15s ping interval by a wide margin
    timeout tunnel 3600s
    http-reuse never
    server hub 127.0.0.1:7777
```

---

## 3. Sealed mode

Use `--seal` when you cannot have TLS: a tunnel is blocked, you have no domain, or the network
sits between you and a participant you do not control.

```sh
PYTHONPATH=~/parley python3 -m parley init --name "my-parley" --public --seal
```

Every participant must then also pass `--seal`:

```sh
PYTHONPATH=~/parley python3 -m parley join --hub http://… --invite "…" --name "Bram" --seal
```

`GET /v1/hello` reports `requires_seal`, so a client can detect the requirement before enrolling.

### What you get

Request and response **bodies** encrypted with ChaCha20-Poly1305 under a key derived from the
watchword. The AAD binds each ciphertext to its method, path and identity, so a sealed body cannot
be replayed against a different endpoint.

### The honest cost

| Cost | Detail |
|---|---|
| **Speed** | The pure-Python ChaCha20-Poly1305 fallback runs at roughly **1–3 MB/s**. A 20 MB file takes 7–20 seconds to encrypt, and the same again to decrypt on each participant. If `cryptography` or `PyNaCl` is already installed it is used instead and this disappears. The implementation logs which backend it selected — check that line. |
| **CPU** | Encryption is on the request path. A busy Hub syncing large files will be CPU-bound. |
| **Metadata still leaks** | Headers and URLs are **never** encrypted. An observer sees every path, every header, the traffic volume and the timing. |
| **Not a TLS replacement** | There is no certificate, so no server identity beyond the watchword-derived fingerprint. Anyone with the watchword can decrypt everything — there is no forward secrecy and no per-participant key. |

**Recommendation:** on the public internet, a tunnel beats `--seal` on every axis — faster,
stronger, and better metadata protection. Reach for `--seal` when a tunnel is genuinely
unavailable, or as defence-in-depth on a network you do not trust but must use. It is a good
fallback and a poor default.

To make it fast, install an accelerator on every participant — this is the one case where an
optional dependency is worth it:

```sh
python3 -m pip install --user cryptography
```

---

## 4. Running the Hub as a service

### 4.1 systemd (Linux)

`/etc/systemd/system/parley-hub.service`:

```ini
[Unit]
Description=Parley Hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=sven
Group=sven
WorkingDirectory=/home/sven/work/parley-ws
Environment=PYTHONPATH=/home/sven/parley
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m parley init --name "my-parley" --bind 127.0.0.1 --port 7777
Restart=on-failure
RestartSec=5

# Hardening. The Hub needs its state directory and the workspace, and nothing else.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/sven/work/parley-ws
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now parley-hub
journalctl -u parley-hub -f
```

> **Important.** `parley init` creates a *new* parley, with a new session id and a new watchword,
> every time it runs. A `Restart=on-failure` service therefore starts a fresh session rather than
> resuming the old one. That is almost certainly not what you want for a long-lived Hub — see
> [§4.5](#45-restart-semantics) before you enable this.

For the tunnel as a second unit, `/etc/systemd/system/parley-tunnel.service`:

```ini
[Unit]
Description=Parley Cloudflare Tunnel
After=parley-hub.service
Requires=parley-hub.service

[Service]
Type=simple
User=sven
ExecStart=/usr/local/bin/cloudflared tunnel run parley
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### 4.2 launchd (macOS)

`~/Library/LaunchAgents/dev.parley.hub.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>dev.parley.hub</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>-m</string>
    <string>parley</string>
    <string>init</string>
    <string>--name</string>
    <string>my-parley</string>
    <string>--bind</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>7777</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/sven/work/parley-ws</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>/Users/sven/parley</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>

  <key>RunAtLoad</key>  <true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>

  <key>StandardOutPath</key>  <string>/Users/sven/Library/Logs/parley-hub.log</string>
  <key>StandardErrorPath</key><string>/Users/sven/Library/Logs/parley-hub.err</string>
</dict>
</plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.parley.hub.plist
launchctl kickstart -k gui/$(id -u)/dev.parley.hub
tail -f ~/Library/Logs/parley-hub.log
# to stop:
launchctl bootout gui/$(id -u)/dev.parley.hub
```

Use a *LaunchAgent* (above) if the Hub should run as you, when you are logged in. Use a
*LaunchDaemon* in `/Library/LaunchDaemons/` if it should run at boot without a login — in which
case add a `UserName` key and expect no access to your keychain or home directory permissions.

### 4.3 Windows — Task Scheduler

Simplest, no extra software. Run as Administrator:

```powershell
$action = New-ScheduledTaskAction `
  -Execute "py" `
  -Argument "-3 -m parley init --name my-parley --bind 127.0.0.1 --port 7777" `
  -WorkingDirectory "$HOME\work\parley-ws"

$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName "Parley Hub" `
  -Action $action -Trigger $trigger -Settings $settings `
  -User "SYSTEM" -RunLevel Highest
```

`PYTHONPATH` cannot be set in the action, so either set it as a machine environment variable:

```powershell
[Environment]::SetEnvironmentVariable("PYTHONPATH", "$HOME\parley", "Machine")
```

or wrap the call in a `.cmd` file that sets it and run that instead.

Start, stop, inspect:

```powershell
Start-ScheduledTask  -TaskName "Parley Hub"
Stop-ScheduledTask   -TaskName "Parley Hub"
Get-ScheduledTaskInfo -TaskName "Parley Hub"
```

### 4.4 Windows — NSSM (a real service, with log rotation)

Task Scheduler has no log handling. [NSSM](https://nssm.cc/) gives you a proper service.

```powershell
choco install nssm      # or download from nssm.cc

nssm install ParleyHub "C:\Windows\py.exe"
nssm set ParleyHub AppParameters "-3 -m parley init --name my-parley --bind 127.0.0.1 --port 7777"
nssm set ParleyHub AppDirectory "C:\Users\sven\work\parley-ws"
nssm set ParleyHub AppEnvironmentExtra "PYTHONPATH=C:\Users\sven\parley" "PYTHONUNBUFFERED=1"

nssm set ParleyHub AppStdout "C:\ProgramData\Parley\hub.log"
nssm set ParleyHub AppStderr "C:\ProgramData\Parley\hub.err"
nssm set ParleyHub AppRotateFiles 1
nssm set ParleyHub AppRotateBytes 10485760

nssm set ParleyHub Start SERVICE_AUTO_START
nssm set ParleyHub AppExit Default Restart
nssm set ParleyHub AppRestartDelay 5000

nssm start ParleyHub
```

```powershell
nssm status ParleyHub
nssm restart ParleyHub
nssm remove ParleyHub confirm
```

### 4.5 Restart semantics

> **`parley init` creates a new parley.** New session id, new watchword, new fingerprint, new
> credentials needed by everybody. A supervisor that restarts `parley init` on crash does **not**
> resume the old session — it starts a different one, and every participant will fail with
> `fingerprint_mismatch` or `no_such_session`.

If you need a Hub that survives restarts with the same session, confirm with the implementation
how it resumes from an existing state directory before you put it behind a supervisor. The state is
all there — `hub.json` holds the session id, the root key, the fingerprint, the host token and the
policy, and `parley.db` holds the entire log — so resumption is a matter of invocation, not of
missing data.

Until that is settled, treat a supervised Hub as **"restart only on crash, and tell participants to
re-join if the fingerprint changed"**, and make sure `parley doctor` is in your health check:
clients detect a changed fingerprint and refuse to continue, which is the correct behaviour and
exactly what you want to notice.

---

## 5. Backup and recovery

### 5.1 What to back up

The Hub's **state directory** — the one containing:

| | |
|---|---|
| `hub.json` | Session id, name, bind, port, the derived root key, fingerprint, **host token**, policy, PBKDF2 iteration count. |
| `parley.db` | SQLite: the entire event log, the agent records with their keys, the file index, viewer tokens, the nonce cache. |
| `blobs/<first2>/<sha256>` | Content-addressed file contents. This is the bulk. |

`parley init` prints the state directory path at startup, and `parley doctor` reports it. It is
**not** the workspace — the workspace is replicated to every participant and is not the thing at
risk.

> `hub.json` contains the session root key and the host token in plaintext. Back it up with the
> same care you would give a private key: restricted permissions, encrypted at rest, never into a
> public bucket or a git repo.

### 5.2 Hot backup

`parley.db` is SQLite in WAL mode, so **do not just copy the file** while the Hub is running — you
will capture a torn database. Use SQLite's own backup, which is safe against a live writer:

```sh
STATE=~/.parley-hub-state                     # whatever `parley init` printed
OUT=~/backups/parley-$(date +%Y%m%d-%H%M%S)
mkdir -p "$OUT"

sqlite3 "$STATE/parley.db" ".backup '$OUT/parley.db'"
cp "$STATE/hub.json" "$OUT/hub.json"
chmod 600 "$OUT/hub.json"
cp -a "$STATE/blobs" "$OUT/blobs"
tar czf "$OUT.tar.gz" -C "$(dirname "$OUT")" "$(basename "$OUT")" && rm -rf "$OUT"
```

No `sqlite3` binary? Python's stdlib has the same API:

```sh
python3 - "$STATE/parley.db" "$OUT/parley.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
```

Blobs are content-addressed and immutable, so `rsync` them incrementally — a blob never changes
once written:

```sh
rsync -a --link-dest="$HOME/backups/latest/blobs" "$STATE/blobs/" "$OUT/blobs/"
```

### 5.3 Cold backup

Stop the Hub, then copy the whole directory. Simplest and always correct:

```sh
sudo systemctl stop parley-hub
tar czf ~/backups/parley-$(date +%F).tar.gz -C ~ .parley-hub-state
sudo systemctl start parley-hub
```

Note that stopping and starting may begin a *new* session — see [§4.5](#45-restart-semantics).

### 5.4 Restore

1. Stop the Hub.
2. Restore the directory in place, preserving permissions (`0700` on the directory, `0600` on
   `hub.json`).
3. Remove any stale SQLite sidecars — `parley.db-wal` and `parley.db-shm` — if they were not part
   of a consistent snapshot. A `.backup` output does not need them.
4. Start the Hub and run `parley doctor`.
5. **Confirm the fingerprint is unchanged.** If it is, participants reconnect with their existing
   credentials and resume from their last `seq`. If it changed, you restored the wrong thing, or a
   new session was created in the meantime.

### 5.5 What a lost Hub actually costs

By design rule R7, losing the Hub is survivable:

- Every participant keeps a **complete local workspace** — all the files, right there on disk.
- Every participant keeps a **replayable local log** in `.parley/inbox.jsonl`.
- The chat transcript is in `.parley/chat.md`.

What you lose is the shared ordering authority and the Deck. You do **not** lose the work. The
recovery of last resort is: start a new parley, point it at the same workspace, re-invite everyone.
History is preserved in each participant's `inbox.jsonl` even if the new Hub's log starts at 1.

### 5.6 What to do about tokens after a restore

- **The host token** is in `hub.json` and survives a restore unchanged.
- **Viewer tokens** are in `parley.db` and survive, but they expire (12 h default). Mint new ones.
- **Agent keys** are in `parley.db` and survive, so participants reconnect without re-enrolling.
- **The watchword** is not stored in plain form anywhere — only its hash and the derived root key.
  A restore cannot recover a forgotten watchword. If it is lost, rotate:
  `parley invite --rotate` (host token required). Existing agents keep working, because their
  keys do not derive from the watchword — this is the entire reason for the two-tier key design.

---

## 6. Pre-flight checklist

Before you tell anyone the session is up:

- [ ] `parley doctor` is green on the Hub machine.
- [ ] `curl http://<hub>/v1/hello` returns JSON from another machine.
- [ ] `curl -N -H 'Accept: text/event-stream' <hub>/v1/stream?since=0` responds immediately rather
      than hanging — the proxy is not buffering.
- [ ] The Deck loads in a browser and shows the roster.
- [ ] Over the internet: you used `--public`, and `--bind 127.0.0.1` so the tunnel is the only path in.
- [ ] You know the three-word fingerprint, and you will say it out loud when you give out the
      watchword.
- [ ] The state directory is backed up, or you have accepted that it is not.
- [ ] The host token is stored somewhere you will find it again.

---

## See also

| | |
|---|---|
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | When one of the above does not work. |
| [`SECURITY.md`](SECURITY.md) | Threat model and residual risk. |
| [`SPEC.md`](SPEC.md) §3.6, §5.1, §13 | Normative: confidentiality, SSE framing, `doctor`. |
| [`../scripts/tunnel.sh`](../scripts/tunnel.sh) | Automates §2.1–§2.3. |
