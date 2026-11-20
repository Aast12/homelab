# Homelab

Two kinds of machine serve everything under one domain (`lab.alamst.me`):

- **A Raspberry Pi** (`core/`, Docker Compose): DNS (Blocky), the front proxy (Traefik), the
  dashboard (Glance) and Home Assistant.
- **A k3s cluster** (`k8s/`): every other app. Some nodes are **media nodes**: they hold the media
  library at `/data/media`, and only they run the apps that need it.

Blocky answers the domain and every name under it with the Pi's address. The Pi's Traefik serves
its own apps and forwards every other name to the cluster's floating address, which kube-vip
keeps on one healthy node. So DNS never changes when you add an app.

The values specific to a home (addresses, domain, tokens) live in two git-ignored files:
`core/.env` on the Pi and `k8s/cluster.env` on the machine you run the cluster script from. Each
has an `.example` beside it that explains every value.

## Set up the Pi

Give the Pi a stable address (a DHCP reservation or a static address), then:

```bash
cp core/.env.example core/.env && $EDITOR core/.env
core/setup.sh
```

Point the router's DHCP DNS server at the Pi so every device resolves the domain through Blocky.

Home Assistant keeps its HTTP settings outside the repo. Open it once directly at
`http://<Pi address>:8123`, go to Settings → System → Network, turn on the reverse proxy
(X-Forwarded-For), and trust `172.16.0.0/12`, the range Docker gives Traefik's network. Until then,
`https://ha.<domain>` answers 400.

Away from home, devices reach the lab over NetBird. `setup.sh` points the public records for the
domain and `*.` the domain at the Pi's NetBird address, so they resolve anywhere without clashing
with the network you are on; at home Blocky answers with the Pi's home address. Re-run
`setup.sh` if the Pi ever gets a new NetBird address.

## Set up the cluster

Give every node a stable address, fill in `k8s/cluster.env`, then install k3s on each machine
over SSH. Start with the server; add `--media` to every node that holds the media library:

```bash
cp k8s/cluster.env.example k8s/cluster.env && $EDITOR k8s/cluster.env
k8s/lab.sh node server --media <user@server>
export KUBECONFIG=$PWD/k8s/kubeconfig
k8s/lab.sh node agent <user@node>
```

Fill in the `secrets.env` of every folder you want running (copy its `secrets.env.example`; the
comments say where each value comes from), then deploy:

```bash
k8s/lab.sh apply              # everything that is configured; prints what it skipped
k8s/lab.sh apply sure arr     # only these apps
k8s/lab.sh apply --strict     # everything, or stop if anything is not configured
```

`apply` always applies the namespaces and the infrastructure first. Re-run it after any change.

| Folder | What | Namespace | Needs `secrets.env` |
|---|---|---|---|
| `k8s/infra/kube-vip` | The cluster's floating address | `kube-system` | no |
| `k8s/infra/cert-manager` | Wildcard certificate for the domain | `cert-manager` | Cloudflare token |
| `k8s/infra/media-library` | The library on the media nodes | `media` | no |
| `k8s/apps/jellyfin` | Jellyfin | `media` | no |
| `k8s/apps/audiobookshelf` | Audiobookshelf | `media` | no |
| `k8s/apps/arr` | Jellyseerr, Sonarr, Radarr, Lidarr, Bazarr, Prowlarr, FlareSolverr, qBittorrent and nzbget behind a VPN | `media` | Proton VPN keys |
| `k8s/apps/sure` | Sure, with nightly database dumps | `finance` | app secrets |

## Where the data lives

| Where | What |
|---|---|
| Pi, `$DATA_DIR/traefik/letsencrypt` | The Pi's certificates |
| Pi, `$DATA_DIR/homeassistant` | Home Assistant's database |
| Pi, `core/homeassistant`, `core/glance` | Home Assistant and Glance settings, in the repo |
| Media nodes, `/data/media` | The media library |
| Cluster volumes | Each app's settings. `kubectl get pv` lists them; k3s keeps them under `/var/lib/rancher/k3s/storage` on the node that runs the app |

## Add an app

- **In the cluster:** a folder in `k8s/apps` with a `kustomization.yaml`, and an Ingress for
  `<name>.lab.alamst.me`. Apps that need the library get the node selector
  `lab.alamst.me/media: "true"` and mount the `media-library` claim.
- **On the Pi:** a service in `core/docker-compose.yml` on the `proxy` network, with the labels
  `traefik.enable=true`, ``traefik.http.routers.<name>.rule=Host(`<name>.${DOMAIN}`)`` and
  `traefik.http.routers.<name>.entrypoints=websecure`.

## Add a media node

Run `k8s/lab.sh node agent --media <user@node>`. A pod only sees the disk of the node it runs on,
so the new node needs the same library at `/data/media`, from a shared disk or a NAS mounted
there.

## Moving from the old Compose stack

Copy the old stack's `media/` folder to this machine, deploy the arr stack, then load each app's
old settings into its new volume:

```bash
k8s/lab.sh import-arr <path to old media folder>
```

The settings still point at the old fixed container addresses. Change them in each app (download
clients in Sonarr, Radarr and Lidarr; apps and the FlareSolverr proxy in Prowlarr; Sonarr and
Radarr in Bazarr; Jellyfin, Sonarr and Radarr in Jellyseerr):

| Old address | New address |
|---|---|
| `172.39.0.2:8080` (qBittorrent) | `downloads:8080` |
| `172.39.0.2:6789` (nzbget) | `downloads:6789` |
| `172.39.0.3:8989`, `.4:7878`, `.5:8686`, `.6:6767`, `.7:9696` | `sonarr:80`, `radarr:80`, `lidarr:80`, `bazarr:80`, `prowlarr:80` |
| `172.39.0.9:8191` (FlareSolverr) | `flaresolverr:8191` |
| Jellyfin, port 8096 | `jellyfin:80` |
