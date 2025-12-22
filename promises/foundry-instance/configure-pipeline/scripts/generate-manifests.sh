#!/bin/bash
set -euo pipefail

# generate-manifests.sh
# Generates Kubernetes manifests for FoundryInstance (Deployment, Service, HTTPRoute, etc.)

echo "Generating Kubernetes manifests..."

RESOURCE=$(cat /kratix/input/object.yaml)
VOLUME_INFO=$(cat /kratix/metadata/volume-info.yaml)

# Extract configuration
INSTANCE_NAME=$(echo "$RESOURCE" | yq '.metadata.name')
NAMESPACE="foundry-vtt"
FOUNDRY_VERSION=$(echo "$RESOURCE" | yq '.spec.foundryVersion // "13.347.0"')
CPU=$(echo "$RESOURCE" | yq '.spec.resources.cpu // "100m"')
MEMORY=$(echo "$RESOURCE" | yq '.spec.resources.memory // "256Mi"')
PROXY_SSL=$(echo "$RESOURCE" | yq '.spec.proxySSL // true')
PROXY_PORT=$(echo "$RESOURCE" | yq '.spec.proxyPort // 443')

# Volume config
NFS_SERVER=$(echo "$VOLUME_INFO" | yq '.nfsServer')
DATA_PATH=$(echo "$VOLUME_INFO" | yq '.dataPath')
STORAGE_BACKEND=$(echo "$VOLUME_INFO" | yq '.storageBackend // "nfs"')



# Define Volume Source based on backend
if [[ "$STORAGE_BACKEND" == "pvc" ]]; then
  # PVC Source
  VOLUME_DEF="persistentVolumeClaim:
          claimName: foundry-${INSTANCE_NAME}-data"
          
  # Generate PVC Resource
  cat > /kratix/output/pvc.yaml <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: foundry-${INSTANCE_NAME}-data
  namespace: ${NAMESPACE}
  labels:
    app: foundry-vtt
    instance: ${INSTANCE_NAME}
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
EOF
else
  # NFS Source (Default)
  VOLUME_DEF="nfs:
          server: \"${NFS_SERVER}\"
          path: \"${DATA_PATH}\""
fi

# Generate Deployment
cat > /kratix/output/deployment.yaml <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: foundry-${INSTANCE_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: foundry-vtt
    instance: ${INSTANCE_NAME}
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: foundry-vtt
      instance: ${INSTANCE_NAME}
  template:
    metadata:
      labels:
        app: foundry-vtt
        instance: ${INSTANCE_NAME}
    spec:
      initContainers:
      - name: volume-permissions
        image: busybox:1.36
        command: ["sh", "-c", "chown -R 1000:1000 /data"]
        volumeMounts:
        - name: data
          mountPath: /data
      containers:
      - name: foundry-vtt
        image: felddy/foundryvtt:${FOUNDRY_VERSION}
        ports:
        - containerPort: 30000
        securityContext:
          allowPrivilegeEscalation: false
          runAsNonRoot: false
          seccompProfile:
            type: RuntimeDefault
        resources:
          requests:
            cpu: ${CPU}
            memory: ${MEMORY}
        env:
        - name: UV_THREADPOOL_SIZE
          value: "6"
        - name: CONTAINER_CACHE
          value: /data/container_cache
        - name: TIMEZONE
          value: UTC
        - name: FOUNDRY_HOSTNAME
          value: ${HOSTNAME}
        - name: FOUNDRY_LOCAL_HOSTNAME
          value: ${HOSTNAME}
        - name: FOUNDRY_PROXY_SSL
          value: "${PROXY_SSL}"
        - name: FOUNDRY_PROXY_PORT
          value: "${PROXY_PORT}"
        - name: FOUNDRY_USERNAME
          valueFrom:
            secretKeyRef:
              name: foundry-credentials
              key: username
        - name: FOUNDRY_PASSWORD
          valueFrom:
            secretKeyRef:
              name: foundry-credentials
              key: password
        - name: FOUNDRY_ADMIN_KEY
          valueFrom:
            secretKeyRef:
              name: foundry-credentials
              key: adminPassword
        - name: FOUNDRY_LICENSE_KEY
          valueFrom:
            secretKeyRef:
              name: foundry-license
              key: license-key
        volumeMounts:
        - name: data
          mountPath: /data
      volumes:
      - name: data
        ${VOLUME_DEF}
EOF

# Generate Service
cat > /kratix/output/service.yaml <<EOF
apiVersion: v1
kind: Service
metadata:
  name: foundry-${INSTANCE_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: foundry-vtt
    instance: ${INSTANCE_NAME}
spec:
  selector:
    app: foundry-vtt
    instance: ${INSTANCE_NAME}
  ports:
    - protocol: TCP
      port: 80
      targetPort: 30000
  type: ClusterIP
EOF

echo "Manifests generated for instance: ${INSTANCE_NAME}"




