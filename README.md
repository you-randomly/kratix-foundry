# Kratix Foundry

A Kratix Promise-based platform for deploying and managing Foundry VTT instances with GitOps via FluxCD.

## Quick Start

```bash
# Set up the development environment (creates Kind cluster with all dependencies)
./scripts/setup-dev-env.sh

# In a separate terminal, enable LoadBalancer IPs for Kind
sudo cloud-provider-kind --enable-lb-port-mapping

# Check status
kubectl get promises
kubectl get gateway -n foundry-vtt
kubectl get kustomization -n flux-system
```

> **Note:** `cloud-provider-kind` is required to assign external IPs to LoadBalancer services.
> Install it from: https://github.com/kubernetes-sigs/cloud-provider-kind

## Architecture

┌─────────────────────────────────────────────────────────────────────┐
│                         Kubernetes Cluster                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  FoundryLicense ──▶ Kratix ──▶ MinIO Bucket ◀── FluxCD             │
│  FoundryInstance              (kratix)           │                  │
│                                                  ▼                  │
│                                          Deployment (w/ Monitor)    │
│                                          ├── Service                │
│                                          ├── HTTPRoute              │
│                                          └── DNSEndpoint            │
└─────────────────────────────────────────────────────────────────────┘
```

The platform now uses **Python-based pipelines** and a **Real-time Status Monitor** sidecar for improved reliability and visibility.

## Promises

### FoundryLicense
Manages license keys for Foundry VTT. Each license can have one active instance at a time.

### FoundryInstance  
Deploys a Foundry VTT instance with:
- NFS or PVC storage backend
- HTTPRoute for ingress
- DNSEndpoint for external-dns
- Standby page when not active

## Development

Both pipelines are implemented in Python. Since they share a common library (`lib/foundry_lib`), you must build them from the repository root:

```bash
# Rebuild pipeline images (run from root)
docker build -t kratix-foundry-instance-configure:dev -f promises/foundry-instance/configure-pipeline/Dockerfile .
docker build -t kratix-foundry-license-configure:dev -f promises/foundry-license/configure-pipeline/Dockerfile .

# Re-apply Promises
kubectl apply -f promises/foundry-license/promise.yaml
kubectl apply -f promises/foundry-instance/promise.yaml

# Test with example resources
kubectl apply -f examples/foundry-license.yaml
kubectl apply -f examples/foundry-instance.yaml
```

### Discord Bot

The Discord bot lives in `discord-bot/` and shares the `lib/foundry_lib` library with the pipelines.

```bash
# Set up the bot
cd discord-bot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure (copy .env.example to .env and add your bot token)
cp .env.example .env

# Run the bot
python bot.py
```

## Cleanup

```bash
kind delete cluster --name kratix-foundry-dev
```
