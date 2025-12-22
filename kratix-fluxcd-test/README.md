# Kratix + FluxCD Test Environment

This directory contains a working test environment for Kratix with FluxCD GitOps.

## Quick Reference

```bash
# Switch context
kubectl config use-context kind-kratix-flux-test

# Check system status
kubectl get promises
kubectl get destinations
kubectl get kustomization -n flux-system
kubectl get bucket -n flux-system

# Check Redis
kubectl get redis
kubectl get redisfailovers
kubectl get pods
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Kind Cluster: kratix-flux-test                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────┐     ┌──────────────┐     ┌───────────────────────────┐ │
│  │ Kratix Promise  │────▶│ MinIO Bucket │◀────│ FluxCD Kustomization      │ │
│  │ Pipeline        │     │ (kratix)     │     │ (watches worker-cluster/) │ │
│  └─────────────────┘     └──────────────┘     └────────────┬──────────────┘ │
│           │                                                │                │
│           ▼                                                ▼                │
│  ┌─────────────────┐                          ┌───────────────────────────┐ │
│  │ Resource Request│                          │ Deployed Resources        │ │
│  │ (redis/my-redis)│                          │ - Redis Operator          │ │
│  └─────────────────┘                          │ - RedisFailover CR        │ │
│                                               │ - Redis + Sentinel Pods   │ │
│                                               └───────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

## What's Installed

| Component | Namespace | Purpose |
|-----------|-----------|---------|
| cert-manager | cert-manager | TLS certificate management for Kratix webhooks |
| Kratix | kratix-platform-system | Platform framework for Promises |
| MinIO | kratix-platform-system | S3-compatible bucket for state storage |
| FluxCD | flux-system | GitOps reconciliation from bucket to cluster |
| Redis Operator | default | Manages Redis instances (installed by Promise) |

## Files

- `setup.yaml` - MinIO, StateStore, Destination, and FluxCD Bucket/Kustomization
- `redis-request.yaml` - Redis resource request (marketplace Promise)
- `nginx-promise.yaml` - Custom NginxApp Promise (demonstrates both workflows)
- `nginx-request.yaml` - NginxApp resource request

## Custom NginxApp Promise

This Promise demonstrates the correct pattern:

```yaml
workflows:
  promise:
    configure:   # Runs once when Promise is installed
      - ...      # Installs dependencies (namespace, operators, CRDs)
  resource:
    configure:   # Runs for each resource request
      - ...      # Creates instance-specific manifests
```

Test it:
```bash
kubectl apply -f nginx-promise.yaml
kubectl apply -f nginx-request.yaml
kubectl get pods -l instance=my-app
kubectl run curl --rm -it --restart=Never --image=curlimages/curl -- curl -s http://nginx-my-app.default.svc.cluster.local
```

## Key Learnings

1. **Both workflows required**: `promise.configure` installs dependencies, `resource.configure` creates instances.

2. **Destination path**: Must match the FluxCD Kustomization path (e.g., `path: worker-cluster` → `path: ./worker-cluster`).

3. **Namespace in manifests**: All generated manifests MUST include `metadata.namespace` for FluxCD to apply them correctly.

4. **Bucket structure**:
   - `dependencies/` - Promise-level resources (operators, CRDs)
   - `resources/<namespace>/<promise>/<name>/` - Instance resources

## Cleanup

```bash
kind delete cluster --name kratix-flux-test
```
