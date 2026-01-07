#!/usr/bin/env python3
import sys
import os

# Ensure shared lib is in path
sys.path.append("/app")

from foundry_lib.kratix_helpers import Pipeline
from foundry_lib.flux_cleanup import cleanup_for_flux
from foundry_lib.foundry_api import check_players
from check_license import check_license
from setup_volume import setup_nfs_volume
from generate_manifests import generate_manifests

def main():
    try:
        pipeline = Pipeline()
        resource = pipeline.resource()
        
        # Step 1: Check License
        is_active, license_data = check_license(pipeline, resource)
        base_domain = license_data.get("baseDomain", "k8s.orb.local")
        
        # Step 2: Setup Volume
        volume_info = setup_nfs_volume(pipeline, resource)
        
        # Step 3: Generate Manifests
        status_updates = generate_manifests(pipeline, resource, volume_info, base_domain)
        
        # Step 4: Add Player Status (if active)
        if is_active:
            print("Checking player status for active instance...")
            # Read admin key from Secret using the helper in generate_manifests
            # (Imports are available since we imported generate_manifests)
            from generate_manifests import get_existing_password
            
            secret_ref = resource.get("spec", {}).get("adminPasswordSecretRef", {})
            secret_name = secret_ref.get("name")
            
            admin_key = None
            if secret_name:
                admin_key = get_existing_password(secret_name, resource["metadata"]["namespace"])
                
            if admin_key:
                hostname = f"{resource['metadata']['name']}.{base_domain}"
                stats = check_players(hostname, admin_key)
                status_updates.update(stats)
            else:
                print(f"WARNING: Admin key secret {secret_name} not found or empty")
            
        # Write status
        current_status = pipeline.metadata("status.yaml")
        current_status.update(status_updates)
        pipeline.write_status(current_status)
            
        # Step 5: Cleanup for FluxCD
        cleanup_for_flux(pipeline)
        
        print("Instance configuration complete")
        
    except Exception as e:
        print(f"FATAL ERROR: {str(e)}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
