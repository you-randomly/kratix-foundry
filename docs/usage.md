# Foundry VTT License-Managed Platform

A Kratix-based platform for managing Foundry VTT instances with automated license compliance.

## Overview

This platform enforces Foundry VTT's licensing requirement that only one instance per license can be publicly accessible at a time. It provides:

- **License tracking** via `FoundryLicense` resources
- **Instance management** via `FoundryInstance` resources
- **Automatic routing** - active instances serve Foundry, standby instances show a landing page
- **Self-service switchover** with player detection

## Quick Start

### 1. Install Kratix

Apply the Kratix ArgoCD application:

```bash
kubectl apply -f manifests/kratix.yaml
```

### 2. Install the Promises

```bash
kubectl apply -f promises/foundry-license/promise.yaml
kubectl apply -f promises/foundry-instance/promise.yaml
```

### 3. Deploy the Standby Page

```bash
kubectl apply -f manifests/foundry-standby-page.yaml
```

### 4. Create a License

```yaml
apiVersion: foundry.platform/v1alpha1
kind: FoundryLicense
metadata:
  name: my-license
spec:
  licenseSecretRef:
    name: foundry-secrets
    key: license-key
  owner: "gm@example.com"
```

### 5. Create Instances

```yaml
apiVersion: foundry.platform/v1alpha1
kind: FoundryInstance
metadata:
  name: main-campaign
spec:
  licenseRef:
    name: my-license
  requestActive: true
  subdomain: main
  foundryVersion: "13.347.0"
```

## Instance Configuration

### Activation

| Field | Description |
|-------|-------------|
| `requestActive` | Set to `true` to request this instance become active |
| `switchoverMode` | How this instance can be displaced: `block`, `queue`, or `force` |

### Volume Modes

Each of `plugins` and `worlds` can be configured independently:

| Mode | Description |
|------|-------------|
| `new` | Creates empty NFS folder (default) |
| `cloned` | Copies from another instance at creation time |
| `shared` | RW mount of a shared pool |

**Transition rules:**
- `new` → `shared`: Only at creation
- `cloned` → `shared`: Only at creation  
- `shared` → `cloned`: Anytime (copies data to new folder)

### Shared Volume Configuration

```yaml
plugins:
  mode: shared
  # Option 1: Share with another instance
  sharedWithInstance: main-campaign
  # Option 2: Explicit NFS path
  # sharedNfsPath: "/volume1/foundry/shared/plugins-pool-a"
```

## Switchover Modes

The `switchoverMode` controls how THIS instance can be displaced when another requests activation:

| Mode | Behavior |
|------|----------|
| `block` | Reject if players are connected (default) |
| `queue` | Queue request, auto-switch when session ends |
| `force` | Allow immediate switchover |

## Directory Structure

```
ruby-cosmos/
├── manifests/
│   ├── kratix.yaml                  # Kratix installation
│   └── foundry-standby-page.yaml    # Standby page deployment
├── promises/
│   ├── foundry-license/             # License Promise
│   └── foundry-instance/            # Instance Promise
├── standby-page/                    # Standby page web app
├── examples/                        # Example resources
└── docs/
    └── usage.md                     # This file
```

## Building Pipeline Images

```bash
# License pipeline
cd promises/foundry-license/configure-pipeline
docker build -t ghcr.io/you-randomly/foundry-platform/license-pipeline:latest .

# Instance pipeline
cd promises/foundry-instance/configure-pipeline
docker build -t ghcr.io/you-randomly/foundry-platform/instance-pipeline:latest .

# Standby page
cd standby-page
docker build -t ghcr.io/you-randomly/foundry-platform/standby-page:latest .
```

### Local Testing (Kind)

Since you are testing locally, you don't need to push to GHCR yet. Instead, load the images directly into your Kind cluster:

```bash
# Load images into Kind
kind load docker-image ghcr.io/you-randomly/foundry-platform/license-pipeline:latest --name kratix-test
kind load docker-image ghcr.io/you-randomly/foundry-platform/instance-pipeline:latest --name kratix-test
kind load docker-image ghcr.io/you-randomly/foundry-platform/standby-page:latest --name kratix-test
```

*Note: Replace `kratix-test` with your actual Kind cluster name.*

## NFS Structure

All instance data is stored on NFS at `192.168.200.184`:

```
/volume1/foundry/
├── instances/           # Per-instance data (auto-created)
│   ├── main-campaign/
│   └── backup/
├── shared/              # Shared volume pools
│   ├── plugins/
│   └── worlds/
└── cloned/              # Point-in-time clones
    └── main-20251221/
```
