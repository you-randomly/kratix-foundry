from kubernetes import client, config
import base64
import secrets
import string
from foundry_lib.manifest_templates import deployment_template, service_template, pvc_template, rbac_templates, credentials_secret_template

def generate_random_password(length=24):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for i in range(length))

def get_existing_password(name, namespace):
    try:
        # Load config if not already loaded (idempotent)
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
            
        v1 = client.CoreV1Api()
        secret = v1.read_namespaced_secret(name, namespace)
        if secret.data and 'adminPassword' in secret.data:
            return base64.b64decode(secret.data['adminPassword']).decode('utf-8')
    except Exception:
        pass
    return None

def generate_manifests(pipeline, resource: dict, volume_info: dict, base_domain: str = "k8s.orb.local"):
    """
    Generates Kubernetes manifests for FoundryInstance.
    Returns a dict of status updates (e.g. password notification pending).
    """
    instance_name = resource["metadata"]["name"]
    namespace = resource["metadata"]["namespace"]
    spec = resource.get("spec", {})
    
    # We'll use the pipeline image itself for the monitor as it has all dependencies
    monitor_image = "ghcr.io/you-randomly/kratix-foundry/instance-pipeline:latest"
    
    version = spec.get("foundryVersion", "13.347.0")
    resources = spec.get("resources", {})
    cpu = resources.get("cpu", "100m")
    memory = resources.get("memory", "256Mi")
    proxy_ssl = spec.get("proxySSL", True)
    proxy_port = spec.get("proxyPort", 443)
    
    hostname = f"{instance_name}.{base_domain}"
    
    # Secret Logic
    secret_ref = spec.get("adminPasswordSecretRef", {})
    secret_name = secret_ref.get("name")
    if not secret_name:
        # Fallback default if not specified (should only happen during migration/dev)
        secret_name = f"foundry-credentials-{instance_name}"
        
    regenerate = spec.get("regeneratePassword", False)
    
    admin_password = None
    status_updates = {}
    
    # 1. Check existing
    existing_pw = get_existing_password(secret_name, namespace)
    
    # 2. Decide: Reuse or Generate
    if existing_pw and not regenerate:
        print(f"Reusing existing password from secret: {secret_name}")
        admin_password = existing_pw
    else:
        print(f"Generating new password for secret: {secret_name} (Regenerate={regenerate})")
        admin_password = generate_random_password()
        status_updates["passwordPendingNotification"] = True
        
    # 3. Create Secret via Kubernetes API (not via Kratix output)
    # This avoids Flux conflicts when multiple instances share a secret
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    
    v1 = client.CoreV1Api()
    
    if existing_pw and not regenerate:
        # Secret exists and not regenerating, skip creation
        print(f"Secret '{secret_name}' exists, skipping creation")
    elif existing_pw and regenerate:
        # Secret exists AND regenerating - update it
        import base64
        secret_data = {
            "adminPassword": base64.b64encode(admin_password.encode()).decode()
        }
        v1.patch_namespaced_secret(secret_name, namespace, {"data": secret_data})
        print(f"Regenerated secret via API: {secret_name}")
    else:
        # Secret doesn't exist - create it (regardless of regenerate flag)
        import base64
        secret_data = {
            "adminPassword": base64.b64encode(admin_password.encode()).decode()
        }
        secret_body = client.V1Secret(
            metadata=client.V1ObjectMeta(name=secret_name, namespace=namespace),
            data=secret_data,
            type="Opaque"
        )
        try:
            v1.create_namespaced_secret(namespace, secret_body)
            print(f"Created secret via API: {secret_name}")
        except client.exceptions.ApiException as e:
            if e.status == 409:
                # Secret was created by another instance (race), just use it
                print(f"Secret '{secret_name}' already exists (created by another instance)")
            else:
                raise
    
    
    storage_backend = volume_info.get("storageBackend", "nfs")
    
    volume_def = {}
    if storage_backend == "pvc":
        # Generate PVC
        pvc = pvc_template(instance_name, namespace)
        pipeline.write_output(f"pvc.yaml", pvc)
        volume_def = {"persistentVolumeClaim": {"claimName": f"foundry-{instance_name}-data"}}
    else:
        # NFS Source (Default)
        volume_def = {
            "nfs": {
                "server": volume_info["nfsServer"],
                "path": volume_info["dataPath"]
            }
        }
        
    # Generate Deployment with Sidecar
    deployment = deployment_template(
        name=instance_name,
        namespace=namespace,
        version=version,
        cpu=cpu,
        memory=memory,
        hostname=hostname,
        proxy_ssl=proxy_ssl,
        proxy_port=proxy_port,
        volume_def=volume_def,
        admin_secret_name=secret_name,
        monitor_image=monitor_image,
        storage_backend=storage_backend
    )
    pipeline.write_output("deployment.yaml", deployment)
    
    # Generate Service
    service = service_template(instance_name, namespace)
    pipeline.write_output("service.yaml", service)

    # Generate RBAC for monitor
    rbac = rbac_templates(instance_name, namespace)
    for i, resource in enumerate(rbac):
        kind = resource["kind"].lower()
        pipeline.write_output(f"rbac-{kind}.yaml", resource)
    
    print(f"Manifests (including sidecar monitor) generated for instance: {instance_name}")
    return status_updates
