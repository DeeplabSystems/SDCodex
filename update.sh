#!/bin/bash
git update-index --skip-worktree docker-compose.override.yml
git update-index --skip-worktree requirements.txt
git pull
# Stamp the deployed core revision into the shared data volume so the web UI's
# update icon can compare it against the latest remote commit.
git rev-parse --short HEAD > db/core.rev 2>/dev/null || true
docker compose down
docker compose up -d --build
