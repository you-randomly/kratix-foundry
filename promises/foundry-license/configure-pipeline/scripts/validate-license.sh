#!/bin/bash
set -euo pipefail

# validate-license.sh
# Validates that the license secret reference exists and is properly formatted

echo "Validating FoundryLicense resource..."

# Read the resource from Kratix input
RESOURCE=$(cat /kratix/input/object.yaml)

# Extract secret reference
SECRET_NAME=$(echo "$RESOURCE" | yq '.spec.licenseSecretRef.name')
SECRET_KEY=$(echo "$RESOURCE" | yq '.spec.licenseSecretRef.key')
LICENSE_NAME=$(echo "$RESOURCE" | yq '.metadata.name')

if [[ -z "$SECRET_NAME" || "$SECRET_NAME" == "null" ]]; then
  echo "ERROR: licenseSecretRef.name is required"
  exit 1
fi

if [[ -z "$SECRET_KEY" || "$SECRET_KEY" == "null" ]]; then
  echo "ERROR: licenseSecretRef.key is required"
  exit 1
fi

echo "License '$LICENSE_NAME' validated successfully"
echo "  Secret: $SECRET_NAME (key: $SECRET_KEY)"

# License validation only - no output manifests needed
# The license resource itself is managed by Kratix, not deployed to workers
echo "License validation complete (no output manifests)"

