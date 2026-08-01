# Minecraft server dashboard — what I'm asking you to run

I built a web dashboard for the Minecraft server. Right now it can only see what
any Minecraft client sees: online/offline, MOTD, player count, version. That part
needs nothing from you and is already working.

To get start/stop/restart, a live console feed, and world backups, one small
program needs to run on your machine. That's what this is.

## The short version

- **You do not open any ports.** No port forwarding, no firewall rule, no static
  IP. It makes an *outbound* connection to my dashboard, like a browser does.
- It is **one Python file**, `mc-agent.py`, about 450 lines, not obfuscated.
- **It is not a remote shell.** There is no "run any command" action. The full
  list of things it can do is a fixed `if/elif` chain in `handle_command()` —
  open the file and look, it's near the bottom.
- You can kill it at any time and nothing else breaks.

## Exactly what it can do

| Action | Scope |
|---|---|
| Start / stop / restart | Only the Minecraft server, via whichever mechanism you configure |
| Stream the console | Reads `<server_dir>/logs/latest.log`. Read-only. |
| List + read files | **Only inside `server_dir`.** Text files under 256 KB. |
| Download files | **Only inside `server_dir`.** Any size. |
| Back up worlds | Writes `tar.gz` archives into `backup_dir` |
| Report status | Running/stopped, mode, PID, CPU/memory, free disk |

## What it cannot do

- Read anything outside `server_dir` — every path goes through `safe_path()`,
  which resolves symlinks and `..` and rejects anything landing outside.
- Touch other services, other users, or the rest of your filesystem.
- Run arbitrary commands. There is no such action.
- **Change any file at all**, unless you opt in — see the next section.

Please run it as the unprivileged user that owns the server directory, **not as
root**.

## Editing files is opt-in, and off by default

The config ships with `allow_writes = no`. While it stays that way, the agent
refuses every attempt to edit, upload, rename, create or delete — it can only
look. On startup it prints which mode it's in, so you can check at a glance.

If you'd rather I could also fix a config or drop in a plugin without bothering
you, set `allow_writes = yes` and restart it. Even then everything stays inside
`server_dir`, uploads are capped by `max_upload_mb`, and every change is logged
with the name of the account that made it. Setting it back to `no` takes the
ability away again, without you having to touch anything else.

I'd suggest starting with `no` and only changing it if the read-only version
turns out to be annoying in practice.

## Install

```bash
# as the user that owns the Minecraft server
python3 -m venv ~/mc-agent-venv
~/mc-agent-venv/bin/pip install websockets

mkdir -p ~/mc-agent && cd ~/mc-agent
# copy mc-agent.py and agent.conf.example into this folder
cp agent.conf.example agent.conf
nano agent.conf
chmod 600 agent.conf     # it holds the token
```

In `agent.conf` set:

```ini
dashboard_url = wss://REPLACE-ME/agent/ws
token         = REPLACE-ME
server_dir    = /path/to/your/minecraft/server
mode          = auto
backup_dir    = /path/to/where/backups/should/go
```

I'll send you the `dashboard_url` and `token` separately.

Then run it:

```bash
~/mc-agent-venv/bin/python mc-agent.py --config agent.conf
```

It prints the mode it detected and `[agent] connected`. If it can't reach the
dashboard it retries with backoff forever, so it's safe to start before I'm up.

## Important: check what mode it prints

Leave `mode = auto`. It probes for a systemd unit, then a docker container, then
a screen session, and only falls back to `managed` if it finds none.

**If it prints `mode=managed`, tell me before we go further.** In that mode the
agent only tracks a process it started itself. If the agent restarts while the
server is running, it loses the handle, reports "stopped", and pressing Start
would launch a *second* server on the same world files — which corrupts them.
The other three modes ask the system for the truth and can't drift.

Since your server already runs 24/7 it's almost certainly under systemd, docker
or screen already, so `auto` should find it. If it doesn't, set
`service_name` / `container` / `screen_name` to match whatever you actually use.

## Keeping it running

`~/.config/systemd/user/mc-agent.service`:

```ini
[Unit]
Description=Minecraft dashboard agent
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/mc-agent
ExecStart=%h/mc-agent-venv/bin/python %h/mc-agent/mc-agent.py --config %h/mc-agent/agent.conf
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now mc-agent
loginctl enable-linger $USER      # so it survives you logging out
journalctl --user -u mc-agent -f
```

If you use `mode=systemd` against a **system-wide** unit, the agent needs
permission for that one unit. Don't give it blanket sudo — use a polkit rule or
a sudoers line scoped to `systemctl start/stop <your-unit>` only.

## Turning it off

`systemctl --user stop mc-agent`. That's it — all of this access ends instantly.
I can also revoke it from my side by rotating the token. The dashboard falls
back to read-only and nothing else is affected.

## If this is more than you want to do

Totally fine, and there's a middle option:

- **Nothing at all** — I keep the online/offline + player count view I already
  have. It uses the same public ping every Minecraft client sends.
- **RCON only** — `enable-rcon=true` and a password in `server.properties`. That
  gives in-game commands but no start/stop, no console feed, no file access.
  One caveat: RCON sends its password unencrypted, so we'd want to put Tailscale
  between our two machines first rather than exposing it to the internet.
