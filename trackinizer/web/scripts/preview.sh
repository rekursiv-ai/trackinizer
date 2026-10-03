#!/usr/bin/env bash
# Run the local preview: a trackinizer server serving the web app's latest build at
# /app/, with `vite build --watch` keeping that build current.
#
#   scripts/preview.sh      # from web/, then open :8765/app/
#
# The database is persistent PGlite under /opt/scratch/runs, so seeded data
# survives restarts (seed it with scripts/seed_local.py). --no-auth makes every
# request a local admin: never point this at anything but localhost. PORT
# overrides 8765, and PREVIEW_DATA the database directory, which two servers
# cannot share.
set -euo pipefail

web="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
data="${PREVIEW_DATA:-/opt/scratch/runs/trackinizer-web-local}"
mkdir -p "$data"

cd "$web"
./npm run -s build
./npm exec -- vite build --watch --logLevel warn &
watch=$!

# uv walks up from web/ to the project around it, in the monorepo and in the
# published package alike, as web/npm does.
uv --quiet run --frozen --project "$web" python -m trackinizer.server \
  --datadir "$data/pgdata" \
  --host 127.0.0.1 \
  --port "${PORT:-8765}" \
  --no-auth \
  --app-dir "$web/dist" &
server=$!

# Stop only what this script started. `kill 0` would signal the whole process
# group, which includes whatever launched this script when it shares the group.
# vite runs as a child of `npm exec`, so its children go first.
stop() {
  pkill -TERM -P "$watch" 2>/dev/null || true
  kill "$watch" "$server" 2>/dev/null || true
}
trap stop EXIT INT TERM
wait "$server"
