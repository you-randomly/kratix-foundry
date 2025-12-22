#!/bin/bash
set -euo pipefail

# generate-route.sh
# Generates identity HTTPRoutes and DNSEndpoints for all instances referencing this license

echo "Generating routing manifests for associated instances..."

RESOURCE=$(cat /kratix/input/object.yaml)
LICENSE_NAME=$(echo "$RESOURCE" | yq '.metadata.name')
LICENSE_NS=$(echo "$RESOURCE" | yq '.metadata.namespace')
ACTIVE_INSTANCE_NAME=$(echo "$RESOURCE" | yq '.spec.activeInstanceName // ""')
NAMESPACE="foundry-vtt"

# Gateway config (Fetch from License or use defaults)
GATEWAY_NAME=$(echo "$RESOURCE" | yq '.spec.gateway.parentRef.name // "default-gateway"')
GATEWAY_NS=$(echo "$RESOURCE" | yq '.spec.gateway.parentRef.namespace // "foundry-vtt"')
DNS_TARGET=$(echo "$RESOURCE" | yq '.spec.gateway.dnsTarget // "192.168.139.2"')

# List all associated instances in the same namespace as the license
INSTANCES_JSON=$(kubectl get foundryinstance -n "$LICENSE_NS" -o json)
ASSOCIATED_INSTANCES=$(echo "$INSTANCES_JSON" | jq -c --arg LICENSE "$LICENSE_NAME" '.items[] | select(.spec.licenseRef.name == $LICENSE)')


REGISTERED_INSTANCES="[]"

if [[ -z "$ASSOCIATED_INSTANCES" ]]; then
  echo "No instances found referencing license $LICENSE_NAME. No routes will be generated."
else
  echo "Found instances for license $LICENSE_NAME. Generating routes..."
  
  while read -r INSTANCE; do
    NAME=$(echo "$INSTANCE" | jq -r '.metadata.name')
    HOSTNAME="${NAME}.k8s.orb.local"
    
    STATE="standby"
    BACKEND_SERVICE="foundry-standby-page"
    
    if [[ "$NAME" == "$ACTIVE_INSTANCE_NAME" ]]; then
      echo "Instance $NAME is ACTIVE"
      STATE="active"
      BACKEND_SERVICE="foundry-${NAME}"
    else
      echo "Instance $NAME is STANDBY"
    fi

    # Register for status surfacing
    REGISTERED_INSTANCES=$(echo "$REGISTERED_INSTANCES" | jq --arg name "$NAME" --arg state "$STATE" '. += [{"name": $name, "state": $state}]')

    # Generate HTTPRoute for this instance identity
    cat > "/kratix/output/route-${NAME}.yaml" <<EOF
apiVersion: gateway.networking.k8s.io/v1beta1
kind: HTTPRoute
metadata:
  name: foundry-id-${NAME}
  namespace: ${NAMESPACE}
  labels:
    app: foundry-vtt
    instance: ${NAME}
    license: ${LICENSE_NAME}
spec:
  hostnames:
    - ${HOSTNAME}
  parentRefs:
    - name: ${GATEWAY_NAME}
      namespace: ${GATEWAY_NS}
  rules:
    - backendRefs:
        - name: ${BACKEND_SERVICE}
          port: 80
EOF

    # Generate DNSEndpoint for this instance identity
    cat > "/kratix/output/dns-${NAME}.yaml" <<EOF
apiVersion: externaldns.k8s.io/v1alpha1
kind: DNSEndpoint
metadata:
  name: foundry-id-${NAME}
  namespace: ${NAMESPACE}
spec:
  endpoints:
    - dnsName: ${HOSTNAME}
      recordTTL: 300
      recordType: A
      targets:
        - ${DNS_TARGET}
EOF

    echo "  Generated identity route for: ${NAME} -> ${BACKEND_SERVICE}"
  done <<< "$ASSOCIATED_INSTANCES"
fi

# Surface status back to Kratix
cat > /kratix/metadata/status.yaml <<EOF
activeInstance: "${ACTIVE_INSTANCE_NAME}"
registeredInstances: ${REGISTERED_INSTANCES}
EOF

