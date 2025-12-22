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
        is_active = check_license(pipeline, resource)
        
        # Step 2: Setup Volume
        volume_info = setup_nfs_volume(pipeline, resource)
        
        # Step 3: Generate Manifests
        generate_manifests(pipeline, resource, volume_info)
        
        # Step 4: Add Player Status (if active)
        if is_active:
            print("Checking player status for active instance...")
            # Read admin key from mount
            admin_key_path = "/etc/foundry/credentials/adminPassword"
            if os.path.exists(admin_key_path):
                with open(admin_key_path, 'r') as f:
                    admin_key = f.read().strip()
                
                hostname = f"{resource['metadata']['name']}.k8s.orb.local"
                stats = check_players(hostname, admin_key)
                
                # Merge stats into status
                current_status = pipeline.metadata("status.yaml")
                current_status.update(stats)
                pipeline.write_status(current_status)
            else:
                print("WARNING: Admin key not found at /etc/foundry/credentials/adminPassword")
            
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
