# For whoever owns the server machine

Someone wants to manage a Minecraft server on your machine from a web dashboard.
This is the piece that would run on your side. Here's exactly what it does, so
you can decide.

## What it is

One Python file (`mc-agent.py`, ~450 lines, no obfuscation — read it). It opens
an **outbound** WebSocket to the dashboard and keeps it open.

**You do not open any ports.** No port forwarding, no inbound firewall rule, no
static IP. The connection is made from your machine, outward, like a browser.

## What it can do

| Action | Scope |
|---|---|
| Start / stop / restart | Only the Minecraft server, via the exact mechanism you configure (`systemctl`, `docker`, `screen`, or a jar it launches itself) |
| Stream the console | Reads `<server_dir>/logs/latest.log`. Read-only. |
| List and read files | **Only inside `server_dir`.** Text files under 256 KB. |
| Create world backups | `tar.gz` of world folders into `backup_dir` |
| Report status | Running/stopped, mode, PID, CPU/memory |

## What it cannot do

- **It is not a remote shell.** There is no "run arbitrary command" action. The
  action list is a fixed `if/elif` chain in `handle_command()` — go look.
- It cannot read outside `server_dir`. Every path goes through `safe_path()`,
  which resolves symlinks and `..` and rejects anything landing outside that
  directory.
- It cannot write or delete files. There is no write action at all. The only
  thing it creates is backup archives in `backup_dir`.
- It cannot touch other services, other users, or the rest of your filesystem.

Run it as an unprivileged user that owns the server directory — not as root.

### Prefer systemd, docker or screen over `managed`

In `managed` mode the agent tracks only the process it started itself. If the
agent is restarted while the server is running, it loses that handle and will
report "stopped" even though the server is up — and pressing Start would then
launch a **second** server on the same world files, which corrupts them.

The other three modes ask the system for the truth (`systemctl is-active`,
`docker inspect`, `screen -ls`), so they can't drift. Since your server already
runs 24/7, it's almost certainly under one of those already — leave `mode = auto`
and let the agent find it. Only fall back to `managed` if the agent prints
`mode=managed` and you know the server is genuinely started by hand.

## Install

```bash
# as the user that owns the Minecraft server
python3 -m venv ~/mc-agent-venv
~/mc-agent-venv/bin/pip install websockets

mkdir -p ~/mc-agent && cd ~/mc-agent
# copy mc-agent.py and agent.conf.example here
cp agent.conf.example agent.conf
nano agent.conf          # set dashboard_url, token, server_dir, mode
chmod 600 agent.conf     # the token is a credential

~/mc-agent-venv/bin/python mc-agent.py --config agent.conf
```

It prints the mode it detected and `[agent] connected`. If it can't reach the
dashboard it retries with backoff and never gives up.

### Keep it running

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
loginctl enable-linger $USER      # so it survives logout
journalctl --user -u mc-agent -f
```

**If you use `mode=systemd` with a system-wide unit**, the agent needs permission
to control just that one unit. Don't give it sudo — use a polkit rule or a
narrow sudoers line scoped to `systemctl start/stop minecraft` only.

## Turning it off

`systemctl --user stop mc-agent`, or rotate `AGENT_TOKEN` on the dashboard side.
Either kills all Tier 2 access instantly. The dashboard keeps working in
read-only mode; nothing else breaks.

## The lighter option

If installing this is more than you want to do, you can grant far less:

- **Nothing at all** — the dashboard already shows online/offline, player count
  and MOTD using the same public ping every Minecraft client sends.
- **RCON only** — set `enable-rcon=true` and a password in `server.properties`.
  That allows in-game commands but no lifecycle control, no console feed, no
  file access. Only do this over a VPN; RCON sends its password in the clear.
