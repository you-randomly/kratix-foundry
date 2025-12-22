#!/bin/bash
set -euo pipefail

# check-license.sh
# Validates that the referenced FoundryLicense exists and checks active instance status

echo "Checking license reference..."

RESOURCE=$(cat /kratix/input/object.yaml)
NAMESPACE=$(echo "$RESOURCE" | yq '.metadata.namespace')
INSTANCE_NAME=$(echo "$RESOURCE" | yq '.metadata.name')
LICENSE_NAME=$(echo "$RESOURCE" | yq '.spec.licenseRef.name')

if [[ -z "$LICENSE_NAME" || "$LICENSE_NAME" == "null" ]]; then
  echo "ERROR: licenseRef.name is required"
  exit 1
fi

# Fetch the FoundryLicense to see if we are the active instance
set +e
LICENSE_JSON=$(kubectl get foundrylicense "$LICENSE_NAME" -n "$NAMESPACE" -o json 2>/dev/null)
FETCH_EXIT=$?
set -e

IS_ACTIVE="false"
if [[ $FETCH_EXIT -eq 0 ]]; then
  ACTIVE_INSTANCE_NAME=$(echo "$LICENSE_JSON" | jq -r '.spec.activeInstanceName // ""')
  if [[ "$ACTIVE_INSTANCE_NAME" == "$INSTANCE_NAME" ]]; then
    echo "This instance IS active for license $LICENSE_NAME"
    IS_ACTIVE="true"
  else
    echo "This instance is NOT active"
  fi
  
  # Trigger License reconciliation to ensure routes are updated
  echo "Touching license $LICENSE_NAME to trigger routing update..."
  TIMESTAMP=$(date +%s)
  kubectl patch foundrylicense "$LICENSE_NAME" -n "$NAMESPACE" --type=merge -p "{\"metadata\":{\"annotations\":{\"foundry.platform/reconcile\":\"$TIMESTAMP\"}}}"
else
  echo "WARNING: Could not fetch FoundryLicense $LICENSE_NAME. Defaulting to inactive."
fi

# Surface status back to Kratix
cat > /kratix/metadata/status.yaml <<EOF
isActive: $IS_ACTIVE
EOF

# Copy input to output for next stage
cp /kratix/input/object.yaml /kratix/output/object.yaml



