#!/bin/bash
set -e

CLUSTER_NAME="kratix-test"

# Ensure we are at the repo root
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo ".")
cd "$REPO_ROOT"

echo "🚀 Starting Local Verification Environment Setup..."

# 1. Check Dependencies
command -v kind >/dev/null 2>&1 || { echo "❌ 'kind' is required but not installed."; exit 1; }
command -v kubectl >/dev/null 2>&1 || { echo "❌ 'kubectl' is required but not installed."; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "❌ 'docker' is required but not installed."; exit 1; }

# 2. Create Kind Cluster
if kind get clusters | grep -q "^$CLUSTER_NAME$"; then
    echo "✅ Cluster '$CLUSTER_NAME' already exists."
else
    echo "📦 Creating Kind cluster '$CLUSTER_NAME'..."
    cat <<EOF | kind create cluster --name "$CLUSTER_NAME" --config=-
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
  kubeadmConfigPatches:
  - |
    kind: InitConfiguration
    nodeRegistration:
      kubeletExtraArgs:
        node-labels: "ingress-ready=true"
  extraPortMappings:
  - containerPort: 80
    hostPort: 8080
    protocol: TCP
  - containerPort: 443
    hostPort: 8443
    protocol: TCP
EOF
fi

# 3. Install Cert Manager (Required by Kratix)
echo "🔒 Installing Cert Manager..."
kubectl --context "kind-$CLUSTER_NAME" apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.4/cert-manager.yaml
kubectl --context "kind-$CLUSTER_NAME" wait --for=condition=available --timeout=300s deployment/cert-manager-webhook -n cert-manager

# 4. Install Kratix
echo "🧩 Installing Kratix..."
kubectl --context "kind-$CLUSTER_NAME" apply -f https://github.com/syntasso/kratix/releases/latest/download/kratix.yaml
kubectl --context "kind-$CLUSTER_NAME" wait --for=condition=available --timeout=300s deployment/kratix-platform-controller-manager -n kratix-platform-system

# 5. Install Gateway API CRDs (Standard)
echo "jg Installing Gateway API CRDs..."
kubectl --context "kind-$CLUSTER_NAME" apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.0.0/standard-install.yaml

# 5. Build & Load Images
echo "🏗️  Building and Loading Images..."

IMAGES_DIR="ghcr.io/you-randomly/foundry-platform"

# License Pipeline
echo "   Building License Pipeline..."
docker build -t "$IMAGES_DIR/license-pipeline:latest" promises/foundry-license/configure-pipeline
kind load docker-image "$IMAGES_DIR/license-pipeline:latest" --name "$CLUSTER_NAME"

# Instance Pipeline
echo "   Building Instance Pipeline..."
docker build -t "$IMAGES_DIR/instance-pipeline:latest" promises/foundry-instance/configure-pipeline
kind load docker-image "$IMAGES_DIR/instance-pipeline:latest" --name "$CLUSTER_NAME"

# Standby Page
echo "   Building Standby Page..."
docker build -t "$IMAGES_DIR/standby-page:latest" standby-page
kind load docker-image "$IMAGES_DIR/standby-page:latest" --name "$CLUSTER_NAME"

echo "✅ Setup Complete!"
echo "   Context switched to: kind-$CLUSTER_NAME"
echo "   Next: Apply your promises and test manifests."
