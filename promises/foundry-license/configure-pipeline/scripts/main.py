#!/usr/bin/env python3
import sys
import os

# Ensure shared lib is in path
sys.path.append("/app")

from foundry_lib.kratix_helpers import Pipeline
from validate_license import validate_license
from generate_route import generate_routes

def main():
    try:
        pipeline = Pipeline()
        resource = pipeline.resource()
        
        # Step 1: Validate License
        validate_license(resource)
        
        # Get Admin Key for live checks
        admin_key = None
        admin_key_path = "/etc/foundry/credentials/adminPassword"
        if os.path.exists(admin_key_path):
            with open(admin_key_path, 'r') as f:
                admin_key = f.read().strip()
        else:
            print("WARNING: Admin key not found at /etc/foundry/credentials/adminPassword")

        # Step 2: Generate Routes (and handle switch logic)
        status = generate_routes(pipeline, resource, admin_key=admin_key)
        
        # Step 3: Check player status of the active instance if it exists
        active_name = status.get("activeInstance")
        if active_name and admin_key:
            print(f"Checking player status for active instance '{active_name}'...")
            hostname = f"foundry-{active_name}.{resource['metadata']['namespace']}.svc.cluster.local"
            from foundry_lib.foundry_api import check_players
            stats = check_players(hostname, admin_key)
            status.update({"activeInstanceStats": stats})

        # Write status back to Kratix
        pipeline.write_status(status)
        
        print("Licence validation and routing update complete")
        
    except Exception as e:
        print(f"FATAL ERROR: {str(e)}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
