#!/usr/bin/env bash
# Sets up and deploys the k3s cluster. Runs from your laptop, reads
# k8s/cluster.env, reaches nodes over SSH and the cluster with kubectl.
#
#   lab.sh node <server|agent> [--media] <ssh-target>
#       Install k3s on a machine (or update it). `server` is the control-plane
#       node, the machine at K3S_SERVER; `agent` is a worker node, every other
#       machine. --media marks the node as one that holds the media library at
#       /data/media.
#   lab.sh apply [--strict] [app ...]
#       Apply namespaces, infrastructure and the apps in k8s/apps (all of them,
#       or the ones named). Anything whose secrets.env is missing or incomplete
#       is skipped; --strict stops instead, before applying anything.
#   lab.sh import-arr <folder>
#       Copy the old Compose stack's app settings (its media/ folder) into the
#       arr stack's volumes.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
media_label=lab.alamst.me/media

die() { echo "lab.sh: $*" >&2; exit 1; }
usage() { sed -n '2,16s/^# \{0,1\}//p' "$0" >&2; exit 2; }

load_env() {
  [[ -f $here/cluster.env ]] || die "no k8s/cluster.env: copy k8s/cluster.env.example and fill it in"
  set -a; . "$here/cluster.env"; set +a
}
need() { for v; do [[ -n ${!v:-} ]] || die "set $v in k8s/cluster.env"; done; }

# ---------------------------------------------------------------- node
cmd_node() {
  local type=${1:-} media=false target=""
  shift || true
  for a in "$@"; do
    case $a in
      --media) media=true ;;
      -*) die "unknown flag $a" ;;
      *) target=$a ;;
    esac
  done
  [[ $type == server || $type == agent ]] && [[ -n $target ]] || usage
  load_env; need K3S_VERSION K3S_SERVER K3S_TOKEN

  local args=("$type")
  if [[ $type == server ]]; then
    args+=(--disable servicelb)              # kube-vip serves CLUSTER_VIP instead
  else
    args+=(--server "https://$K3S_SERVER:6443")
  fi
  $media && args+=(--node-label "$media_label=true")

  # k3s runs as the control-plane service (k3s.service) or the worker service
  # (k3s-agent.service) on a machine, not both: refuse before installing the
  # other one. --media is separate and fits either.
  local other_unit=k3s-agent other_kind="a worker node"
  [[ $type == agent ]] && other_unit=k3s other_kind="the control-plane node"
  local check="if [ -f /etc/systemd/system/$other_unit.service ]; then echo 'lab.sh: this machine is already $other_kind; \`node $type\` is for the other machines' >&2; exit 3; fi; "

  local prep="" post=""
  $media && prep="sudo mkdir -p /data/media && "
  # hand the admin kubeconfig to the SSH user, so it can be fetched without sudo
  [[ $type == server ]] && post=" && sudo install -m 600 -o \$(id -un) /etc/rancher/k3s/k3s.yaml ~/.k3s-kubeconfig"
  ssh -t "$target" "${check}${prep}curl -sfL https://get.k3s.io | sudo INSTALL_K3S_VERSION='$K3S_VERSION' K3S_TOKEN='$K3S_TOKEN' sh -s - ${args[*]}${post}"

  if [[ $type == server && ! -f $here/kubeconfig ]]; then
    ssh "$target" 'cat ~/.k3s-kubeconfig' \
      | sed "s#https://127.0.0.1:6443#https://$K3S_SERVER:6443#" > "$here/kubeconfig"
    chmod 600 "$here/kubeconfig"
    echo "Saved k8s/kubeconfig. Use it with: export KUBECONFIG=$here/kubeconfig"
  fi

  # k3s applies --node-label only when a node first registers; label existing
  # nodes directly.
  if $media; then
    local node
    node=$(ssh "$target" hostname)
    if kubectl get node "$node" >/dev/null 2>&1; then
      kubectl label node "$node" "$media_label=true" --overwrite
    fi
  fi
}

# ---------------------------------------------------------------- apply
# A folder is configured when it needs no secrets.env, or every uncommented key
# of its secrets.env.example has a value in secrets.env.
missing_config() {
  local dir=$1 key missing=()
  [[ -f $dir/secrets.env.example ]] || return 0
  [[ -f $dir/secrets.env ]] || { echo "no secrets.env"; return; }
  while IFS= read -r key; do
    grep -qE "^$key=.+" "$dir/secrets.env" || missing+=("$key")
  done < <(grep -oE '^[A-Z0-9_]+=' "$dir/secrets.env.example" | tr -d =)
  ((${#missing[@]} == 0)) || echo "empty in secrets.env: ${missing[*]}"
}

# Values from cluster.env that manifests may use as ${NAME}.
subst_vars() { grep -oE '^[A-Z0-9_]+=' "$here/cluster.env" | grep -v K3S_TOKEN | sed 's/^/$/; s/=$//' | tr '\n' ' '; }

apply_dir() {
  local dir=$1 out tries=0
  # cert-manager installs its own resource types; the first apply can race it
  until out=$(kubectl kustomize "$dir" | envsubst "$(subst_vars)" | kubectl apply -f - 2>&1); do
    if grep -q 'ensure CRDs are installed' <<<"$out" && ((tries++ < 30)); then
      echo "  waiting for resource types to be installed..."; sleep 10
    else
      echo "$out" >&2; return 1
    fi
  done
  echo "$out" | sed 's/^/  /'
}

cmd_apply() {
  local strict=false apps=() a
  for a in "$@"; do
    case $a in
      --strict) strict=true ;;
      -*) die "unknown flag $a" ;;
      *) [[ -d $here/apps/$a ]] || die "no app folder k8s/apps/$a"; apps+=("$a") ;;
    esac
  done
  load_env; need CLUSTER_VIP
  if ((${#apps[@]} == 0)); then
    for a in "$here"/apps/*/; do apps+=("$(basename "$a")"); done
  fi

  local dirs=(namespaces infra/kube-vip infra/media-library infra/cert-manager)
  for a in "${apps[@]}"; do dirs+=("apps/$a"); done

  local todo=() skipped=() d why
  for d in "${dirs[@]}"; do
    why=$(missing_config "$here/$d")
    if [[ -n $why ]]; then skipped+=("$d ($why)"); else todo+=("$d"); fi
  done
  if $strict && ((${#skipped[@]})); then
    printf 'not configured:\n' >&2; printf '  %s\n' "${skipped[@]}" >&2
    die "nothing applied (--strict)"
  fi

  for d in "${todo[@]}"; do echo "applying $d"; apply_dir "$here/$d"; done
  if ((${#skipped[@]})); then
    echo "skipped, not configured:"; printf '  %s\n' "${skipped[@]}"
  fi
}

# ---------------------------------------------------------------- import-arr
cmd_import_arr() {
  local old=${1:-}
  [[ -d $old ]] || usage
  # settings folder in the old stack -> deployment that uses it
  local -a pairs=(jellyseerr:jellyseerr sonarr:sonarr radarr:radarr lidarr:lidarr
    bazarr:bazarr prowlarr:prowlarr qbittorrent:downloads nzbget:downloads)
  local p app deploy found=() deploys=()
  for p in "${pairs[@]}"; do
    app=${p%%:*}; deploy=${p#*:}
    [[ -d $old/$app ]] || { echo "no $old/$app, leaving $app as it is"; continue; }
    kubectl -n media get pvc "$app-config" >/dev/null 2>&1 || die "no $app-config volume: run 'lab.sh apply arr' first"
    found+=("$app")
    [[ " ${deploys[*]} " == *" $deploy "* ]] || deploys+=("$deploy")
  done
  ((${#found[@]})) || die "nothing to import from $old"

  for deploy in "${deploys[@]}"; do kubectl -n media scale "deploy/$deploy" --replicas=0; done
  for deploy in "${deploys[@]}"; do
    while kubectl -n media get pods -l "app.kubernetes.io/name=$deploy" -o name | grep -q .; do sleep 2; done
  done

  for app in "${found[@]}"; do
    echo "importing $app"
    kubectl -n media apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata: { name: import-$app, namespace: media }
spec:
  restartPolicy: Never
  containers:
    - name: import
      image: busybox:1.37
      command: [sleep, "3600"]
      volumeMounts: [{ name: config, mountPath: /config }]
  volumes:
    - name: config
      persistentVolumeClaim: { claimName: $app-config }
EOF
    kubectl -n media wait --for=condition=Ready "pod/import-$app" --timeout=3m
    kubectl -n media exec "import-$app" -- sh -c 'rm -rf /config/* /config/.[!.]*' || true
    COPYFILE_DISABLE=1 tar -C "$old/$app" -cf - . | kubectl -n media exec -i "import-$app" -- tar -xf - -C /config
    kubectl -n media exec "import-$app" -- chown -R 1000:1000 /config   # PUID/PGID of the apps
    kubectl -n media delete pod "import-$app"
  done

  for deploy in "${deploys[@]}"; do kubectl -n media scale "deploy/$deploy" --replicas=1; done
  echo "Done. Point the apps at each other's new addresses (README, 'Moving from the old Compose stack')."
}

case ${1:-} in
  node) shift; cmd_node "$@" ;;
  apply) shift; cmd_apply "$@" ;;
  import-arr) shift; cmd_import_arr "$@" ;;
  *) usage ;;
esac
