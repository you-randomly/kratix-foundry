#!/bin/bash
# setup-orbstack-env.sh - Sets up Kratix + FluxCD in native OrbStack K8s
# Usage: ./scripts/setup-orbstack-env.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "🚀 Setting up Kratix Foundry in native OrbStack Kubernetes..."

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_step() { echo -e "${GREEN}▶${NC} $1"; }
log_warn() { echo -e "${YELLOW}⚠${NC} $1"; }
log_error() { echo -e "${RED}✗${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }

# Check context
log_step "Checking Kubernetes context..."
CURRENT_CONTEXT=$(kubectl config current-context)
if [ "$CURRENT_CONTEXT" != "orbstack" ]; then
    log_warn "Current context is '$CURRENT_CONTEXT'. Switching to 'orbstack'..."
    kubectl config use-context orbstack
fi
log_success "Using orbstack context"

# Install cert-manager
log_step "Installing cert-manager..."
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.16.2/cert-manager.yaml
kubectl wait --for=condition=available deployment/cert-manager -n cert-manager --timeout=120s
kubectl wait --for=condition=available deployment/cert-manager-webhook -n cert-manager --timeout=120s

# Wait for webhook CA bundle
log_step "Waiting for cert-manager webhook to be ready..."
for i in {1..30}; do
    if kubectl get validatingwebhookconfigurations cert-manager-webhook -o jsonpath='{.webhooks[0].clientConfig.caBundle}' 2>/dev/null | grep -q .; then
        log_success "cert-manager webhook ready"
        break
    fi
    echo "  Waiting for webhook CA bundle... ($i/30)"
    sleep 2
done

# Install Kratix
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
log_success "FluxCD installed"

# Install Gateway API CRDs
log_step "Installing Gateway API CRDs..."
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml
log_success "Gateway API CRDs installed"

# Install Envoy Gateway
log_step "Installing Envoy Gateway..."
kubectl apply --server-side -f https://github.com/envoyproxy/gateway/releases/download/v1.2.5/install.yaml
kubectl wait --for=condition=available deployment/envoy-gateway -n envoy-gateway-system --timeout=120s
log_success "Envoy Gateway installed"

# Create foundry-vtt namespace
log_step "Creating foundry-vtt namespace..."
kubectl create namespace foundry-vtt --dry-run=client -o yaml | kubectl apply -f -
log_success "foundry-vtt namespace ready"

# Create Gateway
log_step "Creating default Gateway..."
# Create self-signed cert
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /tmp/gateway.key -out /tmp/gateway.crt \
  -subj "/CN=foundry.k8s.orb.local" \
  -addext "subjectAltName=DNS:foundry.k8s.orb.local,DNS:*.foundry.k8s.orb.local,DNS:*.k8s.orb.local" 2>/dev/null
kubectl create secret tls gateway-tls --cert=/tmp/gateway.crt --key=/tmp/gateway.key -n foundry-vtt --dry-run=client -o yaml | kubectl apply -f -


kubectl apply -f "$PROJECT_ROOT/manifests/gateway.yaml"
log_success "Default Gateway created"

# Install DNSEndpoint CRD
log_step "Installing DNSEndpoint CRD..."
kubectl apply -f "$PROJECT_ROOT/manifests/dnsendpoint-crd.yaml"
log_success "DNSEndpoint CRD installed"

# Apply infrastructure
log_step "Setting up MinIO state store and Destination..."
kubectl apply -f "$PROJECT_ROOT/manifests/infrastructure.yaml"
kubectl wait --for=condition=available deployment/minio -n kratix-platform-system --timeout=120s
log_success "Infrastructure configured"

# Build pipeline images (No need to load in OrbStack, it uses Docker engine)
log_step "Building Foundry pipeline images..."
docker build -t kratix-foundry-instance-configure:dev -f "$PROJECT_ROOT/promises/foundry-instance/configure-pipeline/Dockerfile" "$PROJECT_ROOT"
docker build -t kratix-foundry-license-configure:dev -f "$PROJECT_ROOT/promises/foundry-license/configure-pipeline/Dockerfile" "$PROJECT_ROOT"
docker build -t ghcr.io/you-randomly/foundry-platform/standby-page:dev "$PROJECT_ROOT/standby-page"
log_success "Images built"

# Apply Standby Page
log_step "Applying Standby Page..."
kubectl apply -f "$PROJECT_ROOT/manifests/foundry-standby-page.yaml"

# Apply Foundry Promises
log_step "Applying Foundry Promises..."
kubectl apply -f "$PROJECT_ROOT/promises/foundry-license/promise.yaml"
kubectl apply -f "$PROJECT_ROOT/promises/foundry-instance/promise.yaml"
log_success "Applied Promises"

echo ""
log_success "Development environment ready in OrbStack Native K8s!"
echo "Visit: https://foundry-test-instance.foundry-vtt.svc.cluster.local"
