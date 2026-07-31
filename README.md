# Minecraft Server Dashboard

A control panel for a Minecraft server running on a machine you don't own,
hosted on a Raspberry Pi 5 at home, reachable from the public internet.

```
   browser  ──https──▶  Cloudflare  ──tunnel──▶   Pi 5          remote machine
                                              ┌──────────┐    ┌──────────────┐
                                              │ dashboard│◀───│  mc-agent.py │  outbound wss
                                              │  :8080   │───▶│              │  (no open ports)
                                              └────┬─────┘    ├──────────────┤
                                                   ├──ping───▶│  :25565      │
                                                   └──rcon───▶│  :25575      │
                                                              └──────────────┘
```

## Capability tiers

The server is on someone else's machine, so what you can do depends on what its
owner grants. The dashboard detects each tier at runtime and greys out what
isn't available — it never breaks, it just does less.

| Tier | Needs | Unlocks |
|---|---|---|
| **0 — Ping** | nothing | online/offline, MOTD, player count + sample, version, latency |
| **1 — RCON** | `enable-rcon=true` + password | send any console command, authoritative player list |
| **2 — Agent** | `mc-agent.py` running on the box | start/stop/restart, live console stream, file browsing, backups |

Tier 0 works today. Tier 1 is a two-line config change. Tier 2 needs the owner
to run a script — hand them [`agent/README-for-owner.md`](agent/README-for-owner.md),
which is written for exactly that conversation.

**RCON has no encryption.** Its password crosses the wire in plaintext on the
first packet. Only ever point `RCON_HOST` at a private address — a Tailscale IP,
a WireGuard peer, or `127.0.0.1` through an SSH tunnel. Never at a public IP.

## Install on the Pi

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone <this repo> ~/mc-dashboard && cd ~/mc-dashboard

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
.venv/bin/python -m app.hashpw          # prints ADMIN_PASSWORD_HASH
nano .env                               # paste it in, set MC_HOST, SECRET_KEY
chmod 600 .env

.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 --no-proxy-headers
```

Open `http://<pi-ip>:8080`. For this first LAN test set `SECURE_COOKIE=false`,
since the session cookie is marked `Secure` and a browser won't store it over
plain HTTP.

`--no-proxy-headers` is not optional. Uvicorn otherwise rewrites the client
address from the *leftmost* `X-Forwarded-For` entry — the part the client
supplies — and the login lockout keys on that address, so a caller who sends a
different value each time gets unlimited password attempts. The app parses the
headers itself instead: only from a loopback peer, preferring Cloudflare's
`CF-Connecting-IP`, and taking the rightmost hop. That parsing stays off until
you set `TRUST_PROXY_HEADERS=true`, which you should do only once the tunnel
below is actually in front.

Then install the service and the supervision around it:

```bash
sudo deploy/install-supervision.sh
journalctl -u mc-dashboard -f
```

Note it binds to `127.0.0.1` — the tunnel below is the only way in.

## Staying up

Four layers, each catching what the one below it can't:

| Failure | What catches it |
| --- | --- |
| Process exits or crashes | `Restart=always`, with `StartLimitIntervalSec=0` so systemd never gives up |
| Process alive but wedged | `/healthz` + the supervisor timer — 3 bad probes in a row triggers a restart |
| You edited the code | Same timer notices the change and restarts, *after* checking it imports |
| Kernel hang, OOM freeze, panic | BCM2835 hardware watchdog + `kernel.panic=10` |

The one that isn't obvious is the second. `Restart=always` only fires when the
process *exits* — an event loop that has wedged keeps the process alive and the
unit `active` while it serves nothing at all, and systemd is perfectly happy.
So `/healthz` reports whether the poll loop is still turning, and the supervisor
restarts on that rather than on liveness of the process.

Two deliberate refusals in `deploy/supervise.sh`:

- **It won't restart into code that doesn't import.** A half-finished save would
  otherwise take the dashboard down and leave systemd restart-looping on it. The
  running version stays up until the code is valid again.
- **It won't restart a unit you stopped by hand.** `systemctl stop` means you
  wanted it stopped; only the `failed` state gets recovered.

The watchdog needs a reboot to arm. Confirm it afterwards:

```bash
journalctl -b | grep -i watchdog        # "Watchdog running with a timeout of 14s"
systemctl list-timers mc-dashboard-supervise.timer
```

To stop auto-restarting on edits but keep everything else, set
`Environment=WATCH_CODE=0` in `mc-dashboard-supervise.service`.

**Once you add the tunnel, it becomes the single point of failure** — the
dashboard can be perfectly healthy and still unreachable if `cloudflared` dies.
Give its unit the same `Restart=always` and `StartLimitIntervalSec=0`.

## Exposing it publicly

Use **Cloudflare Tunnel**. It gives you HTTPS and a hostname without forwarding
a port, exposing your home IP, or depending on your ISP giving you a stable
address — and it means a scanner sweeping your IP finds nothing.

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
  -o cloudflared && chmod +x cloudflared && sudo mv cloudflared /usr/local/bin/

cloudflared tunnel login
cloudflared tunnel create mc-dashboard
cloudflared tunnel route dns mc-dashboard mc.yourdomain.com
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: mc-dashboard
credentials-file: /home/pi/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: mc.yourdomain.com
    service: http://localhost:8080
  - service: http_status:404
```

```bash
sudo cloudflared service install
```

Set `AGENT_TOKEN` and give the owner `wss://mc.yourdomain.com/agent/ws`.

**Do this too**, since the login page is now world-reachable: put Cloudflare
Access in front of the hostname (free tier, email-code or Google SSO). It stops
unauthenticated traffic at Cloudflare's edge, so the Pi never sees a login
attempt. The app's own password auth then becomes your second layer rather than
your only one.

If you'd rather not involve Cloudflare, **Tailscale** is the other good answer —
`tailscale serve` gives you HTTPS on a private network with no public exposure
whatsoever. You lose "share a link with a friend", you gain a much smaller
attack surface.

## Security notes

What's already handled:

- scrypt password hashing; HMAC-signed `HttpOnly` `SameSite=Lax` session cookies
- per-IP login lockout (8 attempts / 15 min), constant-time credential compare
- agent token compared with `hmac.compare_digest`, checked *before* the socket
  is accepted; agent support is off entirely unless `AGENT_TOKEN` is set
- CSP, `nosniff`, `X-Frame-Options: DENY`, no `/docs` endpoint
- untrusted strings (player names, console output, filenames) go through
  `textContent`, never `innerHTML`
- agent file access is jailed to `server_dir` with symlink-aware resolution, and
  exposes no write, delete, or arbitrary-command action

What's on you:

- a long unique dashboard password, and `SECRET_KEY` actually set in `.env`
- `chmod 600 .env` and `agent.conf` — both hold credentials
- RCON only over a private network, never a public IP
- keep the Pi patched: `sudo apt update && sudo apt upgrade`

## Layout

```
app/
  main.py       FastAPI routes, background poller, both WebSocket endpoints
  ping.py       Server List Ping — Tier 0, stdlib only
  rcon.py       Source RCON client — Tier 1, stdlib only
  agenthub.py   Broker between the agent socket and browser sockets
  auth.py       scrypt hashing, signed sessions, login throttle
  config.py     env/.env settings
  hashpw.py     python -m app.hashpw
static/         Vanilla JS + CSS. No build step, nothing to npm install.
agent/          mc-agent.py + the doc to hand the machine's owner
deploy/         systemd unit for the Pi
```

Frontend is deliberately dependency-free — a Pi shouldn't be running a Node
build pipeline to draw six status tiles.
