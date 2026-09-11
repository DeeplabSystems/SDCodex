# SDCodex Self-Update Sidecar
#
# A tiny image that bundles the Docker CLI plus a shell, used by the web UI to
# swap its own running container ("self-update"). The SDCodex container talks to
# the Docker API over a (read-only) host socket and creates BOTH the replacement
# container and this sidecar. The sidecar is launched with the replacement
# already prepared; it only has to perform the irreversible swap:
#
#   stop old -> rm old -> rename new -> reconnect networks -> start new -> verify
#
# Because this is a separate container that itself has Docker access, stopping
# the old SDCodex container does not interrupt the updater.
#
# Build (run from the repo root, e.g. by the self-update flow, or manually):
#   docker build -t sdcodex-updater:latest -f updater/Dockerfile updater/
#
# The image is intentionally minimal: Docker CLI + busybox/sh only.

FROM alpine:3.21

# Install the Docker CLI (talks to the host daemon over the mounted socket or
# DOCKER_HOST). busybox provides sleep/sh needed by update.sh.
RUN apk add --no-cache docker-cli bash busybox

COPY update.sh /update.sh
RUN chmod +x /update.sh

ENTRYPOINT ["/bin/bash", "/update.sh"]