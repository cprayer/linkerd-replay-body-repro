#!/usr/bin/env bash
set -euo pipefail

# https://linkerd.io/docs/getting-started/
# https://linkerd.io/docs/features/gateway-api/
readonly LINKERD_VERSION=edge-26.8.2
readonly GATEWAY_API_VERSION=v1.5.1
CLUSTER_NAME=${CLUSTER_NAME:-linkerd-replay-repro}
LINKERD_BIN=${LINKERD_BIN:-linkerd}
KIND_NODE_IMAGE=${KIND_NODE_IMAGE:-kindest/node:v1.36.1}
readonly CONTEXT="kind-${CLUSTER_NAME}"

usage() {
  cat <<'EOF'
Usage: scripts/cluster.sh [up|check|delete]

up      Create a dedicated kind cluster and install pinned Linkerd and Gateway API.
        An existing cluster is checked without changing its installation.
check   Check the existing cluster without installing or replacing anything.
delete  Explicitly delete this named kind cluster.

Requirements: Bash, Docker (running), kind, kubectl, Linkerd CLI edge-26.8.2.
Install the CLI using the official installer:
  curl --proto '=https' --tlsv1.2 -sSfL https://run.linkerd.io/install-edge \
    | LINKERD2_VERSION=edge-26.8.2 sh
  export PATH="$HOME/.linkerd2/bin:$PATH"

Environment:
  CLUSTER_NAME     Dedicated kind cluster name (default: linkerd-replay-repro)
  LINKERD_BIN      CLI command or executable path (default: linkerd)
  KIND_NODE_IMAGE  Node image for a new cluster (default: kindest/node:v1.36.1)
EOF
}

fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"; }

command_name=${1:-up}
if [[ $# -gt 1 ]]; then usage >&2; exit 2; fi
case "$command_name" in
  -h|--help|help) usage; exit 0 ;;
  up|check|delete) ;;
  *) usage >&2; exit 2 ;;
esac
[[ "$CLUSTER_NAME" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || fail 'CLUSTER_NAME must contain lowercase letters, numbers, and internal hyphens.'
need kind
need docker
docker info >/dev/null 2>&1 || fail 'Docker is not available. Start Docker before running this script.'

if [[ "$command_name" == delete ]]; then
  kind delete cluster --name "$CLUSTER_NAME"
  exit 0
fi

need kubectl
if ! command -v "$LINKERD_BIN" >/dev/null 2>&1; then
  usage >&2
  fail "Linkerd CLI not found: $LINKERD_BIN (set LINKERD_BIN to its executable path)"
fi
cli_version=$("$LINKERD_BIN" version --client --short)
[[ "$cli_version" == "$LINKERD_VERSION" ]] || fail "Expected Linkerd CLI $LINKERD_VERSION, found $cli_version. See --help for the pinned installer."

cluster_names=$(kind get clusters)
cluster_exists=false
while IFS= read -r existing_name; do
  if [[ "$existing_name" == "$CLUSTER_NAME" ]]; then cluster_exists=true; fi
done <<< "$cluster_names"

check_cluster() {
  kubectl --context "$CONTEXT" cluster-info
  kubectl --context "$CONTEXT" wait --for=condition=Ready nodes --all --timeout=60s
  local crd bundle_version
  for crd in httproutes.gateway.networking.k8s.io grpcroutes.gateway.networking.k8s.io; do
    bundle_version=$(kubectl --context "$CONTEXT" get crd "$crd" \
      -o 'jsonpath={.metadata.annotations.gateway\.networking\.k8s\.io/bundle-version}')
    [[ "$bundle_version" == "$GATEWAY_API_VERSION" ]] || fail "Expected $crd bundle $GATEWAY_API_VERSION, found $bundle_version. Existing CRDs were not changed."
  done
  for deployment in linkerd-identity linkerd-destination linkerd-proxy-injector; do
    kubectl --context "$CONTEXT" -n linkerd rollout status "deployment/$deployment" --timeout=60s
  done
  "$LINKERD_BIN" --context "$CONTEXT" check --expected-version "$LINKERD_VERSION" --wait=60s
}

if [[ "$cluster_exists" == true ]]; then
  printf 'Checking existing cluster %s without replacing its installation.\n' "$CLUSTER_NAME"
  check_cluster
  exit 0
fi
[[ "$command_name" != check ]] || fail "kind cluster $CLUSTER_NAME does not exist. Run this script with up first."

kind create cluster --name "$CLUSTER_NAME" --image "$KIND_NODE_IMAGE" --wait=60s
# All Kubernetes operations use the dedicated context, independent of the user's current context.
kubectl --context "$CONTEXT" apply -f \
  "https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/standard-install.yaml"
kubectl --context "$CONTEXT" wait --for=condition=Established \
  crd/httproutes.gateway.networking.k8s.io crd/grpcroutes.gateway.networking.k8s.io --timeout=60s
"$LINKERD_BIN" --context "$CONTEXT" check --pre --expected-version "$LINKERD_VERSION" --wait=60s
"$LINKERD_BIN" --context "$CONTEXT" install --crds | kubectl --context "$CONTEXT" apply -f -
"$LINKERD_BIN" --context "$CONTEXT" check --crds --expected-version "$LINKERD_VERSION" --wait=60s
"$LINKERD_BIN" --context "$CONTEXT" install | kubectl --context "$CONTEXT" apply -f -
check_cluster
