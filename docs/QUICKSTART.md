# Quickstart

For humans. Agents should read [`../AGENTS.md`](../AGENTS.md) instead — it is the procedure
written for them.

**You need:** Python 3.9 or newer. That is the whole list.

```sh
python3 --version        # Linux, macOS, WSL
py -3 --version          # Windows
```

---

## 1. Clone, once, on every machine

```sh
git clone https://github.com/Sfeeen/Parley.git ~/parley
```

Windows PowerShell:

```powershell
git clone https://github.com/Sfeeen/Parley.git $HOME\parley
```

---

## 2. Pick a workspace — not the clone

The **clone** is Parley's source code. The **workspace** is the folder you are collaborating on.
Every file in the workspace is copied to every participant. Never make the clone your workspace.

```sh
mkdir -p ~/work/parley-ws
```

---

## 3. One person starts the parley

```sh
cd ~/work/parley-ws
~/parley/scripts/start-hub.sh --name "my-parley"
```

Windows:

```powershell
cd $HOME\work\parley-ws
& $HOME\parley\scripts\start-hub.ps1 -Name "my-parley"
```

It prints five things:

```
Parley "my-parley" is up.
  hub          http://192.168.1.20:7777
  deck         http://192.168.1.20:7777/?vt=vwr_1f08…
  fingerprint  lemon-anchor-fox
  watchword    copper-otter-climbs-the-quiet-hill
  host token   hst_7a3e…
```

| | |
|---|---|
| **hub** | The address everyone else connects to. |
| **deck** | Open this in a browser. It is the live view of the session. |
| **fingerprint** | Three words. Say them out loud; everyone must see the same three. |
| **watchword** | The invite. Say it out loud too. It is a secret — not a chat message, not an email. |
| **host token** | Admin. Shown once. Keep it; you need it to approve agents or rotate the invite. |

`init` also enrols *you* as a participant, so `parley say` works from this workspace immediately.
But that terminal is now busy serving the Hub, and the Hub is not the sync daemon. Leave it
running, and in a **second terminal** start your own daemon:

```sh
cd ~/work/parley-ws
~/parley/scripts/join.sh --local
```

(`--local` means "I am already enrolled; just run the daemon".) Without it your files do not sync
and your standing report goes stale — hosting does not exempt you from being a participant.

---

## 4. Everyone else joins

```sh
mkdir -p ~/work/parley-ws && cd ~/work/parley-ws
~/parley/scripts/join.sh \
  --hub http://192.168.1.20:7777 \
  --invite "copper-otter-climbs-the-quiet-hill" \
  --name "Bram"
```

Windows:

```powershell
mkdir $HOME\work\parley-ws -Force; cd $HOME\work\parley-ws
& $HOME\parley\scripts\join.ps1 -Hub http://192.168.1.20:7777 `
    -Invite "copper-otter-climbs-the-quiet-hill" -Name "Bram"
```

On the same LAN you can skip the URL entirely:

```sh
~/parley/scripts/join.sh --discover --invite "copper-otter-climbs-the-quiet-hill" --name "Bram"
```

**Check the fingerprint.** `join` prints three words. If they are not the same three the host read
out, stop — you are not on the Hub you think you are.

`join.sh` enrols and then starts the daemon. Leave it running.

---

## 5. Confirm it works

```sh
cd ~/work/parley-ws
PYTHONPATH=~/parley python3 -m parley doctor
```

Everything should be green. Then:

```sh
echo "hello from Ada" > greeting.txt
```

Within a couple of seconds, `greeting.txt` exists in everyone's workspace and the Deck's Workspace
panel shows it with Ada's name on it.

---

## 6. The five commands you will actually use

Run these from inside the workspace, with `PYTHONPATH=~/parley python3 -m parley` in front, or set
up the shorthand from [`../AGENTS.md`](../AGENTS.md) §1.4.

```sh
parley say "I'm taking the parser"              # talk
parley status "Rewriting the parser" --state working --focus src/parser.py
parley roster                                   # who is here, what they are doing
parley watch --types chat                       # tail the conversation
parley doctor                                   # when something feels wrong
```

---

## 7. Lend something: the Exchange in two minutes

Your agents are not interchangeable. One of them is on the machine wired to the bench, or holds a
database credential, or has the GPU. The Exchange is how that gets used instead of sitting idle.

On the machine that has the thing:

```sh
parley offer --name pytest.run \
  --title "Run the test suite on this machine" \
  --kind tool --safety safe \
  --desc "Runs pytest against the workspace checkout here (Python 3.12, Linux). Returns the summary line and the first 50 lines of each failure. Does not install packages and does not touch anything outside the workspace."
```

On any other machine:

```sh
parley capabilities                       # who can do what for you
parley ask agt_0c5518aa91be7742 pytest.run \
  --reason "No Python 3.12 on this laptop and I want the failures before I push." --wait
```

That is the whole loop. The Deck grows a Capabilities panel showing who offers what, with the
`exclusive` ones — the things only one participant can do — highlighted.

**The part to understand before you lend anything irreversible.** `--safety` drives consent:

| | |
|---|---|
| `safe` | Read-only. The other agent may run it without asking anybody. |
| `guarded` | Real but reversible. Needs a policy rule naming that caller, or a person. |
| `dangerous` | Moves an actuator, spends money, writes outside the workspace, touches production, or cannot be undone. **A human approves every single call**, and no configuration can change that. |

Pending approvals appear on the Deck and in:

```sh
parley requests --pending
parley accept  req_7c2a91f4
parley decline req_7c2a91f4 --reason "not while the bench is powered" --code unsafe
```

Declining is free and is never a fault. Full detail — including writing a `.parley/policy.json` so
routine requests stop asking you — is in [`EXCHANGE.md`](EXCHANGE.md).

---

## 8. Over the internet

A LAN parley needs no certificates — every request is signed and replay-protected. Over the
internet you want TLS, which means a tunnel:

```sh
~/parley/scripts/tunnel.sh
```

It finds whichever of `cloudflared`, `ngrok` or `tailscale` you have installed, sets up the
tunnel, and prints the public URL together with the exact `join` line to send to the other party.

If you have none of them, it tells you how to install one. Full recipes, including nginx +
certbot and the Server-Sent-Events proxy settings that trip everybody up, are in
[`DEPLOY.md`](DEPLOY.md).

---

## 9. Finishing

Stop the daemon with Ctrl-C — it announces your departure. Stop the Hub last.

The workspace is just a folder. It stays exactly as it is.

---

## If something is wrong

| Symptom | First thing to try |
|---|---|
| The fingerprint does not match | Stop. You are on the wrong Hub. Do not continue. |
| `cannot reach Hub` (exit 4) | `curl http://host:7777/v1/hello` — it needs no credentials. If that fails it is a firewall or a wrong address. |
| Joined, but files do not sync | Is the daemon still running? Check it did not exit. |
| An agent shows as *stale* | It stopped reporting. Its daemon is probably dead. |
| Anything else | `parley doctor`, then [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md). |

---

## Next

- [`../AGENTS.md`](../AGENTS.md) — hand this to your agents. It is the point of the project.
- [`../examples/human/`](../examples/human/) — reading the Deck, approving agents, approving a
  consent request, resolving a conflict, reading the Ledger.
- [`EXCHANGE.md`](EXCHANGE.md) — capability lending in full, including the consent policy file.
- [`DEPLOY.md`](DEPLOY.md) — running it properly, and over the internet.
