#!/bin/bash
# setup-dev-env.sh - Sets up Kratix + FluxCD development environment
# Usage: ./scripts/setup-dev-env.sh [cluster-name]

set -euo pipefail

CLUSTER_NAME="${1:-kratix-foundry-dev}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "🚀 Setting up Kratix Foundry development environment..."
echo "   Cluster: $CLUSTER_NAME"
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_step() { echo -e "${GREEN}▶${NC} $1"; }
log_warn() { echo -e "${YELLOW}⚠${NC} $1"; }
log_error() { echo -e "${RED}✗${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }

# Check prerequisites
log_step "Checking prerequisites..."
command -v kind >/dev/null 2>&1 || { log_error "kind is required but not installed."; exit 1; }
command -v kubectl >/dev/null 2>&1 || { log_error "kubectl is required but not installed."; exit 1; }
command -v docker >/dev/null 2>&1 || { log_error "docker is required but not installed."; exit 1; }
docker info >/dev/null 2>&1 || { log_error "Docker is not running. Please start Docker/OrbStack."; exit 1; }
log_success "All prerequisites met"

# Delete existing cluster if it exists
if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    log_warn "Cluster '$CLUSTER_NAME' already exists. Deleting..."
    kind delete cluster --name "$CLUSTER_NAME"
fi

# Create Kind cluster
log_step "Creating Kind cluster '$CLUSTER_NAME'..."
kind create cluster --name "$CLUSTER_NAME"
log_success "Kind cluster created"

# Install cert-manager (required by Kratix)
log_step "Installing cert-manager..."
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.16.2/cert-manager.yaml
kubectl wait --for=condition=available deployment/cert-manager -n cert-manager --timeout=120s
kubectl wait --for=condition=available deployment/cert-manager-webhook -n cert-manager --timeout=120s
kubectl wait --for=condition=available deployment/cert-manager-cainjector -n cert-manager --timeout=120s

# Wait for cert-manager webhook to be fully ready (not just the deployment)
log_step "Waiting for cert-manager webhook to be ready..."
for i in {1..30}; do
    if kubectl get validatingwebhookconfigurations cert-manager-webhook -o jsonpath='{.webhooks[0].clientConfig.caBundle}' 2>/dev/null | grep -q .; then
        log_success "cert-manager webhook ready"
        break
    fi
    echo "  Waiting for webhook CA bundle... ($i/30)"
    sleep 2
done

# Install Kratix (with retry for webhook timing issues)
log_step "Installing Kratix..."
for i in {1..3}; do
    if kubectl apply --filename https://github.com/syntasso/kratix/releases/latest/download/kratix.yaml 2>&1; then
        break
    fi
    log_warn "Kratix install attempt $i failed, retrying in 5s..."
    sleep 5
done
kubectl wait --for=condition=available deployment/kratix-platform-controller-manager -n kratix-platform-system --timeout=180s
log_success "Kratix installed"

# Install FluxCD
log_step "Installing FluxCD..."
kubectl apply -f https://github.com/fluxcd/flux2/releases/latest/download/install.yaml
kubectl wait --for=condition=available deployment/source-controller -n flux-system --timeout=120s
kubectl wait --for=condition=available deployment/kustomize-controller -n flux-system --timeout=120s
log_success "FluxCD installed"

# Install Gateway API CRDs
log_step "Installing Gateway API CRDs..."
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml
log_success "Gateway API CRDs installed"

# Install Envoy Gateway (using server-side apply to avoid annotation size limit)
log_step "Installing Envoy Gateway..."
kubectl apply --server-side -f https://github.com/envoyproxy/gateway/releases/download/v1.2.5/install.yaml
kubectl wait --for=condition=available deployment/envoy-gateway -n envoy-gateway-system --timeout=120s
log_success "Envoy Gateway installed"

# Create foundry-vtt namespace early (Promise will also create it, but we need it for Gateway)
log_step "Creating foundry-vtt namespace..."
kubectl create namespace foundry-vtt --dry-run=client -o yaml | kubectl apply -f -
log_success "foundry-vtt namespace ready"

# Note: NFS storage backend requires NFS kernel support (not always available in dev clusters)
# For dev, use storageBackend: pvc in your FoundryInstance resources

# Create default GatewayClass and Gateway
log_step "Creating default Gateway..."
# Create self-signed cert for Gateway (required for OrbStack HTTPS proxying)
log_step "Creating self-signed TLS certificate for Gateway..."
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /tmp/gateway.key -out /tmp/gateway.crt \
  -subj "/CN=*.orb.local" \
  -addext "subjectAltName=DNS:*.orb.local,DNS:*.kratix-foundry-dev-control-plane.orb.local" 2>/dev/null
kubectl create secret tls gateway-tls --cert=/tmp/gateway.crt --key=/tmp/gateway.key -n foundry-vtt --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f "$PROJECT_ROOT/manifests/gateway.yaml"
log_success "Default Gateway (HTTP/HTTPS) created"

# Install DNSEndpoint CRD (required for Foundry instances in dev)
log_step "Installing DNSEndpoint CRD..."
kubectl apply -f "$PROJECT_ROOT/manifests/dnsendpoint-crd.yaml"
log_success "DNSEndpoint CRD installed"


# Apply infrastructure (MinIO, StateStore, Destination, FluxCD config)
log_step "Setting up MinIO state store and Destination..."
kubectl apply -f "$PROJECT_ROOT/manifests/infrastructure.yaml"
kubectl wait --for=condition=available deployment/minio -n kratix-platform-system --timeout=120s
log_success "Infrastructure configured"

# Build and load Foundry pipeline images
log_step "Building Foundry pipeline images..."

if [ -d "$PROJECT_ROOT/promises/foundry-instance/configure-pipeline" ]; then
    docker build -t kratix-foundry-instance-configure:dev \
        "$PROJECT_ROOT/promises/foundry-instance/configure-pipeline"
    kind load docker-image kratix-foundry-instance-configure:dev --name "$CLUSTER_NAME"
    log_success "Built and loaded foundry-instance pipeline image"
else
    log_warn "foundry-instance pipeline not found, skipping..."
fi

if [ -d "$PROJECT_ROOT/promises/foundry-license/configure-pipeline" ]; then
    docker build -t kratix-foundry-license-configure:dev \
        "$PROJECT_ROOT/promises/foundry-license/configure-pipeline"
    kind load docker-image kratix-foundry-license-configure:dev --name "$CLUSTER_NAME"
    log_success "Built and loaded foundry-license pipeline image"
else
    log_warn "foundry-license pipeline not found, skipping..."
fi

# Apply Foundry Promises
log_step "Applying Foundry Promises..."

if [ -f "$PROJECT_ROOT/promises/foundry-license/promise.yaml" ]; then
    kubectl apply -f "$PROJECT_ROOT/promises/foundry-license/promise.yaml"
    log_success "Applied foundry-license Promise"
fi

if [ -f "$PROJECT_ROOT/promises/foundry-instance/promise.yaml" ]; then
    kubectl apply -f "$PROJECT_ROOT/promises/foundry-instance/promise.yaml"
    log_success "Applied foundry-instance Promise"
fi

# Wait for promises to be available
log_step "Waiting for Promises to be available..."
sleep 10

kubectl get promises

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log_success "Development environment ready!"
echo ""
echo "  Cluster:     $CLUSTER_NAME"
echo "  Context:     kind-$CLUSTER_NAME"
echo ""
echo -e "  ${YELLOW}⚠ LoadBalancer Setup Required:${NC}"
echo "    In a separate terminal, run:"
echo -e "    ${GREEN}sudo cloud-provider-kind --enable-lb-port-mapping${NC}"
echo ""
echo "    This assigns external IPs to LoadBalancer services (like the Gateway)."
echo "    Install cloud-provider-kind: https://github.com/kubernetes-sigs/cloud-provider-kind"
echo ""
echo "  Quick commands:"
echo "    kubectl get promises                           # List Promises"
echo "    kubectl get gateway -n foundry-vtt             # Check Gateway status"
echo "    kubectl get svc -n envoy-gateway-system        # Check Envoy LoadBalancer"
echo "    kubectl get kustomization -n flux-system       # Check FluxCD status"
echo ""
echo "  To create a test instance:"
echo "    kubectl apply -f examples/foundry-license.yaml"
echo "    kubectl apply -f examples/foundry-instance.yaml"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
