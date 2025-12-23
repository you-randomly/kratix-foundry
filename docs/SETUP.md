# Kratix Foundry Platform Setup Guide

This guide explains how to set up the Kratix Foundry platform for managing Foundry VTT instances.

## Prerequisites

The following components must be installed in your Kubernetes cluster:

| Component | Purpose | Version |
|-----------|---------|---------|
| [Kratix](https://kratix.io/) | Promise-based platform orchestration | Latest |
| [FluxCD](https://fluxcd.io/) | GitOps delivery from Kratix StateStore | Latest |
| [cert-manager](https://cert-manager.io/) | Certificate management (Kratix dependency) | v1.16+ |
| [Gateway API](https://gateway-api.sigs.k8s.io/) | HTTPRoute CRDs for ingress | v1.2+ |
| [Envoy Gateway](https://gateway.envoyproxy.io/) | Gateway implementation | v1.2+ |

### Optional Components

| Component | Purpose |
|-----------|---------|
| [external-dns](https://github.com/kubernetes-sigs/external-dns) | Automatic DNS from DNSEndpoint resources |
| NFS Server | Shared storage backend (if using `storageBackend: nfs`) |

## Quick Start (OrbStack)

For local development with OrbStack's built-in Kubernetes:

```bash
./scripts/setup-orbstack-env.sh
```

This script installs all prerequisites and configures the environment automatically.

## Manual Installation

### 1. Install cert-manager

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.16.2/cert-manager.yaml
kubectl wait --for=condition=available deployment/cert-manager-webhook -n cert-manager --timeout=120s
```

### 2. Install Kratix

```bash
kubectl apply -f https://github.com/syntasso/kratix/releases/latest/download/kratix.yaml
kubectl wait --for=condition=available deployment/kratix-platform-controller-manager -n kratix-platform-system --timeout=180s
```

### 3. Install FluxCD

```bash
kubectl apply -f https://github.com/fluxcd/flux2/releases/latest/download/install.yaml
kubectl wait --for=condition=available deployment/source-controller -n flux-system --timeout=120s
```

### 4. Install Gateway API + Envoy Gateway

```bash
# Gateway API CRDs
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml

# Envoy Gateway
kubectl apply --server-side -f https://github.com/envoyproxy/gateway/releases/download/v1.2.5/install.yaml
kubectl wait --for=condition=available deployment/envoy-gateway -n envoy-gateway-system --timeout=120s
```

### 5. Configure Kratix StateStore

Apply the MinIO-based state store and Destination:

```bash
kubectl apply -f manifests/infrastructure.yaml
```

### 6. Create Gateway

```bash
kubectl create namespace foundry-vtt

# Create TLS certificate (self-signed for dev)
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /tmp/gateway.key -out /tmp/gateway.crt \
  -subj "/CN=*.k8s.orb.local" \
  -addext "subjectAltName=DNS:*.k8s.orb.local"

kubectl create secret tls gateway-tls \
  --cert=/tmp/gateway.crt --key=/tmp/gateway.key \
  -n foundry-vtt

kubectl apply -f manifests/gateway.yaml
```

### 7. Build Pipeline Images

```bash
# From repository root
docker build -t kratix-foundry-instance-configure:dev \
  -f promises/foundry-instance/configure-pipeline/Dockerfile .

docker build -t kratix-foundry-license-configure:dev \
  -f promises/foundry-license/configure-pipeline/Dockerfile .
```

> **Note:** For Kind clusters, load images with `kind load docker-image <image> --name <cluster>`.

### 8. Install Promises

```bash
kubectl apply -f manifests/foundry-standby-page.yaml
kubectl apply -f promises/foundry-license/promise.yaml
kubectl apply -f promises/foundry-instance/promise.yaml
```

## Creating Resources

### 1. Create Secrets

```bash
# Foundry credentials
kubectl create secret generic foundry-credentials \
  -n foundry-vtt \
  --from-literal=username=admin \
  --from-literal=password=<password> \
  --from-literal=adminPassword=<admin-key>

# Foundry license
kubectl create secret generic foundry-license-secret \
  -n foundry-vtt \
  --from-literal=license-key=<your-license-key>
```

### 2. Create a License

```bash
kubectl apply -f examples/foundry-license.yaml
```

### 3. Create an Instance

```bash
kubectl apply -f examples/foundry-instance.yaml
```

## Verification

```bash
# Check Promises are installed
kubectl get promises

# Check Kratix state store
kubectl get buckets -n kratix-platform-system

# Check FluxCD sync
kubectl get kustomization -n flux-system

# Check instances
kubectl get foundryinstances -n foundry-vtt
kubectl get foundrylicenses -n foundry-vtt

# Check routing
kubectl get httproute -n foundry-vtt
kubectl get gateway -n foundry-vtt
```

## Troubleshooting

### Pipeline not running
Check Kratix controller logs:
```bash
kubectl logs -n kratix-platform-system deploy/kratix-platform-controller-manager
```

### Routes not updating
Manually trigger license reconciliation:
```bash
kubectl label foundrylicense <name> -n foundry-vtt kratix.io/manual-reconciliation=true
```

### FluxCD not syncing
Check Kustomization status:
```bash
kubectl get kustomization -n flux-system -o yaml
```
