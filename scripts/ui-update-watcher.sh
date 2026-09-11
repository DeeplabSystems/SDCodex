#!/usr/bin/env bash
#
# ui-update-watcher.sh — host-side updater for SDCodex
#
# The web app runs inside the `sdcodex` container and therefore cannot run
# `update.sh` itself (no `docker`/`docker-compose` inside the container, and
# `docker compose down` / `up -d --build` are host operations). Instead, the
# Settings -> System & Update "Request Update" button writes a request into the
# shared state file <repo>/db/system_update.json (bind-mounted into the
# container at /data/db/system_update.json). This script watches that file and
# applies the update on the HOST.
#
# Usage:
#   ui-update-watcher.sh --once     # check for a pending request, apply if any, exit
#   ui-update-watcher.sh --watch    # loop forever, applying pending requests (Ctrl+C to stop)
#
# cron (host), every 5 minutes:
#   */5 * * * * /path/to/SDCodex/scripts/ui-update-watcher.sh --once
#
# Requirements: run AS the user that owns the repo and has docker access
# (the same user that normally runs `update.sh`). Give the full path if docker
# isn't on the cron PATH, or export PATH=/usr/local/bin:$PATH in the crontab.

set -u
SELF="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
REPO_ROOT="$(dirname "$SELF")"

# Resolve the shared state-file location. The web app (inside the container)
# writes /data/db/system_update.json which is bind-mounted from the host dir
# given by the compose DB variable (default ./db). We read it from the host
# side so the watcher sees the same file the web app wrote.
_default_db_dir(){
  local db
  db="$(grep -E '^DB=' "$REPO_ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
  db="${db:-./db}"
  case "$db" in
    /*) printf '%s\n' "$db" ;;
    *)  printf '%s/%s\n' "$REPO_ROOT" "$db" ;;
  esac
}
DB_DIR="${DB_DIR:-$( _default_db_dir )}"
STATE_FILE="${STATE_FILE:-$DB_DIR/system_update.json}"
UPDATE_SCRIPT="${UPDATE_SCRIPT:-$REPO_ROOT/update.sh}"
GUARD_FILE="${GUARD_FILE:-$DB_DIR/system_update.lock}"
LOG_FILE="${LOG_FILE:-$DB_DIR/system_update.log}"

MODE="${1:---once}"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

now_iso(){ date '+%Y-%m-%dT%H:%M:%S%z'; }

git_describe(){
  ( cd "$REPO_ROOT" && git describe --always --tags 2>/dev/null || git rev-parse --short HEAD 2>/dev/null || echo "unknown" )
}

usage(){ grep '^#' "$0" | sed '1,1d;s/^# \{0,1\}//'; }

# write_state <key> <value> constructs the JSON; we rewrite minimal fields.
write_state(){
  local requested="$1" state="$2" message="$3"
  # Preserve fields the web app owns (requested_at/requested_by/run_id) by
  # re-reading and patching with python (available on the host).
  python3 - "$STATE_FILE" "$requested" "$state" "$message" <<'PY'
import json, os, sys, time
path, requested, state, message = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
try:
    d = json.load(open(path))
except Exception:
    d = {}
d["requested"] = (requested == "true")
if requested == "true":
    d["requested_at"] = d.get("requested_at") or time.strftime("%Y-%m-%dT%H:%M:%S%z")
d["state"] = state
if message is not None:
    d["message"] = message
if state == "running":
    d["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
elif state in ("done", "failed"):
    d["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    d["version"] = d.get("version") or ""
os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
tmp = path + ".tmp"
json.dump(d, open(tmp, "w"), indent=2)
os.replace(tmp, path)
PY
}

# Is there a pending request? (requested == true and not already running)
pending(){
  python3 - "$STATE_FILE" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
sys.exit(0 if (d.get("requested") and d.get("state") != "running") else 1)
PY
}

apply_once(){
  if [ ! -f "$STATE_FILE" ]; then
    return 0
  fi
  if ! pending; then
    return 0
  fi
  log "Pending update request found — applying."

  # Single-flight guard so --watch and cron can't both run update.sh at once.
  if [ -f "$GUARD_FILE" ] && kill -0 "$(cat "$GUARD_FILE" 2>/dev/null)" 2>/dev/null; then
    log "Another updater is running (pid $(cat "$GUARD_FILE")); skipping."
    return 0
  fi
  echo "$$" > "$GUARD_FILE"

  write_state true "running" "Update in progress… (host updater $(git_describe))"
  log "Running $UPDATE_SCRIPT"

  if bash "$UPDATE_SCRIPT" >"$LOG_FILE" 2>&1; then
    write_state false "done" "Update completed successfully."
    log "Update OK."
  else
    write_state false "failed" "Update failed — see $LOG_FILE on the host."
    log "Update failed (see $LOG_FILE)."
  fi
  rm -f "$GUARD_FILE"
}

case "$MODE" in
  --once)
    apply_once
    ;;
  --watch)
    log "Watching $STATE_FILE for update requests (Ctrl+C to stop)."
    while true; do
      apply_once
      sleep "${POLL_SECONDS:-10}"
    done
    ;;
  --help|-h)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

exit 0