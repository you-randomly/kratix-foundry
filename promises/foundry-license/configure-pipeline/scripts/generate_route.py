from kubernetes import client, config
from foundry_lib.manifest_templates import httproute_template
from foundry_lib.foundry_api import check_players

from typing import Optional

def generate_routes(pipeline, resource: dict, admin_key: Optional[str] = None) -> dict:
    """
    Generate HTTPRoutes and DNSEndpoints for all instances referencing this license.
    Implements switchMode (block/force) logic.
    """
    try:
        config.load_incluster_config()
    except config.ConfigException:
        # Fallback for local testing if running outside cluster
        config.load_kube_config()

    custom_api = client.CustomObjectsApi()
    
    license_name = resource["metadata"]["name"]
    license_ns = resource["metadata"]["namespace"]
    desired_active_name = resource.get("spec", {}).get("activeInstanceName", "")
    switch_mode = resource.get("spec", {}).get("switchMode", "block")
    
    # Get current active instance from status
    status = resource.get("status", {})
    current_active_name = status.get("activeInstance", "")
    
    target_ns = license_ns # Use the same namespace as the license
    
    # Gateway config
    spec_gw = resource.get("spec", {}).get("gateway", {})
    parent_ref = spec_gw.get("parentRef", {})
    gateway_name = parent_ref.get("name", "default-gateway")
    gateway_ns = parent_ref.get("namespace", license_ns) # Default to license namespace
    dns_target = spec_gw.get("dnsTarget", "192.168.139.2")
    base_domain = spec_gw.get("baseDomain", "k8s.orb.local")  # Default for dev

    warning = None
    active_instance_name = desired_active_name

    ANNOTATION_SCHEDULED_DELETE = 'foundry.platform/scheduled-delete-at'

    # Check if we are trying to switch instances
    if current_active_name and desired_active_name and current_active_name != desired_active_name:
        if switch_mode == "block":
            print(f"Switch requested from '{current_active_name}' to '{desired_active_name}' (Mode: block)")
            if admin_key:
                # Live query current instance for players
                hostname = f"foundry-{current_active_name}.{license_ns}.svc.cluster.local"
                stats = check_players(hostname, admin_key)
                players = stats.get("connectedPlayers", 0)
                error = stats.get("error")
                
                if error:
                    print(f"  BLOCKING switch: Could not verify player count for '{current_active_name}' (Error: {error})")
                    active_instance_name = current_active_name
                    warning = f"Switch to '{desired_active_name}' blocked: Unable to verify player count on '{current_active_name}' ({error})"
                elif players > 0:
                    print(f"  BLOCKING switch: {players} players connected to '{current_active_name}'")
                    active_instance_name = current_active_name
                    warning = f"Switch to '{desired_active_name}' blocked: {players} players connected to '{current_active_name}'"
                else:
                    print(f"  Allowing switch: no players connected to '{current_active_name}'")
            else:
                print(f"  BLOCKING switch: No admin_key provided for '{current_active_name}' check")
                active_instance_name = current_active_name
                warning = f"Switch to '{desired_active_name}' blocked: No admin credentials available to check player count"

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
            
        # Check if instance is marked for deletion
        instance_annotations = instance.get("metadata", {}).get("annotations", {})
        scheduled_delete = instance_annotations.get(ANNOTATION_SCHEDULED_DELETE)
        
        name = instance["metadata"]["name"]
        
        if scheduled_delete:
            print(f"  Instance '{name}' is scheduled for deletion, forcing standby")
            
            # If this instance is supposed to be active, block it
            if name == active_instance_name:
                print(f"  WARNING: Active instance '{name}' is scheduled for deletion, blocking activation")
                active_instance_name = current_active_name if current_active_name != name else ""
                warning = f"Instance '{name}' is scheduled for deletion and cannot be active"
            
            # Proceed to generate route (as standby)
            
        found_any = True
        hostname = f"{name}.{base_domain}"
        
        state = "standby"
        backend_service = "foundry-standby-page"
        backend_ns = "foundry-vtt"
        
        if name == active_instance_name:
            print(f"  Instance '{name}' is ACTIVE")
            state = "active"
            backend_service = f"foundry-{name}"
            backend_ns = None # Same namespace as the route
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
            backend_service=backend_service,
            backend_ns=backend_ns
        )
        # Add license label
        route["metadata"].setdefault("labels", {})["license"] = license_name
        pipeline.write_output(f"route-{name}.yaml", route)


        print(f"    Generated identity route for: {name} -> {backend_service}")

    if not found_any:
        print(f"No instances found referencing license {license_name}. No routes generated.")

    return_status = {
        "activeInstance": active_instance_name,
        "registeredInstances": registered_instances
    }
    if warning:
        return_status["warning"] = warning
        
    return return_status
