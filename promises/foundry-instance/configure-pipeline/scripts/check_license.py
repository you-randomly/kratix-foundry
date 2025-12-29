import sys
from kubernetes import client, config

def check_license(pipeline, resource: dict) -> bool:
    """
    Validates that the referenced FoundryLicense exists and checks active instance status.
    Ported from check-license.sh
    """
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    custom_api = client.CustomObjectsApi()
    
    namespace = resource["metadata"]["namespace"]
    instance_name = resource["metadata"]["name"]
    license_ref = resource.get("spec", {}).get("licenseRef", {})
    license_name = license_ref.get("name")

    if not license_name or license_name == "null":
        print("ERROR: licenseRef.name is required", file=sys.stderr)
        sys.exit(1)

    print(f"Checking license reference '{license_name}'...")

    is_active = False
    license_data = {"baseDomain": "k8s.orb.local"}  # Default for dev
    try:
        # Fetch the FoundryLicense
        license_obj = custom_api.get_namespaced_custom_object(
            group="foundry.platform",
            version="v1alpha1",
            namespace=namespace,
            plural="foundrylicenses",
            name=license_name
        )
        
        # Extract gateway config including baseDomain
        gateway = license_obj.get("spec", {}).get("gateway", {})
        license_data["baseDomain"] = gateway.get("baseDomain", "k8s.orb.local")
        
        active_instance = license_obj.get("spec", {}).get("activeInstanceName", "")
        if active_instance == instance_name:
            print(f"This instance IS active for license {license_name}")
            is_active = True
        else:
            print(f"This instance is NOT active")

        # Trigger License reconciliation to ensure routes are updated
        # Must use a LABEL (not annotation) with value "true" for Kratix to detect
        print(f"Touching license {license_name} to trigger routing update...")
        patch = {
            "metadata": {
                "labels": {
                    "kratix.io/manual-reconciliation": "true"
                }
            }
        }
        custom_api.patch_namespaced_custom_object(
            group="foundry.platform",
            version="v1alpha1",
            namespace=namespace,
            plural="foundrylicenses",
            name=license_name,
            body=patch
        )

    except client.exceptions.ApiException as e:
        if e.status == 404:
            print(f"WARNING: Could not find FoundryLicense {license_name}. Defaulting to inactive.")
        else:
            print(f"ERROR: Failed to fetch FoundryLicense: {e.reason}", file=sys.stderr)
            # We don't exit here to maintain bash behavior of defaulting to inactive
    
    # Surface status back to Kratix
    pipeline.write_status({"isActive": is_active})
    
    return is_active, license_data
