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
  namespace: foundry-vtt
spec:
  licenseRef:
    name: my-license
  foundryVersion: "13.347.0"
  storageBackend: pvc  # or nfs
```

## Instance Configuration

### Activation

Instances are activated/deactivated via the parent `FoundryLicense.spec.activeInstanceName` field, not directly on the instance.

### Storage Backend

| Value | Description |
|-------|-------------|
| `nfs` | Uses shared NFS storage (default) |
| `pvc` | Creates a dedicated PersistentVolumeClaim |

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

## Real-time Status Monitoring

Each instance automatically receives a `monitor` sidecar container that polls the Foundry VTT API every 60 seconds.

### Reported Fields

| Field | Description |
|-------|-------------|
| `connectedPlayers` | Number of currently logged-in users |
| `activeWorld` | Name of the active world (e.g. `standby-world`) |
| `lastSidecarUpdate` | Timestamp of the last successful poll |

## Switchover Modes

## Directory Structure

```
kratix-foundry/
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

Both pipelines are implemented in Python. Since they share a common library (`lib/foundry_lib`), you must build them from the repository root:

```bash
# License pipeline
docker build -t kratix-foundry-license-configure:dev -f promises/foundry-license/configure-pipeline/Dockerfile .

# Instance pipeline
docker build -t kratix-foundry-instance-configure:dev -f promises/foundry-instance/configure-pipeline/Dockerfile .
```

### Local Testing (Orbstack/Kind)

For local testing, the images built above are automatically available if using Orbstack's built-in Kubernetes. If using Kind, load them manually:

```bash
kind load docker-image kratix-foundry-license-configure:dev --name kratix-test
kind load docker-image kratix-foundry-instance-configure:dev --name kratix-test
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
