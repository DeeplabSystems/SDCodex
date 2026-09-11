"""System update state — a tiny shared-state channel between the web UI and a
host-side updater (``scripts/ui-update-watcher.sh``).

Because the Flask app runs inside the ``sdcodex`` container but ``update.sh``
(``git pull`` + ``docker compose up -d --build``) must run on the **host**, the
UI cannot invoke it directly. Instead the UI writes an *update request* into a
JSON state file, and a small host-side watcher (triggered by cron or a loop)
notices it, runs ``update.sh``, and records the outcome.

The state file lives in the shared ``/data/db`` volume (host ``./db``), which
is bind-mounted into the container and survives ``git pull`` / rebuilds.

State file shape::

    {
      "requested": bool,
      "requested_at": "ISO8601",
      "requested_by": "web",
      "state": "idle" | "running" | "done" | "failed",
      "message": "human-readable status/error",
      "started_at": "ISO8601",
      "finished_at": "ISO8601",
      "version": "git describe of the repo at last run",
      "run_id": "uuid-like string per update run"
    }
"""

import json
import os
import time
import uuid

import click

# The container path; the host watcher uses the same relative file under ./db.
DEFAULT_STATE_FILE = os.environ.get("SYSTEM_UPDATE_STATE", "/data/db/system_update.json")

_STATE_KEYS = (
    "requested", "requested_at", "requested_by",
    "state", "message", "started_at", "finished_at", "version", "run_id",
)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def state_file():
    """Return the state-file path (respects env override)."""
    return os.environ.get("SYSTEM_UPDATE_STATE", DEFAULT_STATE_FILE)


def _read(path=None, default_ok=True):
    path = path or state_file()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    for k in _STATE_KEYS:
        data.setdefault(k, "")
    data.setdefault("requested", False)
    data.setdefault("state", "idle")
    return data


def _write(data, path=None):
    path = path or state_file()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception as e:
        click.echo(f"could not write {path}: {e}", err=True)


def get_status(path=None):
    return _read(path)


def request_update(user="web", path=None):
    """Record a request to update; returns the new status dict."""
    data = _read(path)
    data.update({
        "requested": True,
        "requested_at": _now(),
        "requested_by": user or "web",
        "state": "idle",
        "message": "Update requested; will be applied by the host updater on its next run.",
    })
    _write(data, path)
    return data


def cancel_after_failure(path=None):
    """Called when a request is no longer actionable (internal helper)."""
    data = _read(path)
    data["requested"] = False
    _write(data, path)
    return data


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SDCodex system-update state helper.")
    parser.add_argument("--file", default=None, help="Path to the state JSON file.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="Print the current state file as JSON.")
    p_request = sub.add_parser("request", help="Record an update request.")
    p_request.add_argument("--user", default="web")

    args = parser.parse_args()
    path = args.file
    if args.cmd == "status":
        click.echo(json.dumps(get_status(path), indent=2))
    elif args.cmd == "request":
        click.echo(json.dumps(request_update(args.user, path), indent=2))