from kubernetes import client, config
from foundry_lib.manifest_templates import httproute_template, dnsendpoint_template

def generate_routes(pipeline, resource: dict) -> dict:
    """
    Generate HTTPRoutes and DNSEndpoints for all instances referencing this license.
    Ported from generate-route.sh
    """
    try:
        config.load_incluster_config()
    except config.ConfigException:
        # Fallback for local testing if running outside cluster
        config.load_kube_config()

    custom_api = client.CustomObjectsApi()
    
    license_name = resource["metadata"]["name"]
    license_ns = resource["metadata"]["namespace"]
    active_instance_name = resource.get("spec", {}).get("activeInstanceName", "")
    target_ns = license_ns # Use the same namespace as the license
    
    # Gateway config
    spec_gw = resource.get("spec", {}).get("gateway", {})
    parent_ref = spec_gw.get("parentRef", {})
    gateway_name = parent_ref.get("name", "default-gateway")
    gateway_ns = parent_ref.get("namespace", license_ns) # Default to license namespace
    dns_target = spec_gw.get("dnsTarget", "192.168.139.2")

    print(f"Generating routing manifests for instances associated with license '{license_name}'...")
    
    # List all instances in the same namespace as the license
    instances = custom_api.list_namespaced_custom_object(
        group="foundry.platform",
        version="v1alpha1",
        namespace=license_ns,
        plural="foundryinstances"
    )
    
    registered_instances = []
    found_any = False

    for instance in instances.get("items", []):
        instance_license = instance.get("spec", {}).get("licenseRef", {}).get("name")
        if instance_license != license_name:
            continue
            
        found_any = True
        name = instance["metadata"]["name"]
        hostname = f"{name}.k8s.orb.local"
        
        state = "standby"
        backend_service = "foundry-standby-page"
        
        if name == active_instance_name:
            print(f"  Instance '{name}' is ACTIVE")
            state = "active"
            backend_service = f"foundry-{name}"
        else:
            print(f"  Instance '{name}' is STANDBY")

        registered_instances.append({"name": name, "state": state})

        # Generate HTTPRoute
        route = httproute_template(
            name=name,
            namespace=target_ns,
            hostname=hostname,
            gateway_name=gateway_name,
            gateway_ns=gateway_ns,
            backend_service=backend_service
        )
        # Add license label
        route["metadata"]["labels"]["license"] = license_name
        pipeline.write_output(f"route-{name}.yaml", route)

        # Generate DNSEndpoint
        dns = dnsendpoint_template(
            name=name,
            namespace=target_ns,
            hostname=hostname,
            dns_target=dns_target
        )
        pipeline.write_output(f"dns-{name}.yaml", dns)

        print(f"    Generated identity route for: {name} -> {backend_service}")

    if not found_any:
        print(f"No instances found referencing license {license_name}. No routes generated.")

    return {
        "activeInstance": active_instance_name,
        "registeredInstances": registered_instances
    }
