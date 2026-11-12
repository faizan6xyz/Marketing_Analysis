#!/usr/bin/env bash
set -Eeuo pipefail
NAMESPACE="leo-social"
GH_USER="faizan6xyz"
MK_MEMORY="6000mb"
MK_CPUS="4"
MK_LOG="minikube-start.log"
DB1_POD="db-0"
DB2_POD="db2-0"
DB3_POD="db3-0"
TMP="$(mktemp -d)"
chmod 700 "$TMP"
cleanup() { rm -rf "$TMP"; unset GH_PAT || true; }
trap cleanup EXIT
trap 'echo "ERROR: deployment failed at line $LINENO." >&2' ERR
wait_for_pod() {
  local pod="$1" timeout="${2:-180}"
  local end=$((SECONDS + timeout))
  until kubectl get pod "$pod" -n "$NAMESPACE" >/dev/null 2>&1; do
    if (( SECONDS > end )); then
      echo "Timed out waiting for pod/$pod to be created" >&2
      return 1
    fi
    sleep 2
  done
  kubectl wait --for=condition=Ready "pod/$pod" -n "$NAMESPACE" --timeout="${timeout}s"
}
retry() {
  local tries="$1"; shift
  local n=1
  until "$@"; do
    if (( n >= tries )); then return 1; fi
    echo "Retrying ($n/$tries): $*"
    n=$((n + 1))
    sleep 5
  done
}
for cmd in minikube kubectl podman base64 sed; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "Missing required command: $cmd" >&2; exit 1; }
done
required_files=( .env schema.sql pg_hba.conf minikube/namespace.yml minikube/db_read_service.yml minikube/db_write_service.yml  minikube/pv.yml minikube/db1_state.yml minikube/db2_state.yml minikube/db3_state.yml minikube/backend_service.yml minikube/hpa.yml minikube/stateful_backend.yml minikube/backendnetwrok.yml minikube/ingress.yaml minikube/ingresswebhook.yml )
for f in "${required_files[@]}"; do
  [[ -f "$f" ]] || { echo "Missing required file: $f" >&2; exit 1; }
done
if minikube status >/dev/null 2>&1; then
  echo "Minikube already running, skipping start"
else
  echo "Starting Minikube..."
  if ! minikube start   --driver=podman --rootless --container-runtime=containerd   --memory="$MK_MEMORY" --cpus="$MK_CPUS" --nodes=1   --addons=metrics-server,ingress,storage-provisioner,default-storageclass   >"$MK_LOG" 2>&1; then
    echo "Failed to start the cluster. Last 30 lines of $MK_LOG:" >&2
    tail -n 30 "$MK_LOG" >&2
    exit 1
  fi
  echo "Minikube started successfully"
fi
kubectl config use-context minikube >/dev/null
echo "Waiting for ingress controller..."
kubectl wait --namespace ingress-nginx --for=condition=Ready pod --selector=app.kubernetes.io/component=controller --timeout=180s
kubectl apply -f minikube/namespace.yml
kubectl wait --for=jsonpath='{.status.phase}'=Active "namespace/$NAMESPACE" --timeout=60s
kubectl apply -f minikube/db_read_service.yml
kubectl apply -f minikube/db_write_service.yml
kubectl apply -f minikube/pv.yml
if [[ -z "${GH_PAT:-}" ]]; then
  read -rsp "Enter GHCR Personal Access Token: " GH_PAT
  echo
fi
AUTH_B64="$(printf '%s:%s' "$GH_USER" "$GH_PAT" | base64 | tr -d '\n')"
( umask 077
  printf '{"auths":{"ghcr.io":{"username":"%s","password":"%s","auth":"%s"}}}' "$GH_USER" "$GH_PAT" "$AUTH_B64" > "$TMP/.dockerconfigjson" )
unset GH_PAT AUTH_B64
kubectl create secret generic ghcr-secret --type=kubernetes.io/dockerconfigjson --from-file=.dockerconfigjson="$TMP/.dockerconfigjson" -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
( umask 077
  sed -E -e '/^[[:space:]]*(#|$)/d' -e 's/^[[:space:]]*export[[:space:]]+//' -e "s/^([A-Za-z_][A-Za-z0-9_]*)=([\"'])(.*)\2\$/\1=\3/" .env > "$TMP/clean.env" )
kubectl create secret generic app-env --from-env-file="$TMP/clean.env" -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl create configmap db-config -n "$NAMESPACE" --from-file=schema.sql=schema.sql --from-file=pg_hba.conf=pg_hba.conf --dry-run=client -o yaml | kubectl apply -f -
echo "Deploying primary database..."
kubectl apply -f minikube/db1_state.yml
wait_for_pod "$DB1_POD" 180
retry 12 kubectl exec "$DB1_POD" -n "$NAMESPACE" -- pg_isready >/dev/null
kubectl label pod "$DB1_POD" -n "$NAMESPACE" primary=true --overwrite
echo "Primary database is ready"
echo "Deploying replicas..."
kubectl apply -f minikube/db2_state.yml
kubectl apply -f minikube/db3_state.yml
wait_for_pod "$DB2_POD" 180
wait_for_pod "$DB3_POD" 180
echo "Replicas are ready"
kubectl apply -f minikube/backend_service.yml
kubectl apply -f minikube/hpa.yml
kubectl apply -f minikube/stateful_backend.yml
kubectl apply -f minikube/backendnetwrok.yml
echo "Waiting for backend..."
kubectl rollout status statefulset/backend -n "$NAMESPACE" --timeout=180s
retry 5 kubectl apply -f minikube/ingress.yaml
retry 5 kubectl apply -f minikube/ingresswebhook.yml
echo
echo "Deployment completed!"
kubectl get pods -n "$NAMESPACE" -o wide
kubectl get services -n "$NAMESPACE"
kubectl get ingress -n "$NAMESPACE"
cat <<'EOF'
NOTE: with the rootless podman driver the ingress is not reachable on the node IP
from the host. Run `minikube tunnel` in another terminal, or use:
  kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller 8080:80
EOF