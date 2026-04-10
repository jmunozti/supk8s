#!/bin/bash
# deploy.sh — Deploy supk8s: Kind + ArgoCD + Prometheus + agent
set -euo pipefail

# Load config
if [ -f config.env ]; then
  set -a
  source config.env
  set +a
  echo "Config loaded from config.env"
fi

# Load secrets from .env (gitignored — never commit)
if [ -f .env ]; then
  set -a
  source .env
  set +a
  echo "Secrets loaded from .env"
fi

echo "Starting supk8s deployment..."

# 1. Check dependencies
for cmd in docker kind kubectl helm; do
  if ! command -v "$cmd" &> /dev/null; then
    echo "Error: '$cmd' is not installed."
    exit 1
  fi
done
echo "Dependencies OK"



# 2. Kind cluster config
CLUSTER_NAME="supk8s"
KUBECONFIG_FILE="kubeconfig-kind"

cat > kind-config.yaml << 'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    image: kindest/node:v1.29.0
    extraPortMappings:
      - containerPort: 30080
        hostPort: 30080
        protocol: TCP
      - containerPort: 30443
        hostPort: 30443
        protocol: TCP
      - containerPort: 30090
        hostPort: 30090
        protocol: TCP
EOF

# 3. Create or recreate cluster
if kind get clusters | grep -q "^${CLUSTER_NAME}$"; then
  echo "Deleting existing cluster..."
  kind delete cluster --name "${CLUSTER_NAME}"
fi

echo "Creating Kind cluster..."
kind create cluster --name "${CLUSTER_NAME}" --config kind-config.yaml
kind get kubeconfig --name "${CLUSTER_NAME}" > "${KUBECONFIG_FILE}"
chmod 600 "${KUBECONFIG_FILE}"
export KUBECONFIG="${KUBECONFIG_FILE}"

# 4. Build and load demo app
export DOCKER_BUILDKIT=0
echo "Building demo app..."
docker build -t demo-app:v1 demo-app/ --no-cache 2>&1 | tail -1
docker build -t demo-app:v2-crash -f demo-app/Dockerfile.crash demo-app/ --no-cache 2>&1 | tail -1
echo "Images built: demo-app:v1, demo-app:v2-crash"
kind load docker-image demo-app:v1 --name "${CLUSTER_NAME}"
kind load docker-image demo-app:v2-crash --name "${CLUSTER_NAME}"

# 5. Build and load agent
echo "Building agent..."
docker build -t supk8s-agent:latest agent/ --no-cache 2>&1 | tail -1
echo "Image built: supk8s-agent:latest"
kind load docker-image supk8s-agent:latest --name "${CLUSTER_NAME}"

# 6. Install ArgoCD
echo "Installing ArgoCD..."
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
echo "Waiting for ArgoCD to be ready..."
kubectl wait --for=condition=available deployment/argocd-server -n argocd --timeout=120s

# 7. Set ArgoCD credentials, disable TLS, expose as NodePort, fast reconciliation
kubectl apply -f k8s/argocd/argocd-credentials.yaml
kubectl -n argocd patch configmap argocd-cmd-params-cm --type merge -p '{"data":{"server.insecure":"true"}}'
# Reduce sync interval from default 180s → 15s, and shorten Git poll
kubectl -n argocd patch configmap argocd-cm --type merge -p '{"data":{"timeout.reconciliation":"15s","timeout.hard.reconciliation":"0"}}'
kubectl patch svc argocd-server -n argocd -p '{"spec": {"type": "NodePort", "ports": [{"port": 80, "targetPort": 8080, "nodePort": 30443}]}}'
kubectl rollout restart deployment/argocd-server -n argocd
kubectl rollout restart deployment/argocd-repo-server -n argocd
kubectl rollout restart statefulset/argocd-application-controller -n argocd
kubectl wait --for=condition=available deployment/argocd-server -n argocd --timeout=60s

# 8. Apply base manifests (demo-app + agent)
echo "Applying base manifests..."
kubectl apply -f k8s/base/namespace.yaml
sleep 2

# OpenRouter API key is REQUIRED — the agent will not start without it
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  echo "Error: OPENROUTER_API_KEY is not set."
  echo "Get a free API key at https://openrouter.ai/keys"
  echo "Then add it to a .env file in this directory:"
  echo "  echo 'OPENROUTER_API_KEY=sk-or-v1-...' > .env"
  exit 1
fi
echo "Creating OpenRouter secret for LLM analysis..."
kubectl create secret generic supk8s-llm \
  --namespace demo \
  --from-literal=openrouter-api-key="${OPENROUTER_API_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f k8s/base/

# Patch model env var if provided
if [ -n "${OPENROUTER_MODEL:-}" ]; then
  kubectl set env deployment/supk8s-agent -n demo OPENROUTER_MODEL="${OPENROUTER_MODEL}"
fi

echo "Waiting for demo app..."
kubectl wait --for=condition=available deployment/demo-app -n demo --timeout=120s

# 9. Install Prometheus via Helm
echo "Installing Prometheus..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo update
helm upgrade --install prometheus prometheus-community/prometheus \
  --namespace monitoring --create-namespace \
  -f k8s/prometheus/values.yaml \
  --wait --timeout 120s

# 11. Apply ArgoCD project and app
kubectl apply -f k8s/argocd/project.yaml 2>/dev/null || true
REPO_URL="${ARGOCD_REPO_URL:-https://github.com/jmunozti/supk8s.git}"
BRANCH="${ARGOCD_BRANCH:-develop}"
sed "s|repoURL:.*|repoURL: ${REPO_URL}|; s|targetRevision:.*|targetRevision: ${BRANCH}|" \
  k8s/argocd/demo-app-argo.yaml | kubectl apply -f - 2>/dev/null || true

PASS="${ARGOCD_ADMIN_PASSWORD:-supk8s-admin}"
echo ""
echo "supk8s deployed successfully!"
echo ""
echo "  Demo App:   http://localhost:30080"
echo "  ArgoCD:     http://localhost:30443  (admin / $PASS)"
echo "  Prometheus: http://localhost:30090"
echo ""
echo "To simulate a bad deployment:"
echo "  make simulate-failure"
echo "  make logs"
