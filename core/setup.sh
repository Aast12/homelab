#!/usr/bin/env bash
# Starts the Pi's services: checks core/.env, creates the data folders that live
# outside the repo, brings up the Compose stack, and points the public DNS
# records at the Pi's NetBird address. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

[[ -f .env ]] || { echo "setup.sh: copy core/.env.example to core/.env and fill it in" >&2; exit 1; }
get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }

for key in $(grep -oE '^[A-Z0-9_]+=' .env.example | tr -d =); do
  [[ -n $(get "$key") ]] || { echo "setup.sh: set $key in core/.env" >&2; exit 1; }
done

data=$(get DATA_DIR)
sudo mkdir -p "$data/traefik/letsencrypt" "$data/homeassistant"
docker compose build --pull   # images built here start from the newest base image
docker compose up -d

# Away from home, the domain resolves through public DNS to the Pi's NetBird
# address (see public-dns.py). Re-running keeps the records in step with it.
netbird_ip=$(ip -4 -o addr show dev wt0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
if [[ -n $netbird_ip ]]; then
  DOMAIN=$(get DOMAIN) CF_DNS_API_TOKEN=$(get CF_DNS_API_TOKEN) NETBIRD_IP=$netbird_ip \
    python3 public-dns.py
else
  echo "setup.sh: NetBird is not up (no wt0 address); public DNS records left as they are"
fi
