#!/usr/bin/env bash
# Host-native upstream Nod; cluster resources contain routing/operation metadata only.
set -euo pipefail
umask 077
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
. "$HERE/release.env"
SSH_USER=${NOD_SSH_USER:-}
SSH_KEY=${NOD_SSH_KEY:-}
CONTROL=${NOD_CONTROL_HOST:-}
HOSTNAME=${NOD_HOSTNAME:-}
CACHE=${HOME}/.cache/homelab/nod/$NOD_REVISION
ZIP=$CACHE/artifact.zip
SSH=(ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o UpdateHostKeys=no -i "$SSH_KEY")
fail() { printf 'nod: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'HELP'
Usage: host/nod/nod.sh deploy [NODE] | status | backup | move NODE | rollback
  deploy   Install the pinned upstream bundle on NODE (or NOD_DEFAULT_NODE), or
           reconcile that host without replacing keys/data. Refuses another host.
  status   Read authoritative EndpointSlice placement, pending operation, host
           service health and resources; never installs anything.
  backup   Run upstream backup on the routed host; briefly pauses the assistant.
  move     Prepare a Ready node, stop and back up source, transfer/restore into a
           fresh generation, health-check then switch the existing private URL.
  rollback Recover an interrupted move only: fence target before restarting the
           preserved source. Never use concurrently with a live move process.

Mutation commands perform real changes when invoked; --help and status are safe
planning entrypoints. They require approved root Docker installation on a clean
Debian 13 AMD64 node, trusted SSH host keys, sudo -n and a Ready k3s control node.
Local prerequisites: Bash, ssh, python3, tar; gh only for the first artifact fetch.
No creds.env, kubeconfig, key copying, Docker group grants or lab.sh apply.
Required environment: NOD_SSH_USER, NOD_SSH_KEY (path), NOD_CONTROL_HOST,
NOD_HOSTNAME (DNS hostname only). NOD_DEFAULT_NODE is optional for deploy.
Secrets and snapshots stay root-private on hosts. The immutable verified ZIP is
retained in ~/.cache/homelab/nod/<revision>/artifact.zip and /opt/nod/artifact.zip.
An ambiguous failure retains nod/nod-operation; status shows its metadata. Move
failures require rollback before retry. Other failed operations require operator
inspection and explicit removal of that ConfigMap before retry; never clear a
marker while any controller or remote operation is still running.
HELP
}
case "${1:---help}" in -h|--help|help) usage; exit 0 ;; esac
command=$1; shift
case "$command:$#" in deploy:0|deploy:1|status:0|backup:0|move:1|rollback:0) ;; *) usage >&2; exit 2 ;; esac
for tool in ssh python3 tar; do command -v "$tool" >/dev/null || fail "Missing local prerequisite: $tool"; done
for setting in NOD_SSH_USER NOD_SSH_KEY NOD_CONTROL_HOST NOD_HOSTNAME; do
  [ -n "${!setting:-}" ] || fail "Required environment variable: $setting"
done
[ -f "$SSH_KEY" ] && [ -r "$SSH_KEY" ] || fail 'NOD_SSH_KEY must name a readable key file.'
[[ "$SSH_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || fail 'Invalid SSH user.'
[[ "$CONTROL" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || fail 'Invalid control hostname.'
valid_node() { [[ "$1" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || fail 'Use a registered node name, not an address or hostname.'; }
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
valid_hostname "$HOSTNAME"
deploy_target=
if [ "$command" = deploy ]; then
  deploy_target=${1:-${NOD_DEFAULT_NODE:-}}
  [ -n "$deploy_target" ] || fail 'deploy requires NODE or NOD_DEFAULT_NODE.'
  valid_node "$deploy_target"
elif [ "$command" = move ]; then
  valid_node "$1"
fi
kube() { "${SSH[@]}" "$SSH_USER@$CONTROL" sudo -n k3s kubectl "$@"; }
remote() {
  local node=$1; shift; valid_node "$node"
  "${SSH[@]}" "$SSH_USER@$node.local" sudo -n /opt/nod/node.sh "$@"
}
read_host() {
  local node=$1; shift; valid_node "$node"
  "${SSH[@]}" "$SSH_USER@$node.local" sudo -n bash -s -- "$@" < "$HERE/node.sh"
}
upload_helper() {
  valid_node "$1"
  # Expand the temporary filename only in the remote root shell.
  # shellcheck disable=SC2016
  "${SSH[@]}" "$SSH_USER@$1.local" 'sudo -n bash -c '\''umask 077; install -d -m 700 /opt/nod; f=$(mktemp /opt/nod/helper.XXXXXXXX); cat > "$f"; chmod 700 "$f"; mv "$f" /opt/nod/node.sh'\''' < "$HERE/node.sh"
}
node_ip() {
  valid_node "$1"
  kube get node "$1" -o json | python3 -c '
import ipaddress,json,sys
n=json.load(sys.stdin)
assert any(c["type"]=="Ready" and c["status"]=="True" for c in n["status"]["conditions"]), "Target node is not Ready"
assert n["status"]["nodeInfo"]["architecture"]=="amd64", "Pinned bundle requires AMD64"
ips=[a["address"] for a in n["status"]["addresses"] if a["type"]=="InternalIP"]
nets=[ipaddress.ip_network(x) for x in ("10.0.0.0/8","172.16.0.0/12","192.168.0.0/16")]
ips=[a for a in ips if ipaddress.ip_address(a).version==4 and any(ipaddress.ip_address(a) in x for x in nets)]
assert len(ips)==1, "Requires exactly one RFC1918 IPv4 InternalIP"
print(ips[0])'
}
# List calls distinguish absent resources from authentication/transport errors.
placement() {
  kube get endpointslices.discovery.k8s.io -n nod -l kubernetes.io/service-name=nod -o json | python3 -c '
import json,sys
items=[x for x in json.load(sys.stdin)["items"] if x["metadata"]["namespace"]=="nod" and x["metadata"].get("labels",{}).get("kubernetes.io/service-name")=="nod"]
assert len(items)<=1, "Multiple Nod EndpointSlices; routing is ambiguous"
if items:
 s=items[0]; assert s["metadata"]["name"]=="nod-host", "Unmanaged Nod EndpointSlice"
 assert len(s["endpoints"])==1 and len(s["endpoints"][0]["addresses"])==1, "Ambiguous placement"
 e=s["endpoints"][0]; print(e["nodeName"],e["addresses"][0],s["metadata"]["resourceVersion"])
'
}
pending() {
  kube get configmap nod-operation -n nod --ignore-not-found -o json | python3 -c '
import json,sys
text=sys.stdin.read()
if text.strip(): print(json.dumps(json.loads(text)["data"],sort_keys=True))
'
}
field() { python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2],""))' "$1" "$2"; }
route() {
  local node=$1 ip=$2 version=$3 verb=replace
  [ -n "$version" ] || verb=create
  python3 - "$node" "$ip" "$version" <<'PY' | kube "$verb" -f - >/dev/null
import json,sys
node,ip,version=sys.argv[1:]
m={"name":"nod-host","namespace":"nod","labels":{"kubernetes.io/service-name":"nod","endpointslice.kubernetes.io/managed-by":"homelab-nod"}}
if version: m["resourceVersion"]=version
print(json.dumps({"apiVersion":"discovery.k8s.io/v1","kind":"EndpointSlice","metadata":m,"addressType":"IPv4","ports":[{"name":"http","protocol":"TCP","port":4310}],"endpoints":[{"addresses":[ip],"nodeName":node,"conditions":{"ready":True}}]}))
PY
}
ingress() {
  python3 - "$HOSTNAME" <<'PY'
import json,sys
host=sys.argv[1]
print(json.dumps({"apiVersion":"networking.k8s.io/v1","kind":"Ingress","metadata":{"name":"nod","namespace":"nod"},"spec":{"ingressClassName":"traefik","tls":[{"hosts":[host]}],"rules":[{"host":host,"http":{"paths":[{"path":"/","pathType":"Prefix","backend":{"service":{"name":"nod","port":{"name":"http"}}}}]}}]}}))
PY
}
assert_hostname() {
  kube get ingress nod -n nod -o json | python3 -c '
import json,sys
spec=json.load(sys.stdin)["spec"]
host=sys.argv[1]
assert [r.get("host") for r in spec.get("rules",[])]==[host]
assert any(host in entry.get("hosts",[]) for entry in spec.get("tls",[]))
' "$HOSTNAME" || fail 'NOD_HOSTNAME must match the deployed Ingress before move or rollback.'
}
apply_base() {
  # Stream only the three reviewed resources. No kubeconfig or broad apply.
  local manifest
  {
    for manifest in 00-namespace.yaml 30-service.yaml; do
      printf '%s\n' '---'
      cat "$REPO/k8s/apps/nod/$manifest"
    done
    printf '\n%s\n' '---'
    ingress
  } | kube apply -f - >/dev/null
}
verify_zip() {
  python3 - "$ZIP" "$NOD_ZIP_SHA256" <<'PY'
import hashlib,sys
h=hashlib.sha256()
with open(sys.argv[1],'rb') as f:
 for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
assert h.hexdigest()==sys.argv[2], 'Pinned ZIP checksum mismatch; retained for inspection'
PY
}
ensure_bundle() {
  local source=${1:-}
  mkdir -p "$CACHE"
  if [ ! -f "$ZIP" ]; then
    local part
    part=$(mktemp "$CACHE/artifact.XXXXXXXX")
    if [ -n "$source" ]; then
      read_host "$source" export-bundle > "$part"
    else
      command -v gh >/dev/null || fail 'Install/authenticate local gh to fetch the first upstream artifact; credentials never leave the controller.'
      gh api "repos/n8n-io/nod/actions/artifacts/$NOD_ARTIFACT_ID/zip" > "$part"
    fi
    python3 - "$part" "$NOD_ZIP_SHA256" <<'PY'
import hashlib,sys
h=hashlib.sha256()
with open(sys.argv[1],'rb') as f:
 for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
assert h.hexdigest()==sys.argv[2], 'Fetched ZIP checksum mismatch'
PY
    mv "$part" "$ZIP"
  fi
  verify_zip
}
prepare() {
  upload_helper "$1"
  remote "$1" preflight "${2:-fresh}"
  remote "$1" receive-bundle < "$ZIP"
  remote "$1" prepare
}
lock_operation() {
  python3 - "$command" "$id" "$source" "$target" "$source_ip" "$target_ip" "$source_generation" "$version" <<'PY' | kube create -f - >/dev/null
import json,sys
keys=("kind","id","source","target","source-ip","target-ip","source-generation","route-version")
print(json.dumps({"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"nod-operation","namespace":"nod"},"data":dict(zip(keys,sys.argv[1:]))}))
PY
  locked=true
  local locked_placement
  locked_placement=$(placement)
  [ "$locked_placement" = "$active" ] || fail 'Placement changed before operation lock acquisition; marker retained and no host changes performed.'
}
assert_operation() {
  local state
  state=$(pending)
  [ "$(field "$state" id)" = "$id" ] && [ "$(field "$state" kind)" = "$command" ] || fail 'Operation marker changed; refusing further action.'
}
complete() {
  assert_operation
  kube -n nod delete configmap nod-operation >/dev/null
  locked=false
}
locked=false
rollback_locked=false
on_exit() {
  local rc=$?
  if [ "$rollback_locked" = true ]; then
    kube -n nod delete configmap nod-rollback >/dev/null || printf 'nod: Recovery lock remains; inspect nod/nod-rollback before retry.\n' >&2
  fi
  if [ "$rc" -ne 0 ] && [ "$locked" = true ]; then
    printf 'nod: Operation marker retained. Run status; do not restart either host blindly. An interrupted move requires rollback.\n' >&2
  fi
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
state=$(pending)
active=$(placement)
source=; source_ip=; version=; source_generation=; target=; target_ip=
if [ -n "$active" ]; then read -r source source_ip version <<< "$active"; valid_node "$source"; fi
if [ "$command" = status ]; then
  printf 'url=https://%s\n' "$HOSTNAME"
  printf 'placement=%s\npending=%s\n' "${active:-none}" "${state:-none}"
  [ -z "$source" ] || read_host "$source" status
  exit 0
fi
if [ "$command" = rollback ]; then
  [ -n "$state" ] && [ "$(field "$state" kind)" = move ] || fail 'Rollback requires a pending move, not a deployment or normal active placement.'
  assert_hostname
  id=$(field "$state" id); source=$(field "$state" source); target=$(field "$state" target)
  source_ip=$(field "$state" source-ip); source_generation=$(field "$state" source-generation)
  valid_node "$source"; valid_node "$target"
  # A second rollback cannot race this one. Retain the marker on any failure.
  kube -n nod create configmap nod-rollback --from-literal=operation="$id" >/dev/null
  rollback_locked=true
  locked=true
  # A retained marker from a stale lock must not undo another operation.
  active=$(placement)
  if [ "$active" != "$source $source_ip $(field "$state" route-version)" ]; then
    routed_node=; routed_ip=
    if [ -n "$active" ]; then read -r routed_node routed_ip _ <<< "$active"; fi
    [ "$routed_node" = "$target" ] && [ "$routed_ip" = "$(field "$state" target-ip)" ] || fail 'Rollback rejected: placement no longer belongs to this operation.'
    target_generation=$(read_host "$target" current)
    [ "$target_generation" = "$id" ] || fail 'Rollback rejected: target generation belongs to another operation.'
  fi
  upload_helper "$target"
  remote "$target" fence "$id"
  upload_helper "$source"
  remote "$source" restart "$source_generation" "$source_ip" "$HOSTNAME"
  active=$(placement); read -r _ _ version <<< "$active"
  route "$source" "$source_ip" "$version"
  kube -n nod delete configmap nod-operation nod-rollback >/dev/null
  rollback_locked=false
  locked=false
  printf 'Rolled back to %s; target fenced, backup and generations retained.\n' "$source"
  exit 0
fi
[ -z "$state" ] || fail 'Another operation is pending. Run status; interrupted moves require rollback. No marker was cleared.'
id=nod-$(date -u +%Y%m%d%H%M%S)-$(python3 -c 'import secrets; print(secrets.token_hex(4))')
case "$command" in
  deploy)
    target=$deploy_target
    [ -z "$source" ] || [ "$source" = "$target" ] || fail "Already routed to $source; use move $target."
    target_ip=$(node_ip "$target")
    if [ -n "$source" ]; then source_generation=$(read_host "$source" current); fi
    ensure_bundle "$source"
    # Namespace bootstrap is the only mutation needed before the atomic lock.
    cat "$REPO/k8s/apps/nod/00-namespace.yaml" | kube apply -f - >/dev/null
    lock_operation
    apply_base
    if [ -n "$source" ]; then prepare "$target" current; else prepare "$target" fresh; fi
    remote "$target" deploy "$id" "$target_ip" "$HOSTNAME"
    assert_operation
    route "$target" "$target_ip" "$version"
    complete
    printf 'Nod is routed to %s at https://%s\n' "$target" "$HOSTNAME"
    ;;
  backup)
    [ -n "$source" ] || fail 'No active placement to back up.'
    source_generation=$(read_host "$source" current)
    lock_operation
    upload_helper "$source"
    remote "$source" backup "$source_generation" "$id"
    complete
    printf 'Private snapshot retained on %s at /var/lib/nod/backups/%s\n' "$source" "$id"
    ;;
  move)
    target=$1; valid_node "$target"
    [ -n "$source" ] || fail 'No active placement; use deploy.'
    [ "$source" != "$target" ] || fail 'Target is already active.'
    assert_hostname
    target_ip=$(node_ip "$target")
    source_generation=$(read_host "$source" current)
    ensure_bundle "$source"
    lock_operation
    prepare "$target"
    upload_helper "$source"
    assert_operation
    remote "$source" stop-backup "$source_generation" "$id"
    remote "$source" export-backup "$id" | remote "$target" receive-backup "$id"
    assert_operation
    remote "$target" restore "$id" "$id" "$target_ip" "$HOSTNAME"
    assert_operation
    route "$target" "$target_ip" "$version"
    complete
    printf 'Nod moved from %s to %s; original keys/data preserved. Source remains stopped; snapshots and old volumes retained.\n' "$source" "$target"
    ;;
esac
