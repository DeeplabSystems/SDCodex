#!/usr/bin/env bash
#
# setup_cron.sh — set up a host-side cron job for the SDCodex UI updater
#
# Installs a cron entry that periodically runs
#   scripts/ui-update-watcher.sh --once
# which applies any update requested from Settings -> System & Update.
#
# The script asks for:
#   1. Your SDCodex install directory (e.g. /home/you/SDCodex) — the rest of
#      the path (scripts/ui-update-watcher.sh) is appended automatically.
#   2. How often to check for updates (minutes, or a raw cron expression).
#
# Requirement: run as (and with) the user whose Docker + repo access matches
# how you normally run update.sh. cron jobs run without a login shell, so if
# `docker` isn't on the default cron PATH, either pass the full path below or
# add `export PATH=/usr/local/bin:$PATH` to the top of your crontab.

set -u

WATCHER_REL="scripts/ui-update-watcher.sh"
LOG_FILE_REL="db/ui_cron.log"

# ANSI colors (best-effort)
if [ -t 1 ]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; NC=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; NC=""
fi

err(){ printf '%sError: %s%s\n' "$RED" "$*" "$NC" >&2; }

# --- 1. Install directory ---------------------------------------------------
while :; do
  read -r -p "${BOLD}SDCodex install directory${NC} (path that contains update.sh, e.g. /home/you/SDCodex): " INSTALL_DIR
  INSTALL_DIR="${INSTALL_DIR%/}"
  if [ -z "$INSTALL_DIR" ]; then
    unset INSTALL_DIR; continue
  fi
  break
done

if [ ! -d "$INSTALL_DIR" ]; then
  err "'$INSTALL_DIR' is not a directory."
  exit 1
fi
if [ ! -x "$INSTALL_DIR/$WATCHER_REL" ]; then
  err "Expected watcher not found/executable: '$INSTALL_DIR/$WATCHER_REL'"
  printf 'Make sure you installed from a current checkout (it ships in scripts/).\n' >&2
  exit 1
fi
if [ ! -f "$INSTALL_DIR/update.sh" ]; then
  err "Could not find '$INSTALL_DIR/update.sh' — is this the SDCodex install directory?"
  exit 1
fi

echo
printf '%sUsing:%s\n' "$BOLD" "$NC"
printf '  watcher: %s\n' "$INSTALL_DIR/$WATCHER_REL"
printf '  update : %s\n' "$INSTALL_DIR/update.sh"
echo

# --- 2. Frequency ------------------------------------------------------------
while :; do
  read -r -p "${BOLD}Check for updates every N minutes${NC} [default 5] (or paste a full cron expression): " FREQ
  FREQ="${FREQ:-5}"
  if printf '%s' "$FREQ" | grep -qE '[[:space:]]'; then
    # Looks like a raw cron schedule (contains whitespace) — use as-is.
    SCHEDULE="$FREQ"
    break
  fi
  if ! printf '%s' "$FREQ" | grep -qE '^[1-9][0-9]*$'; then
    err "Please enter a positive number of minutes (or a cron expression)."
    continue
  fi
  if [ "$FREQ" -gt 59 ]; then
    err "For intervals over 59 minutes use a cron expression (e.g. '0 */2 * * *' for every 2 hours)."
    continue
  fi
  SCHEDULE="*/$FREQ * * * *"
  break
done

# Cron-escape the install dir (spaces). Tilde is expanded by cron's sh. We
# leave straightforward plain paths alone; escape literal spaces.
CRON_INSTALL="${INSTALL_DIR// /\\ }"

cmd="cd ${CRON_INSTALL} && ${CRON_INSTALL}/${WATCHER_REL} --once >> ${CRON_INSTALL}/${LOG_FILE_REL} 2>&1"
entry="$SCHEDULE ${cmd}"

echo
printf '%sWill install the following cron job:%s\n' "$BOLD" "$NC"
printf '  %s\n' "$entry"
read -r -p "Install this cron job? [Y/n] " confirm
case "${confirm:-y}" in
  y|Y|yes|YES) : ;;
  *) printf 'Aborted. No changes made.\n'; exit 0 ;;
esac

# --- 3. Install into the user crontab (idempotent) ---------------------------
CRON_CMD="cd ${CRON_INSTALL} && .*${WATCHER_REL} --once"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

if crontab -l 2>/dev/null | grep -qE "$CRON_CMD"; then
  # Remove any prior auto-generated line(s), then re-add the new one.
  crontab -l 2>/dev/null | grep -vE "$CRON_CMD" > "$tmp"
else
  crontab -l 2>/dev/null > "$tmp" || :  # empty crontab -> blank file
fi
printf '\n# SDCodex UI update watcher (managed by scripts/setup_cron.sh)\n%s\n' "$entry" >> "$tmp"
if crontab "$tmp"; then
  printf '\n%s✔ Installed.%s\n' "$GREEN" "$NC"
else
  err "crontab install failed."
  exit 1
fi

echo
printf '%sCurrent crontab:%s\n' "$BOLD" "$NC"
crontab -l 2>/dev/null | sed 's/^/  /'
echo
printf 'Log file: %s\n' "$INSTALL_DIR/$LOG_FILE_REL"
printf 'To remove later, run:  crontab -e   and delete the "%s" line.\n' "$WATCHER_REL"
exit 0