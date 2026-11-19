#!/usr/bin/env bash
# Starts the Pi's services: checks core/.env, creates the data folders that live
# outside the repo, and brings up the Compose stack. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

[[ -f .env ]] || { echo "setup.sh: copy core/.env.example to core/.env and fill it in" >&2; exit 1; }
get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }

for key in $(grep -oE '^[A-Z0-9_]+=' .env.example | tr -d =); do
  [[ -n $(get "$key") ]] || { echo "setup.sh: set $key in core/.env" >&2; exit 1; }
done

data=$(get DATA_DIR)
sudo mkdir -p "$data/traefik/letsencrypt" "$data/homeassistant"
docker compose up -d
