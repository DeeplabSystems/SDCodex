"""System cron — shared config for the host-side cron installer.

The UI (inside the container) cannot edit the host user's crontab, so it writes
a *desired* cron spec into the shared state file ``db/system_cron.json``
(bind-mounted as /data/db). A small host-side agent (``scripts/ui-cron-agent.sh``,
run once, typically with ``--watch``) notices the request, installs/updates the
cron job with ``crontab``, and records the outcome so the web UI shows status.

Because the agent lives in <repo>/scripts, it already knows the install dir and
only needs the *frequency* from the UI.

State file shape::

    {
      "requested": bool,
      "requested_at": "ISO8601",
      "requested_by": "web",
      "schedule_minutes": 5,          // OR
      "schedule": "0 */2 * * *",      // raw cron expression (preferred when set)
      "state": "idle" | "applied" | "pending" | "error",
      "message": "human-readable status / installed cron line",
      "applied_at": "ISO8601",
      "cron_line": "the installed crontab line"
    }
"""

import json
import os
import re
import time

import click

DEFAULT_CRON_FILE = os.environ.get(
    "SYSTEM_CRON_STATE", "/data/db/system_cron.json"
)

_KEYS = (
    "requested", "requested_at", "requested_by",
    "schedule_minutes", "schedule", "state", "message",
    "applied_at", "cron_line",
)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def cron_file():
    return os.environ.get("SYSTEM_CRON_STATE", DEFAULT_CRON_FILE)


def _read(path=None):
    path = path or cron_file()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    for k in _KEYS:
        data.setdefault(k, "")
    data.setdefault("requested", False)
    data.setdefault("schedule_minutes", 5)
    data.setdefault("state", "idle")
    return data


def _write(data, path=None):
    path = path or cron_file()
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


def get_config(path=None):
    return _read(path)


def _validate_frequency(schedule_minutes, schedule):
    """Return (valid, normalized_schedule_minutes, normalized_schedule, error)."""
    if schedule and str(schedule).strip():
        raw = str(schedule).strip()
        # Must look like a 5-field cron expression.
        if not re.match(r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+$", raw):
            return False, None, None, "Invalid cron expression (need 5 fields)."
        return True, schedule_minutes, raw, ""
    try:
        mins = int(schedule_minutes)
    except (TypeError, ValueError):
        mins = 0
    if not (1 <= mins <= 59):
        return False, None, None, "Minutes must be 1-59 for an every-N schedule (use a cron expression for longer)."
    return True, mins, "", ""


def request_apply(schedule_minutes=5, schedule="", user="web", path=None):
    """Record a request to apply a cron frequency; returns (ok, message, config)."""
    ok, mins, raw_sched, err = _validate_frequency(schedule_minutes, schedule)
    if not ok:
        return False, err, _read(path)
    data = _read(path)
    data.update({
        "requested": True,
        "requested_at": _now(),
        "requested_by": user or "web",
        "schedule_minutes": mins,
        "schedule": raw_sched,
        "state": "pending",
        "message": "Saved — the host cron agent will apply it on its next run.",
        "applied_at": "",
        "cron_line": "",
    })
    _write(data, path)
    return True, "Saved.", data


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SDCodex system-cron state helper.")
    parser.add_argument("--file", default=None, help="Path to the cron state JSON.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="Print current cron config as JSON.")
    p_apply = sub.add_parser("apply", help="Record an apply request.")
    p_apply.add_argument("--minutes", type=int, default=5)
    p_apply.add_argument("--schedule", default="")
    p_apply.add_argument("--user", default="web")

    args = parser.parse_args()
    path = args.file
    if args.cmd == "status":
        click.echo(json.dumps(get_config(path), indent=2))
    elif args.cmd == "apply":
        ok, msg, cfg = request_apply(
            args.minutes, args.schedule, args.user, path
        )
        if not ok:
            click.echo(f"error: {msg}", err=True)
            raise SystemExit(1)
        click.echo(json.dumps(cfg, indent=2))