# Media automation (Jellyseerr and the arr stack)

Jellyseerr, Sonarr, Radarr, Lidarr, Bazarr, Prowlarr, FlareSolverr, qBittorrent and nzbget,
running in the cluster's `media` namespace. qBittorrent and nzbget share one pod with gluetun, so
all their traffic goes through the Proton VPN. The apps that touch the library (Sonarr, Radarr,
Lidarr, Bazarr and the downloaders) run on the media node, `k3s-01`, and see it at `/data`, the
same path as in the old Compose stack.

| App | Address |
|---|---|
| Jellyseerr | https://jellyseerr.lab.alamst.me |
| Sonarr | https://sonarr.lab.alamst.me |
| Radarr | https://radarr.lab.alamst.me |
| Lidarr | https://lidarr.lab.alamst.me |
| Bazarr | https://bazarr.lab.alamst.me |
| Prowlarr | https://prowlarr.lab.alamst.me |
| qBittorrent | https://qb.lab.alamst.me |
| nzbget | https://nzbget.lab.alamst.me |

FlareSolverr has no web address; Prowlarr reaches it inside the cluster.

## First-time setup

1. Make sure the library is at `/data/media` on `k3s-01` and Jellyfin is deployed. Jellyfin
   creates the `media-library` volume that every app here mounts:

   ```bash
   kubectl apply -f k8s/namespaces/media.yaml
   kubectl apply -f k8s/apps/jellyfin/
   ```

2. Fill in the VPN settings. Copy the example and paste the gluetun lines from the old
   `media/.env` (the `VPN_*`, `WIREGUARD_*` and `SERVER_*` ones; `PUID`, `PGID` and `TZ` are set
   in the manifests):

   ```bash
   cp k8s/apps/arr/secrets.env.example k8s/apps/arr/secrets.env
   $EDITOR k8s/apps/arr/secrets.env
   ```

   `secrets.env` is ignored by git. Proton only forwards a port on P2P servers, and the
   WireGuard config must be generated with NAT-PMP turned on.

3. Deploy:

   ```bash
   kubectl apply -k k8s/apps/arr/
   kubectl -n media get pods -w        # wait until every pod is Running and ready
   ```

   The downloads pod waits for the VPN before it starts qBittorrent and nzbget, so it can take a
   minute.

## Bring over settings from the old Compose stack

Each app starts empty. To keep its history, indexers and settings, copy its old folder (`sonarr/`,
`radarr/`, `qbittorrent/` and so on, next to the old `media/docker-compose.yml`) into the
volume the cluster created for it. Do it after the first deploy, because the volume only exists
once the app has started.

1. Pack the old folders on the old machine. `sudo tar` keeps the file owners (UID 1000), which
   the apps need:

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
   nzbget both live in the `downloads` deployment:

   ```bash
   kubectl -n media scale deploy/sonarr --replicas=0

   # on the node printed above, with the archive copied there
   sudo rm -rf <path>/*
   sudo tar -xzpf ~/arr-configs.tgz -C <path> --strip-components=1 sonarr

   kubectl -n media scale deploy/sonarr --replicas=1
   ```

4. Update the addresses the apps use to reach each other. The old stack used fixed container IPs
   and ports; in the cluster each app has a name and listens on port 80, except the downloaders
   and FlareSolverr:

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
   | Jellyfin | host IP, port 8096 | `jellyfin:80` |

   Where to change them: download clients in Sonarr, Radarr and Lidarr; apps and the
   FlareSolverr proxy in Prowlarr; Sonarr and Radarr in Bazarr; Jellyfin, Sonarr and Radarr in
   Jellyseerr.

## Check it works

```bash
# the VPN is up and shows Proton's address, not your home's
kubectl -n media exec deploy/downloads -c gluetun -- wget -qO- http://localhost:8000/v1/publicip/ip

# the port Proton forwarded, which gluetun also writes into qBittorrent
kubectl -n media logs deploy/downloads -c gluetun | grep -i 'port forward'
```

In qBittorrent, Settings → Connection should show that same listening port.

## When something goes wrong

- **The downloads pod is stuck at `Init`.** gluetun cannot connect. Read
  `kubectl -n media logs deploy/downloads -c gluetun`; it is almost always a key or server
  setting in `secrets.env`. After fixing it, run `kubectl apply -k k8s/apps/arr/` and
  `kubectl -n media rollout restart deploy/downloads`.
- **qBittorrent restarts every few minutes.** It cannot reach the internet through the tunnel,
  and the cluster restarts it until it can (the job deunhealth used to do).
- **An app shows no media.** The library is only on `k3s-01`; check the pod runs there with
  `kubectl -n media get pods -o wide`.

## Upgrades

Every image is pinned to a version in its manifest. To upgrade an app, read its release notes,
change the tag, and apply again:

```bash
kubectl apply -k k8s/apps/arr/
```
