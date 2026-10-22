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

## Candidate: MetaController for license routing

MetaController is a better fit than a Kro resource graph for the license's
cross-resource routing workflow. The `FoundryLicense` would remain the desired
control record (including its license Secret reference and requested active
instance), while a MetaController sync hook would calculate and reconcile the
per-instance `HTTPRoute` and optional `DNSEndpoint` resources.

The controller must reconcile when either the license changes or a related
`FoundryInstance` is created, updated, or deleted. Since instances currently
refer to a license through `spec.licenseRef` rather than an ownership
relationship, the implementation needs a reliable way to find the related
instances, such as adding a license label or watching instances and filtering
by their spec reference.

The sync behavior should be idempotent: derive the desired routes from the
license and current instance set, and report the effective active instance and
switch warnings in status. For `switchMode: block`, it must retain the current
active route if the player count cannot be checked or players are connected.
Requests to Foundry should be bounded and treated as read-only checks; sync
retries must not accidentally accept a rejected activation. Scheduled-for-
deletion instances must remain ineligible for activation.

This is a migration design, not a deployed MetaController implementation. Do
not run it alongside the Kratix license pipeline as a second owner of the same
routes or DNS records. A cutover should first verify the controller's event
coverage and behavior, then transfer ownership of those outputs from Kratix.

The pilot intentionally leaves all current Kratix installation and production
manifests unchanged. A complete cutover should preserve the existing
`foundry.platform` API contract and migrate license orchestration before
removing Kratix.
