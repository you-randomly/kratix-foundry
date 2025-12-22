from foundry_lib.manifest_templates import deployment_template, service_template, pvc_template, rbac_templates

def generate_manifests(pipeline, resource: dict, volume_info: dict):
    """
    Generates Kubernetes manifests for FoundryInstance (Deployment, Service, PVC, RBAC, etc.)
    Ported from generate-manifests.sh
    """
    instance_name = resource["metadata"]["name"]
    namespace = resource["metadata"]["namespace"]
    spec = resource.get("spec", {})
    
    # We'll use the pipeline image itself for the monitor as it has all dependencies
    monitor_image = "kratix-foundry-instance-configure:dev"
    
    version = spec.get("foundryVersion", "13.347.0")
    resources = spec.get("resources", {})
    cpu = resources.get("cpu", "100m")
    memory = resources.get("memory", "256Mi")
    proxy_ssl = spec.get("proxySSL", True)
    proxy_port = spec.get("proxyPort", 443)
    
    hostname = f"{instance_name}.k8s.orb.local"
    
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
        monitor_image=monitor_image
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
