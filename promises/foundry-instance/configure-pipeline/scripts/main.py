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

def get_admin_password_from_secret(secret_name: str, namespace: str) -> str:
    """Read admin password from the secret managed by ESO."""
    try:
        from kubernetes import client, config
        import base64
        
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        
        v1 = client.CoreV1Api()
        secret = v1.read_namespaced_secret(secret_name, namespace)
        if secret.data and 'adminPassword' in secret.data:
            return base64.b64decode(secret.data['adminPassword']).decode('utf-8')
    except Exception as e:
        print(f"Could not read admin password from secret {secret_name}: {e}")
    return None

def main():
    try:
        pipeline = Pipeline()
        resource = pipeline.resource()
        
        # Step 1: Check License
        is_active, license_data = check_license(pipeline, resource)
        base_domain = license_data.get("baseDomain", "k8s.orb.local")
        
        # Step 2: Setup Volume
        volume_info = setup_nfs_volume(pipeline, resource)
        
        # Step 3: Generate Manifests (including ExternalSecret for password)
        generate_manifests(pipeline, resource, volume_info, base_domain)
        
        # Step 4: Add Player Status (if active)
        status_updates = {}
        if is_active:
            print("Checking player status for active instance...")
            
            # Get secret name (same logic as generate_manifests)
            secret_ref = resource.get("spec", {}).get("adminPasswordSecretRef", {})
            secret_name = secret_ref.get("name")
            if not secret_name:
                secret_name = f"foundry-credentials-{resource['metadata']['name']}"
            
            # Note: On first creation, the secret may not exist yet (ESO creates it async)
            # The player check will fail gracefully in that case
            admin_key = get_admin_password_from_secret(secret_name, resource["metadata"]["namespace"])
                
            if admin_key:
                hostname = f"{resource['metadata']['name']}.{base_domain}"
                stats = check_players(hostname, admin_key)
                status_updates.update(stats)
            else:
                print(f"WARNING: Admin key secret {secret_name} not found or empty (may be pending ESO sync)")
            
        # Write status updates
        pipeline.write_status(status_updates)
            
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
