# Synapse (Matrix homeserver)

Service definition lives in `../docker-compose.yml`. Runtime data — `homeserver.yaml`, signing key, SQLite DB, media — lives on the host at `/data/synapse/` (not in this repo).

Server identity is `lab.alamst.me`, served from `matrix.lab.alamst.me` via Traefik. Federation is enabled. Registration is closed (admin-only).

## First-time bootstrap

Synapse needs a generated `homeserver.yaml` + signing key before it can start. Run once on the host:

```bash
sudo mkdir -p /data/synapse
sudo chown 991:991 /data/synapse        # synapse container runs as uid 991

cd ~/projs/homelab/core
docker compose run --rm synapse generate
```

This writes `homeserver.yaml`, `lab.alamst.me.signing.key`, and a log config into `/data/synapse/`.

## Edit `/data/synapse/homeserver.yaml`

```yaml
public_baseurl: "https://matrix.lab.alamst.me/"
serve_server_wellknown: true                  # answer /.well-known/matrix/server
enable_registration: false                    # admin-only (default)
registration_shared_secret: "<long random>"   # uncomment; needed for register_new_matrix_user
```

In the `listeners:` block, confirm the http listener has `x_forwarded: true` — Traefik terminates TLS, Synapse must trust `X-Forwarded-*`. The generator sets this by default; just verify.

Generate the shared secret with `openssl rand -hex 32`. Never commit it.

## Start

```bash
docker compose up -d synapse
docker compose logs -f synapse        # wait for "Synapse now listening on TCP port 8008"
```

## Create the first admin

```bash
docker compose exec synapse register_new_matrix_user \
  -c /data/homeserver.yaml \
  -u <username> \
  -a \
  http://localhost:8008
```

`-a` flags the user as admin. Subsequent invites can be done from a Matrix client by an admin, or via this same command.

## Verify federation delegation

```bash
curl -s https://lab.alamst.me/.well-known/matrix/server
# {"m.server":"matrix.lab.alamst.me:443"}

curl -s https://matrix.lab.alamst.me/_matrix/federation/v1/version
# {"server":{"name":"Synapse","version":"..."}}
```

Final sanity check: <https://federationtester.matrix.org/>.

## Routing notes

Two Traefik routers in `../docker-compose.yml`:

- `synapse` — `Host(matrix.lab.alamst.me)` → container port 8008. Carries client + federation traffic.
- `synapse-wk` — `Host(lab.alamst.me) && PathPrefix(/.well-known/matrix/)` with `priority=100`. Lets the apex domain (which otherwise routes to glance) delegate federation discovery without disturbing the dashboard.

## Backups worth taking

Everything important is under `/data/synapse/`:

- `homeserver.yaml` — config
- `*.signing.key` — **irreplaceable**; lose this and you lose the server identity
- `homeserver.db` — SQLite DB (rooms, messages, users)
- `media_store/` — uploaded files

## Upgrades

`image: ghcr.io/element-hq/synapse:latest` is pinned to the moving tag. Before pulling, skim the [release notes](https://github.com/element-hq/synapse/releases) for breaking changes, then:

```bash
docker compose pull synapse
docker compose up -d synapse
```

Schema migrations run automatically on start.
