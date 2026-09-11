#!/bin/sh
set -e

# PUID / PGID mapping with fallbacks to UID / GID / GUID or default 1000
PUID="${PUID:-${UID:-1000}}"
PGID="${PGID:-${GID:-${GUID:-1000}}}"
UMASK="${UMASK:-002}"
umask "$UMASK"

# Install the bind-mounted requirements.txt so plugin dependencies that were
# aggregated into it (e.g. via Settings -> Plugins) are available at runtime.
# This makes plugin installs take effect on the next `docker compose up -d`
# without requiring an image rebuild. Best effort: if install fails, continue
# so the app can still start (the affected plugin will simply report a load error).
if [ -f /app/requirements.txt ] && [ "$(id -u)" = "0" ]; then
    echo "[entrypoint] Installing/updating Python dependencies from /app/requirements.txt (+ plugin-requirements.txt if present) ..."
    if [ -f /app/plugin-requirements.txt ]; then
        python -m pip install --no-cache-dir -r /app/requirements.txt -r /app/plugin-requirements.txt \
            || echo "[entrypoint] WARNING: pip install failed (will continue without updated deps)"
    else
        python -m pip install --no-cache-dir -r /app/requirements.txt \
            || echo "[entrypoint] WARNING: pip install failed (will continue without updated deps)"
    fi
fi

# If running as root, prepare user, groups, directory permissions, and drop privileges
if [ "$(id -u)" = "0" ]; then
    # Create group if not existing
    if [ "$PGID" != "0" ]; then
        if ! getent group "$PGID" >/dev/null 2>&1; then
            groupadd -g "$PGID" sdcodex 2>/dev/null || groupadd -g "$PGID" appgroup 2>/dev/null || true
        fi
    fi

    # Create user if not existing
    if [ "$PUID" != "0" ]; then
        if ! getent passwd "$PUID" >/dev/null 2>&1; then
            useradd -u "$PUID" -g "$PGID" -d /home/sdcodex -m -s /bin/bash sdcodex 2>/dev/null || \
            useradd -u "$PUID" -d /home/sdcodex -m -s /bin/bash appuser 2>/dev/null || true
        fi
    fi

    # Ensure writable HOME directory for gunicorn/cache tools
    mkdir -p /home/sdcodex
    [ "$PUID" != "0" ] && [ "$PGID" != "0" ] && chown -R "$PUID:$PGID" /home/sdcodex 2>/dev/null || true
    export HOME=/home/sdcodex

    # Ensure all volume directories exist
    for dir in \
        /data/db \
        "${TASKS_DIR:-/data/tasks}" \
        "${CONFIG_DIR:-/data/config}" \
        "${DOWNLOADS_DIR:-/data/downloads}" \
        "${HF_HOME:-/data/huggingface}" \
        "${REMBG_OUTPUT:-/data/rembg_output}" \
        "${COMFYUI_CUSTOM_NODES:-/data/custom_nodes}" \
        "${PLUGINS_DIR:-/app/plugins}" \
        /app/app/static/downloads \
        /app/app/static/saved_gallery; do
        if [ -n "$dir" ]; then
            mkdir -p "$dir"
        fi
    done

    # If PUID/PGID are not root (0), chown mount points and application files
    if [ "$PUID" != "0" ] && [ "$PGID" != "0" ]; then
        # Fast chown on mount points so any directory Docker created on the host gets correct user ownership
        for dir in \
            /data \
            /data/db \
            "${TASKS_DIR:-/data/tasks}" \
            "${CONFIG_DIR:-/data/config}" \
            "${DOWNLOADS_DIR:-/data/downloads}" \
            "${HF_HOME:-/data/huggingface}" \
            "${REMBG_OUTPUT:-/data/rembg_output}" \
            "${COMFYUI_CUSTOM_NODES:-/data/custom_nodes}" \
            "${PLUGINS_DIR:-/app/plugins}" \
            /app/app/static/downloads \
            /app/app/static/saved_gallery; do
            if [ -d "$dir" ]; then
                chown "$PUID:$PGID" "$dir" 2>/dev/null || true
                chmod 775 "$dir" 2>/dev/null || true
            fi
        done

        # Recursively ensure app state and db files are owned by PUID:PGID
        chown -R "$PUID:$PGID" /data/db "${TASKS_DIR:-/data/tasks}" "${CONFIG_DIR:-/data/config}" "${REMBG_OUTPUT:-/data/rembg_output}" /app/plugins 2>/dev/null || true

        # Optional recursive chown on large model/download directories if requested
        if [ "${CHOWN_ALL:-0}" = "1" ]; then
            chown -R "$PUID:$PGID" /app/app/static/downloads "${DOWNLOADS_DIR:-/data/downloads}" "${HF_HOME:-/data/huggingface}" /app/app/static/saved_gallery 2>/dev/null || true
        fi

        # Ensure mounted docker-compose and .env files are writable by the container user
        [ -f /app/docker-compose.yml ] && chown "$PUID:$PGID" /app/docker-compose.yml 2>/dev/null || true
        [ -f /app/.env ] && chown "$PUID:$PGID" /app/.env 2>/dev/null || true

        # Make the mounted Docker socket reachable by the (dropped) runtime user so
        # the in-UI self-update can talk to the daemon. A bind-mounted socket is a
        # root:docker 660 inode; the app runs as PUID:PGID which usually is not in
        # the docker group, so connect() is denied unless we relax it. Best-effort.
        if [ -S /var/run/docker.sock ] && [ "$PUID" != "0" ]; then
            chmod 666 /var/run/docker.sock 2>/dev/null || true
            chmod o+rx /var/run 2>/dev/null || true
        fi

        # Default command if none provided
        if [ $# -eq 0 ]; then
            set -- gunicorn --bind 0.0.0.0:"${PORT:-5001}" --workers 1 --threads 8 --timeout 0 run:app
        fi

        # Drop privileges to user PUID:PGID
        if command -v gosu >/dev/null 2>&1; then
            exec gosu "$PUID:$PGID" "$@"
        elif command -v setpriv >/dev/null 2>&1; then
            exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
        elif command -v runuser >/dev/null 2>&1; then
            USER_NAME="$(getent passwd "$PUID" | cut -d: -f1)"
            if [ -n "$USER_NAME" ]; then
                exec runuser -u "$USER_NAME" -- "$@"
            else
                exec runuser -u "#$PUID" -g "#$PGID" -- "$@"
            fi
        fi
    fi
fi

# Fallback when running directly as non-root or when root
if [ $# -eq 0 ]; then
    set -- gunicorn --bind 0.0.0.0:"${PORT:-5001}" --workers 1 --threads 8 --timeout 0 run:app
fi

exec "$@"
