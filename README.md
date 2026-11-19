# Homelab

Everything runs under `lab.alamst.me`, on two kinds of machine:

- **The Raspberry Pi** (`core/`, Docker Compose): DNS (Blocky), the front proxy (Traefik), the
  dashboard (Glance at https://lab.alamst.me) and Home Assistant (https://ha.lab.alamst.me).
- **The k3s cluster** (`k8s/`): every other app. Three nodes, `k3s-01` to `k3s-03`; `k3s-01` is
  the media node and holds the media library at `/data/media`.

Blocky answers `lab.alamst.me` and every name under it with the Pi's address. The Pi's Traefik
serves its own apps and forwards every other name to the cluster's Traefik. So adding an app
never touches DNS: on the Pi, give it a Traefik router label; in the cluster, give it an Ingress
for `<name>.lab.alamst.me`.

| App | Address | Runs on |
|---|---|---|
| Glance | https://lab.alamst.me | Pi |
| Home Assistant | https://ha.lab.alamst.me | Pi |
| Traefik dashboard (Pi) | https://traefik.lab.alamst.me | Pi |
| Jellyfin | https://jellyfin.lab.alamst.me | `k3s-01` |
| Audiobookshelf | https://books.lab.alamst.me | `k3s-01` |
| Jellyseerr | https://jellyseerr.lab.alamst.me | cluster |
| Sonarr, Radarr, Lidarr, Bazarr | https://sonarr.lab.alamst.me and so on | `k3s-01` |
| Prowlarr | https://prowlarr.lab.alamst.me | cluster |
| qBittorrent | https://qb.lab.alamst.me | `k3s-01`, behind the VPN |
| nzbget | https://nzbget.lab.alamst.me | `k3s-01`, behind the VPN |
| Sure | https://sure.lab.alamst.me | cluster |

## 1. The home network

1. In the router, reserve fixed addresses: the Pi at `192.168.1.14`, the nodes at
   `192.168.1.11`, `.12` and `.13`.
2. Once the Pi is up (step 2), set the router's DHCP DNS server to the Pi, so every device
   resolves `lab.alamst.me` through Blocky.

## 2. The Raspberry Pi

Needs Docker with the Compose plugin, and a Cloudflare API token with Zone / DNS / Edit on
`alamst.me` (dash.cloudflare.com/profile/api-tokens). Traefik uses it to get one wildcard
certificate for `*.lab.alamst.me`.

1. Fill in the settings for this home. `core/.env` is the only file that holds addresses, the
   domain, the timezone and the token; each value is explained in the example:

   ```bash
   cd core
   cp .env.example .env
   $EDITOR .env
   ```

2. Create the folders that live outside the repo, and start everything:

   ```bash
   sudo mkdir -p /data/traefik/letsencrypt /data/homeassistant
   docker compose up -d
   ```

3. Check it:

   ```bash
   dig +short anything.lab.alamst.me @192.168.1.14   # 192.168.1.14
   docker compose logs traefik | grep -i certificate  # the wildcard certificate was issued
   ```

   Then open https://lab.alamst.me.

**Moving house:** change the values in `core/.env` and run `docker compose up -d` again.

## 3. The cluster

1. Install k3s on `k3s-01` with its defaults, which include the Traefik and ServiceLB the setup
   relies on, then join the other two nodes with the token it prints:

   ```bash
   # on k3s-01
   curl -sfL https://get.k3s.io | sh -
   sudo cat /var/lib/rancher/k3s/server/node-token

   # on k3s-02 and k3s-03
   curl -sfL https://get.k3s.io | K3S_URL=https://192.168.1.11:6443 K3S_TOKEN=<token> sh -
   ```

   Copy `/etc/rancher/k3s/k3s.yaml` from `k3s-01` to `~/.kube/config` on your laptop and change
   its `server:` to `https://192.168.1.11:6443`.

2. Mark the media node. Apps that need the library or the GPU only run on nodes with this label:

   ```bash
   kubectl label node k3s-01 lab.alamst.me/media=true
   ```

3. Create the namespaces, then cert-manager, which gets the cluster's own wildcard certificate.
   It needs the same Cloudflare token as the Pi:

   ```bash
   kubectl apply -f k8s/namespaces/
   cp k8s/infra/cert-manager/secrets.env.example k8s/infra/cert-manager/secrets.env
   $EDITOR k8s/infra/cert-manager/secrets.env
   kubectl apply -k k8s/infra/cert-manager/
   ```

   The first apply can fail on the certificate while cert-manager is still installing; wait a
   minute and run it again. `kubectl -n kube-system get certificate lab-wildcard` shows
   `READY True` once the certificate is issued.

## 4. The apps

Every `secrets.env` is ignored by git. Copy it from the `secrets.env.example` next to it.

### Jellyfin and Audiobookshelf

Jellyfin goes first: it creates the `media-library` volume that the arr apps also mount.

```bash
kubectl apply -f k8s/apps/jellyfin/
kubectl apply -f k8s/apps/audiobookshelf/
```

### Sure

```bash
cp k8s/apps/sure/secrets.env.example k8s/apps/sure/secrets.env
$EDITOR k8s/apps/sure/secrets.env        # generate each value as the comments say
kubectl apply -k k8s/apps/sure/
kubectl apply -f k8s/apps/sure/50-backup.yaml -f k8s/apps/sure/60-ingress.yaml
```

Never change `SECRET_KEY_BASE` once Sure has data: it encrypts with it.

### Jellyseerr and the arr stack

qBittorrent and nzbget share a pod with gluetun, so all their traffic goes through the Proton VPN.

1. Fill in the VPN settings. If you have the old Compose stack's `media/.env`, copy its
   `VPN_*`, `WIREGUARD_*` and `SERVER_*` lines; `PUID`, `PGID` and `TZ` are already set in the
   manifests. Proton only forwards a port on P2P servers, and the WireGuard config must be
   generated with NAT-PMP turned on.

   ```bash
   cp k8s/apps/arr/secrets.env.example k8s/apps/arr/secrets.env
   $EDITOR k8s/apps/arr/secrets.env
   ```

2. Deploy, and wait until every pod is ready. The downloads pod starts qBittorrent and nzbget only
   once the VPN is up, so it takes a minute:

   ```bash
   kubectl apply -k k8s/apps/arr/
   kubectl -n media get pods -w
   ```

3. Check the VPN. The first command shows Proton's address, not your home's; the second shows the
   port Proton forwarded, which should match qBittorrent's Settings → Connection:

   ```bash
   kubectl -n media exec deploy/downloads -c gluetun -- wget -qO- http://localhost:8000/v1/publicip/ip
   kubectl -n media logs deploy/downloads -c gluetun | grep -i 'port forward'
   ```

The apps start empty. To keep their history and settings, see
[Bringing settings over from the old Compose stack](#bringing-settings-over-from-the-old-compose-stack).
Otherwise, connect them to each other by these names:

| App | Address inside the cluster |
|---|---|
| qBittorrent | `downloads:8080` |
| nzbget | `downloads:6789` |
| Sonarr, Radarr, Lidarr, Bazarr, Prowlarr, Jellyfin | `sonarr:80`, `radarr:80` and so on |
| FlareSolverr | `flaresolverr:8191` |

## Adding an app

- **In the cluster:** a Deployment, a Service and an Ingress for `<name>.lab.alamst.me` with
  `ingressClassName: traefik`. The wildcard certificate covers it; DNS already points at it.
- **On the Pi:** a service in `core/docker-compose.yml` on the `proxy` network, with the labels
  `traefik.enable=true`, ``traefik.http.routers.<name>.rule=Host(`<name>.${DOMAIN}`)`` and
  `traefik.http.routers.<name>.entrypoints=websecure`.

## Bringing settings over from the old Compose stack

Each arr app keeps its settings in a volume the cluster creates the first time the app starts.
To reuse the old ones, copy the old folder (`sonarr/`, `radarr/`, `qbittorrent/` and so on,
next to the old `media/docker-compose.yml`) into that volume.

1. Pack the old folders on the old machine. `sudo tar` keeps the file owners the apps need:

   ```bash
   cd <old homelab checkout>/media
   sudo tar -czpf ~/arr-configs.tgz \
     jellyseerr sonarr radarr lidarr bazarr prowlarr qbittorrent nzbget
   ```

2. For each app, find which node holds its volume and where:

   ```bash
   app=sonarr
   pv=$(kubectl -n media get pvc $app-config -o jsonpath='{.spec.volumeName}')
   kubectl get pv $pv -o jsonpath='{.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]} {.spec.hostPath.path}{.spec.local.path}{"\n"}'
   # k3s-01 /var/lib/rancher/k3s/storage/pvc-..._media_sonarr-config
   ```

3. Stop the app, replace the folder's contents on that node, and start it again. qBittorrent and
   nzbget are both in the `downloads` deployment:

   ```bash
   kubectl -n media scale deploy/sonarr --replicas=0

   # on the node printed above, with the archive copied there
   sudo rm -rf <path>/*
   sudo tar -xzpf ~/arr-configs.tgz -C <path> --strip-components=1 sonarr

   kubectl -n media scale deploy/sonarr --replicas=1
   ```

4. The copied settings still point at the old fixed container addresses. Change them in each app
   (download clients in Sonarr, Radarr and Lidarr; apps and the FlareSolverr proxy in Prowlarr;
   Sonarr and Radarr in Bazarr; Jellyfin, Sonarr and Radarr in Jellyseerr):

   | App | Old address | New address |
   |---|---|---|
   | qBittorrent | `172.39.0.2:8080` | `downloads:8080` |
   | nzbget | `172.39.0.2:6789` | `downloads:6789` |
   | Sonarr | `172.39.0.3:8989` | `sonarr:80` |
   | Radarr | `172.39.0.4:7878` | `radarr:80` |
   | Lidarr | `172.39.0.5:8686` | `lidarr:80` |
   | Bazarr | `172.39.0.6:6767` | `bazarr:80` |
   | Prowlarr | `172.39.0.7:9696` | `prowlarr:80` |
   | FlareSolverr | `172.39.0.9:8191` | `flaresolverr:8191` |
   | Jellyfin | the host's address, port 8096 | `jellyfin:80` |

## When something goes wrong

- **A name doesn't resolve.** Check the device uses the Pi for DNS:
  `dig +short <name>.lab.alamst.me` should print the Pi's address.
- **A cluster app shows a certificate error or a 404.** The Pi forwarded it but the cluster has
  no Ingress for that name, or its certificate is not ready:
  `kubectl get ingress -A` and `kubectl -n kube-system get certificate lab-wildcard`.
- **The downloads pod is stuck at `Init`.** gluetun cannot connect; it is almost always a key or
  server setting in `secrets.env`. Read `kubectl -n media logs deploy/downloads -c gluetun`, fix
  the file, then run `kubectl apply -k k8s/apps/arr/` and
  `kubectl -n media rollout restart deploy/downloads`.
- **qBittorrent restarts every few minutes.** It cannot reach the internet through the tunnel,
  and the cluster restarts it until it can.
- **A media app shows no media.** The library is only on `k3s-01`; check the pod runs there with
  `kubectl -n media get pods -o wide`.

## Upgrades

Cluster images are pinned to a version in their manifests. To upgrade one, read its release
notes, change the tag, and apply that folder again. On the Pi, `docker compose pull` and
`docker compose up -d`.
