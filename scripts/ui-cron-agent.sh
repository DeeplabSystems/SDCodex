#!/usr/bin/env bash
#
# ui-cron-agent.sh — host-side agent that applies cron settings from the web UI
#
# The Settings -> System & Update -> "Cron setup" card can't edit your crontab
# directly (the app runs inside a container with no host access). Instead it
# writes a *desired* frequency into the shared file <repo>/db/system_cron.json
# (bind-mounted as /data/db). This agent reads that file, installs/updates the
# corresponding cron job with `crontab`, and records the result.
#
# The agent lives in <repo>/scripts, so it already knows the install dir — you
# only need to start it once on the HOST:
#
#   scripts/ui-cron-agent.sh --watch     # run in a terminal (apply automatically)
#   scripts/ui-cron-agent.sh --once      # check-and-apply once (cron-friendly)
#
# Requirements: run AS the user that owns <repo> and has Docker access (the
# user that normally runs update.sh). The cron job it installs is:
#
#   <schedule> cd <repo> && <repo>/scripts/ui-update-watcher.sh --once >> <repo>/db/ui_cron.log 2>&1
#
# (The leading `cd <repo>` ensures update.sh / `docker compose` run from the
# right directory.)

set -u

SELF="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
REPO_ROOT="$(dirname "$SELF")"

WATCHER_REL="scripts/ui-update-watcher.sh"
LOG_FILE_REL="db/ui_cron.log"
CRON_TAG="# SDCodex UI update watcher (managed by ui-cron-agent.sh)"

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
CRON_STATE="${CRON_STATE:-$DB_DIR/system_cron.json}"
MODE="${1:---once}"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
now_iso(){ date '+%Y-%m-%dT%H:%M:%S%z'; }

write_state(){
  # $1=state  $2=message (may be empty)  $3=cron_line (may be empty)
  python3 - "$CRON_STATE" "$1" "${2:-}" "${3:-}" <<'PY'
import json, os, sys, time
path, state, message, cron_line = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
try:
    d = json.load(open(path))
except Exception:
    d = {}
d["requested"] = False
d["state"] = state
if message is not None:
    d["message"] = message
if cron_line is not None:
    d["cron_line"] = cron_line
if state == "applied":
    d["applied_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
tmp = path + ".tmp"
json.dump(d, open(tmp, "w"), indent=2)
os.replace(tmp, path)
PY
}

# extract current request from the state file; echoes "MINUTES|RAW" or empty
pending_request(){
  python3 - "$CRON_STATE" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
if not d.get("requested"):
    sys.exit(1)
raw = str(d.get("schedule") or "").strip()
if raw:
    print(raw)
else:
    print(str(int(d.get("schedule_minutes") or 5)))
PY
}

cronexpr(){
  # $1 = "MINUTES" or a raw cron expression
  if [[ "$1" =~ ^[0-9]+$ ]]; then
    printf '*/%s * * * *' "$1"
  else
    printf '%s' "$1"
  fi
}

apply_once(){
  [ -f "$CRON_STATE" ] || return 0
  local freq
  if ! freq="$(pending_request)"; then
    return 0
  fi
  log "Cron apply requested (frequency: $freq) — installing."

  if [ ! -x "$REPO_ROOT/$WATCHER_REL" ]; then
    log "Watcher '$REPO_ROOT/$WATCHER_REL' missing; marking error."
    write_state "error" "Watcher not found: $REPO_ROOT/$WATCHER_REL" ""
    return 0
  fi

  local sched line entry
  sched="$(cronexpr "$freq")"
  # Cron-escape spaces in the repo path (rare); plain paths pass through.
  local esc
  esc="${REPO_ROOT// /\\ }"
  entry="cd ${esc} && ${esc}/${WATCHER_REL} --once >> ${esc}/${LOG_FILE_REL} 2>&1"
  line="$sched $entry"

  local tmp
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' RETURN
  if crontab -l 2>/dev/null | grep -qE "${WATCHER_REL} --once"; then
    crontab -l 2>/dev/null | grep -vE "${WATCHER_REL} --once" > "$tmp"
  else
    crontab -l 2>/dev/null > "$tmp" || :
  fi
  printf '\n%s\n%s\n' "$CRON_TAG" "$line" >> "$tmp"

  if crontab "$tmp" 2>/dev/null; then
    write_state "applied" "Cron installed: $line" "$line"
    log "Installed: $line"
  else
    write_state "error" "crontab install failed (permission/PATH?) — run ui-cron-agent.sh with the docker-capable user." ""
    log "crontab install failed."
  fi
  rm -f "$tmp"
}

case "$MODE" in
  --once) apply_once ;;
  --watch)
    log "Watching $CRON_STATE for cron apply requests (Ctrl+C to stop)."
    while true; do
      apply_once
      sleep "${POLL_SECONDS:-5}"
    done
    ;;
  --help|-h)
    grep '^#' "$0" | sed '1,1d;s/^# \{0,1\}//'
    exit 0
    ;;
  *) echo "usage: $0 [--once|--watch|--help]" >&2; exit 2 ;;
esac
exit 0