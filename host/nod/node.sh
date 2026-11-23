#!/usr/bin/env bash
# Root-only host helper; never source an installation .env or emit upstream logs.
set -euo pipefail
umask 077
ROOT=/opt/nod
BACKUPS=/var/lib/nod/backups
REV=940c88a81c520d96caeb3df63c29d8605ed5b429
ZIP_SHA=857795b1083a3874c6dd58df285681dc74b236661073d6a9ba15e0b36ead8ae6
BUNDLE=nod-server-linux-amd64-$REV
fail() { printf 'nod host: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || fail 'Requires root via sudo -n.'
command=${1:-}; shift || true
valid_id() { [[ "$1" =~ ^nod-[a-z0-9-]+$ ]] || fail 'Invalid generation identifier.'; }
valid_ip() {
  python3 -c 'import ipaddress,sys; a=ipaddress.ip_address(sys.argv[1]); assert a.version==4 and (a in ipaddress.ip_network("10.0.0.0/8") or a in ipaddress.ip_network("172.16.0.0/12") or a in ipaddress.ip_network("192.168.0.0/16"))' "$1" || fail 'Requires an RFC1918 IPv4 InternalIP.'
}
valid_hostname() {
  python3 - "$1" <<'PY' || fail 'NOD_HOSTNAME must be a lowercase DNS hostname, not a URL or IP address.'
import ipaddress,re,sys
host=sys.argv[1]
assert len(host)<=253 and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split("."))
try:
    ipaddress.ip_address(host)
except ValueError:
    pass
else:
    raise ValueError("IP address is not a DNS hostname")
PY
}
# Reject absent/unsafe hostnames before creating directories, locks or instances.
case "$command" in
  deploy|restart) valid_hostname "${3:-}" ;;
  restore) valid_hostname "${4:-}" ;;
esac
current() {
  [ -L "$ROOT/current" ] || fail 'No managed current installation.'
  generation=$(basename "$(readlink -f "$ROOT/current")")
  valid_id "$generation"
  [ "$(readlink -f "$ROOT/current")" = "$ROOT/instances/$generation" ] || fail 'Invalid current link.'
}
instance() {
  valid_id "$1"; generation=$1; directory=$ROOT/instances/$generation
  [ -d "$directory" ] || fail 'Missing managed generation.'
  [ "$(cat "$directory/source-revision")" = "$REV" ] || fail 'Revision mismatch.'
  cd "$directory"
}
compose() { docker compose --file compose.yaml --env-file .env "$@"; }
quiet() {
  local label=${1##*/}
  case "$label" in sh|compose) label="$label ${2:-}" ;; esac
  "$@" >>"$operation_log" 2>&1 || fail "$label failed; private diagnostics retained at $operation_log (root only)."
}
new_instance() {
  local staged
  valid_id "$1"
  [ ! -e "$ROOT/instances/$1" ] || fail 'Generation already exists.'
  staged=$(mktemp -d "$ROOT/instances/.$1.XXXXXXXX")
  quiet cp -a "$ROOT/bundle/." "$staged/"
  # No scripts run until the complete copy is published on the same filesystem.
  mv -T "$staged" "$ROOT/instances/$1"
}
configure() {
  valid_ip "$1"
  valid_hostname "$2"
  python3 - "$1" "$2" <<'PY'
import os,sys
from pathlib import Path
p=Path('.env')
assert p.is_file() and not p.is_symlink()
lines=p.read_text().splitlines()
keys=('NOD_BIND_ADDRESS','NOD_PUBLIC_URL')
lines=[line for line in lines if not any(line.startswith(k+'=') for k in keys)]
lines += ['NOD_BIND_ADDRESS='+sys.argv[1], 'NOD_PUBLIC_URL=https://'+sys.argv[2]]
tmp=Path('.env.new'); fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f: f.write('\n'.join(lines)+'\n')
os.replace(tmp,p)
PY
}
health() {
  local service container state
  for service in server postgres runtime browser workspace workspace-network; do
    container=$(compose ps --all --quiet "$service")
    [ -n "$container" ] || fail "Missing $service container."
    state=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container")
    case "$service:$state" in
      server:running\ healthy|postgres:running\ healthy|browser:running\ healthy|workspace-network:running\ healthy|runtime:running\ none|workspace:running\ none) ;;
      *) fail "$service is not ready ($state)." ;;
    esac
  done
}
resources() { free -m; df -h /opt /var/lib; }
port_free() {
  command -v ss >/dev/null || fail 'Requires iproute2 for port collision detection.'
  [ -z "$(ss -H -ltn 'sport = :4310')" ] || fail 'TCP port 4310 is occupied; refusing a second active stack.'
}
preflight() {
  [ "$(uname -m)" = x86_64 ] || fail 'Only the pinned AMD64 bundle is supported.'
  # Target-only Debian release metadata; this file need not exist on the controller.
  # shellcheck disable=SC1091
  . /etc/os-release
  [ "$ID" = debian ] && [ "$VERSION_ID" = 13 ] || fail 'Requires Debian 13; no repository changes are performed.'
  command -v python3 >/dev/null || fail 'Install python3 through the approved host provisioning first.'
  command -v openssl >/dev/null || fail 'Install openssl through the approved host provisioning first.'
  [ "$(sysctl -n net.ipv4.ip_forward)" = 1 ] || fail 'IPv4 forwarding must already be enabled for k3s.'
  # The ZIP, extracted image bundle, loaded images and a second generation coexist.
  [ "$(df -Pk /opt | awk 'NR==2 {print $4}')" -ge 12582912 ] || fail 'Requires at least 12 GiB free in /opt for bundle staging and image loading.'
  [ "$(df -Pk /var/lib | awk 'NR==2 {print $4}')" -ge 12582912 ] || fail 'Requires at least 12 GiB free in /var/lib; data backups may require more.'
  if command -v docker >/dev/null || dpkg-query -W -f='${Status}' docker.io 2>/dev/null | grep -q 'install ok installed'; then
    [ -f "$ROOT/docker-managed" ] || fail 'Existing Docker is not managed by Nod; refusing replacement or reconfiguration.'
    [ "$(dpkg-query -W -f='${Version}' docker.io)" = '26.1.5+dfsg1-9+deb13u1+b1' ] || fail 'Managed Docker package differs from approved version.'
    [ "$(dpkg-query -W -f='${Version}' docker-compose)" = '2.26.1-4' ] || fail 'Managed Compose package differs from approved version.'
    docker info >/dev/null 2>&1 || fail 'Managed Docker is not running.'
    return
  fi
  [ ! -e /etc/docker/daemon.json ] && [ ! -d /var/lib/docker ] || fail 'Existing Docker configuration/data requires operator review.'
  command -v iptables >/dev/null || fail 'iptables is required to capture the k3s forwarding policy.'
}
bootstrap() {
  preflight
  [ ! -f "$ROOT/docker-managed" ] || return 0
  local before after plan
  before=$(iptables -S FORWARD | grep '^-P FORWARD ')
  plan=$(LC_ALL=C apt-get --simulate --no-upgrade install docker.io=26.1.5+dfsg1-9+deb13u1+b1 docker-compose=2.26.1-4)
  printf '%s\n' "$plan" | grep -q '^0 upgraded, [0-9][0-9]* newly installed, 0 to remove' || fail 'Docker apt plan would upgrade/remove existing packages; review manually.'
  # Preserve evidence if installation fails; partial installation needs review.
  printf '%s\n' "$before" > "$ROOT/forward-policy-before"
  quiet env DEBIAN_FRONTEND=noninteractive apt-get --yes --no-upgrade install docker.io=26.1.5+dfsg1-9+deb13u1+b1 docker-compose=2.26.1-4
  after=$(iptables -S FORWARD | grep '^-P FORWARD ')
  [ "$before" = "$after" ] || fail 'Docker changed the FORWARD policy. Stop and review k3s networking; no policy was silently rewritten.'
  printf '%s\n' "$after" > "$ROOT/forward-policy-after"
  touch "$ROOT/docker-managed"
}
# Read-only status does not initialize directories, install packages or take a lock.
if [ "$command" = status ]; then
  current; instance "$generation"
  printf 'generation=%s\nrevision=%s\n' "$generation" "$REV"
  compose ps --all --format '{{.Service}} {{.State}} {{.Health}}'
  resources
  exit 0
fi
if [ "$command" = current ]; then current; printf '%s\n' "$generation"; exit 0; fi
if [ "$command" = export-bundle ]; then
  [ "$(sha256sum "$ROOT/artifact.zip" | cut -d ' ' -f1)" = "$ZIP_SHA" ] || fail 'Cached ZIP failed verification.'
  cat "$ROOT/artifact.zip"; exit 0
fi
install -d -m 700 "$ROOT" "$ROOT/instances" "$BACKUPS"
exec 9>"$ROOT/lifecycle.lock"
flock -n 9 || fail 'Another host lifecycle operation is running.'
operation_log=$(mktemp "$ROOT/operation-XXXXXXXX")
chmod 600 "$operation_log"
case "$command" in
  preflight)
    preflight
    if [ "${1:-}" = fresh ]; then port_free; fi
    resources
    ;;
  receive-bundle)
    [ ! -e "$ROOT/artifact.zip.part" ] || rm -f "$ROOT/artifact.zip.part"
    cat > "$ROOT/artifact.zip.part"
    [ "$(sha256sum "$ROOT/artifact.zip.part" | cut -d ' ' -f1)" = "$ZIP_SHA" ] || fail 'Transferred ZIP checksum mismatch.'
    mv "$ROOT/artifact.zip.part" "$ROOT/artifact.zip"
    ;;
  prepare)
    bootstrap
    [ "$(sha256sum "$ROOT/artifact.zip" | cut -d ' ' -f1)" = "$ZIP_SHA" ] || fail 'Verified immutable ZIP is required.'
    if [ ! -d "$ROOT/bundle" ]; then
      staging=$(mktemp -d "$ROOT/bundle.XXXXXXXX")
      python3 - "$ROOT/artifact.zip" "$staging" "$BUNDLE" <<'PY'
import hashlib,sys,tarfile,zipfile
from pathlib import Path
archive,stage,bundle=sys.argv[1:]; stage=Path(stage)
with zipfile.ZipFile(archive) as z:
    expected={bundle+'.tar.gz',bundle+'.tar.gz.sha256'}
    assert set(z.namelist())==expected, 'Unexpected ZIP contents'
    z.extractall(stage)
tar=stage/(bundle+'.tar.gz')
expected=(stage/(bundle+'.tar.gz.sha256')).read_text().split()[0]
h=hashlib.sha256()
with tar.open('rb') as f:
    for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
assert h.hexdigest()==expected, 'Inner archive checksum mismatch'
with tarfile.open(tar) as t: t.extractall(stage,filter='data')
PY
      [ "$(cat "$staging/nod-server-linux-amd64/source-revision")" = "$REV" ] || fail 'Bundle revision mismatch.'
      [ "$(cat "$staging/nod-server-linux-amd64/architecture")" = amd64 ] || fail 'Bundle architecture mismatch.'
      (cd "$staging/nod-server-linux-amd64" && quiet sha256sum --check checksums.sha256)
      mv "$staging/nod-server-linux-amd64" "$ROOT/bundle"
      rm -rf "$staging"
    fi
    (cd "$ROOT/bundle" && quiet sha256sum --check checksums.sha256)
    ;;
  deploy)
    generation=$1; ip=$2; valid_id "$generation"; valid_ip "$ip"
    if [ -L "$ROOT/current" ]; then
      current; instance "$generation"
    else
      [ ! -e "$ROOT/instances/$generation" ] || fail 'Generation already exists; review interrupted deployment before resuming.'
      port_free
      new_instance "$generation"
      instance "$generation"
      quiet sh setup.sh
    fi
    [ ! -e fenced ] || fail 'Generation is fenced; use explicit rollback for preserved source only.'
    configure "$ip" "$3"
    quiet sh install.sh
    health
    ln -sfn "$directory" "$ROOT/current"
    printf '%s\n' "$generation"
    ;;
  backup|stop-backup)
    instance "$1"; id=$2; valid_id "$id"
    [ ! -e "$BACKUPS/$id" ] || fail 'Backup path already exists; incomplete backup retained for inspection.'
    if [ "$command" = stop-backup ]; then quiet compose stop server browser workspace; fi
    quiet sh backup.sh "$BACKUPS/$id"
    if [ "$command" = stop-backup ]; then
      quiet compose down
      touch fenced
    fi
    printf '%s\n' "$BACKUPS/$id"
    ;;
  export-backup)
    valid_id "$1"; [ -f "$BACKUPS/$1/complete" ] || fail 'No complete backup.'
    tar -C "$BACKUPS/$1" -czf - .
    ;;
  receive-backup)
    valid_id "$1"; [ ! -e "$BACKUPS/$1" ] || fail 'Destination backup already exists.'
    mkdir "$BACKUPS/$1"
    tar -C "$BACKUPS/$1" --no-same-owner -xzf -
    chmod 700 "$BACKUPS/$1"; chmod 600 "$BACKUPS/$1/.env"
    [ "$(cat "$BACKUPS/$1/complete")" = nod-self-host-backup-v1 ] || fail 'Incomplete transferred snapshot.'
    ;;
  restore)
    generation=$1; id=$2; ip=$3; valid_id "$generation"; valid_id "$id"; valid_ip "$ip"
    [ ! -e "$ROOT/instances/$generation" ] || fail 'Restore requires a fresh generation.'
    port_free
    new_instance "$generation"; instance "$generation"
    quiet sh restore.sh "$BACKUPS/$id" "$generation"
    configure "$ip" "$4"
    [ ! -e fenced ] || fail 'Restore generation was fenced.'
    quiet sh install.sh
    health
    ln -sfn "$directory" "$ROOT/current"
    ;;
  fence)
    valid_id "$1"
    # Restore always uses the operation ID as its exact Compose project name.
    # Fence by that label, not by potentially incomplete copied metadata/.env.
    if command -v docker >/dev/null; then
      containers=$(docker ps --all --quiet --filter "label=com.docker.compose.project=$1")
      for container in $containers; do
        quiet docker stop "$container"
        quiet docker rm "$container"
      done
      containers=$(docker ps --all --quiet --filter "label=com.docker.compose.project=$1")
      [ -z "$containers" ] || fail 'Target project containers remain; source must stay stopped.'
    else
      [ ! -e /var/lib/docker ] || fail 'Cannot prove target stopped without its Docker client.'
    fi
    # Idempotent even if interrupted between mkdir and the tombstone write.
    mkdir -p "$ROOT/instances/$1"
    touch "$ROOT/instances/$1/fenced"
    ;;
  restart)
    instance "$1"; configure "$2" "$3"
    rm -f fenced
    quiet sh install.sh
    health
    ln -sfn "$directory" "$ROOT/current"
    ;;
  *) fail 'Usage: node.sh status|current|preflight|receive-bundle|prepare|deploy|backup|stop-backup|export-backup|receive-backup|restore|fence|restart (controller supplies arguments).' ;;
esac
