# Kro migration pilot

This repository is still Kratix-based. The Kro ResourceGraphDefinition here is an
opt-in pilot for the part that maps cleanly to a resource graph: a Foundry
Deployment, Service, and PVC. It uses the separate `foundry.kro.run` API group
so the existing Kratix-generated `foundry.platform` APIs are not overwritten.

## Try the pilot

Install Kro by following the [official installation guide](https://kro.run/docs/getting-started/Installation),
then apply the graph and its example:

```bash
kubectl apply -f manifests/kro/foundry-workload.yaml
kubectl wait --for=condition=Ready rgd/foundry-workload.foundry.kro.run --timeout=120s
kubectl apply -f examples/foundry-workload-kro.yaml
```

The pilot expects `foundry-vtt/foundry-credentials` with `username`,
`password`, and `adminPassword` keys, plus `foundry-vtt/foundry-license` with a
`license-key` key. It provisions a PVC; configure a suitable default StorageClass
before creating an instance.

This example is not a replacement for `FoundryInstance`. Do not create both
resources for the same Foundry instance: the pilot does not connect to the
license-routing workflow and does not create identity routes or DNS records.

## What can move to Kro

- The Foundry workload's Deployment, Service, and PVC are declarative
  Kubernetes resources and are represented directly in the graph.
- The graph surfaces available replicas from the Deployment.
- Foundry application secrets remain Kubernetes Secrets and are referenced by
  name; Kro does not replace secret management.

## What still needs a controller or workflow

- `FoundryLicense` currently lists associated `FoundryInstance` resources,
  checks connected players, validates license changes, and updates status.
  That cross-resource decision logic is not a simple resource template.
- NFS provisioning currently creates directories on a mounted export, while
  the pilot supports PVC storage only.
- The status monitor writes back to the Kratix `FoundryInstance` API, and
  password notification/cleanup behavior also depends on the existing
  pipelines.
- Kratix currently provides the production API and reconciles license routing.
  Removing it requires migrating these behaviors and planning a CRD/data
  handoff, not just replacing Promise YAML.

The pilot intentionally leaves all current Kratix installation and production
manifests unchanged. A complete cutover should preserve the existing
`foundry.platform` API contract and migrate license orchestration before
removing Kratix.
