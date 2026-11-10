#!/usr/bin/env bash
# Pull the latest chase from git and redeploy if anything changed. Meant for cron:
#   make install-cron            # every 15 minutes (see Makefile)
#
# Safe by design:
#   - fast-forward only: never merges, rebases or discards anything;
#   - refuses to run if tracked files have local edits (config/chase.yaml and .env are
#     git-ignored, so normal setup never blocks it);
#   - one run at a time (flock);
#   - rebuilds and restarts only when the commit actually changed.
# A restart during barking is safe: state is in the bark log, and any audio gap is recorded in
# the episode metadata.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
BRANCH="${BRANCH:-$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)}"
COMPOSE="${COMPOSE:-docker compose}"
LOCK="${LOCK:-/tmp/chase-auto-update.lock}"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) chase-auto-update: $*"; }

exec 9>"$LOCK"
if ! flock -n 9; then
  log "another update is running; skipping"
  exit 0
fi

cd "$REPO_DIR"

if ! git diff --quiet || ! git diff --cached --quiet; then
  log "tracked files have local changes; not updating (git status to see them)"
  exit 1
fi

git fetch --quiet origin "$BRANCH"
local_rev=$(git rev-parse HEAD)
remote_rev=$(git rev-parse "origin/$BRANCH")

if [ "$local_rev" = "$remote_rev" ]; then
  log "up to date at ${local_rev:0:7} ($BRANCH)"
  exit 0
fi

if ! git merge-base --is-ancestor "$local_rev" "$remote_rev"; then
  log "origin/$BRANCH is not a fast-forward of ${local_rev:0:7}; not updating"
  exit 1
fi

log "updating ${local_rev:0:7} -> ${remote_rev:0:7} ($BRANCH)"
git merge --ff-only --quiet "origin/$BRANCH"
$COMPOSE up -d --build --remove-orphans
log "redeployed at ${remote_rev:0:7}"
