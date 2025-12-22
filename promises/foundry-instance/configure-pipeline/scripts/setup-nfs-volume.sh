#!/bin/bash
set -euo pipefail

# setup-nfs-volume.sh
# Creates or references NFS volumes for instance data

echo "Setting up NFS volume paths..."

RESOURCE=$(cat /kratix/input/object.yaml)
INSTANCE_NAME=$(echo "$RESOURCE" | yq '.metadata.name')

# NFS server configuration
NFS_SERVER="192.168.200.184"
NFS_BASE="/volume1/foundry"

# Determine data volume path (auto-generate if not specified)
DATA_PATH=$(echo "$RESOURCE" | yq '.spec.nfsBasePath // ""')
if [[ -z "$DATA_PATH" || "$DATA_PATH" == "null" ]]; then
  DATA_PATH="${NFS_BASE}/instances/${INSTANCE_NAME}"
fi

# Simplified paths (always relative to DATA_PATH for now)
PLUGIN_PATH="${DATA_PATH}/Data/modules"
WORLD_PATH="${DATA_PATH}/Data/worlds"

echo "Volume paths resolved:"
echo "  Data: $DATA_PATH"
echo "  Plugins: $PLUGIN_PATH"
echo "  Worlds: $WORLD_PATH"

STORAGE_BACKEND=$(echo "$RESOURCE" | yq '.spec.storageBackend // "nfs"')

# Save volume info for manifest generation
cat > /kratix/metadata/volume-info.yaml <<EOF
nfsServer: "$NFS_SERVER"
dataPath: "$DATA_PATH"
pluginPath: "$PLUGIN_PATH"
worldPath: "$WORLD_PATH"
storageBackend: "$STORAGE_BACKEND"
EOF

cp /kratix/input/object.yaml /kratix/output/object.yaml

