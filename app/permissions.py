"""The capabilities an account can be granted.

One canonical list, read by three places that must never disagree: the
dependency guarding each endpoint, the validator deciding what may be written
to users.json, and the checkbox grid in the debug panel. A permission cannot
appear in the UI without something enforcing it, and a string that isn't here
cannot be stored.
"""

from typing import Any

CONSOLE_VIEW = "console.view"
CONSOLE_COMMAND = "console.command"
POWER_START = "power.start"
POWER_STOP = "power.stop"
POWER_RESTART = "power.restart"
FILES_BROWSE = "files.browse"
FILES_READ = "files.read"
FILES_WRITE = "files.write"
FILES_DELETE = "files.delete"
BACKUP_LIST = "backup.list"
BACKUP_CREATE = "backup.create"
DEBUG_PM2 = "debug.pm2"

GROUPS: list[dict[str, Any]] = [
    {
        "title": "Console",
        "permissions": [
            {
                "key": CONSOLE_VIEW,
                "label": "View console",
                "detail": "Watch the live server log, including its backlog.",
            },
            {
                "key": CONSOLE_COMMAND,
                "label": "Run commands",
                "detail": "Send any command over RCON. This is the full server console.",
                "danger": True,
            },
        ],
    },
    {
        "title": "Power",
        "permissions": [
            {
                "key": POWER_START,
                "label": "Start",
                "detail": "Bring the server up.",
            },
            {
                "key": POWER_STOP,
                "label": "Stop",
                "detail": "Shut the server down. Players are disconnected.",
                "danger": True,
            },
            {
                "key": POWER_RESTART,
                "label": "Restart",
                "detail": "Stop, then start. Players are disconnected.",
                "danger": True,
            },
        ],
    },
    {
        "title": "Files",
        "permissions": [
            {
                "key": FILES_BROWSE,
                "label": "Browse",
                "detail": "List directories in the server folder.",
            },
            {
                "key": FILES_READ,
                "label": "Read and download",
                "detail": "Open or download any file, configs and their secrets included.",
                "danger": True,
            },
            {
                "key": FILES_WRITE,
                "label": "Edit and upload",
                "detail": "Save edits, upload files, make folders, rename and move things.",
                "danger": True,
            },
            {
                "key": FILES_DELETE,
                "label": "Delete",
                "detail": "Remove files and folders, worlds included. Nothing is recoverable.",
                "danger": True,
            },
        ],
    },
    {
        "title": "Backups",
        "permissions": [
            {
                "key": BACKUP_LIST,
                "label": "List",
                "detail": "See which archives exist.",
            },
            {
                "key": BACKUP_CREATE,
                "label": "Create",
                "detail": "Trigger a world backup. Slow, but harmless.",
            },
        ],
    },
    {
        "title": "Diagnostics",
        "permissions": [
            {
                "key": DEBUG_PM2,
                "label": "PM2 processes",
                "detail": "See the process table in the debug tab.",
            },
        ],
    },
]


def ordered() -> list[str]:
    """Every permission key, in catalogue order."""
    return [entry["key"] for group in GROUPS for entry in group["permissions"]]


ALL: frozenset[str] = frozenset(ordered())


def sanitize(raw: Any) -> list[str]:
    """Reduce untrusted input to known permissions, in catalogue order.

    A whitelist, not a filter: anything not in the catalogue is dropped rather
    than rejected, so a stale panel or a hand-edited users.json can't inject a
    permission string that no endpoint checks — which would read as "granted"
    in the UI while meaning nothing on the server.
    """
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return []
    wanted = {item for item in raw if isinstance(item, str)}
    return [key for key in ordered() if key in wanted]
