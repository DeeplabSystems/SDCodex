#!/bin/bash
git update-index --skip-worktree docker-compose.override.yml
git update-index --skip-worktree requirements.txt
git pull
docker compose down
docker compose up -d --build
