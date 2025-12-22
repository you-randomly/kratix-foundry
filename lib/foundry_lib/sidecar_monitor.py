import os
import time
import sys
from kubernetes import client, config
from foundry_lib.foundry_api import check_players

def monitor_loop():
    # Load configuration
    namespace = os.getenv("POD_NAMESPACE", "default")
    instance_name = os.getenv("INSTANCE_NAME")
    admin_key_path = "/etc/foundry/credentials/adminPassword"
    
    if not instance_name:
        print("FATAL: INSTANCE_NAME env var not set")
        sys.exit(1)

    print(f"Starting status monitor for {instance_name} in {namespace}...", flush=True)

    # Initialize K8s client
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    
    custom_api = client.CustomObjectsApi()
    
    while True:
        try:
            # 1. Read Admin Key
            if not os.path.exists(admin_key_path):
                print(f"Waiting for admin key at {admin_key_path}...")
                time.sleep(10)
                continue
                
            with open(admin_key_path, 'r') as f:
                admin_key = f.read().strip()
            
            # 2. Check Players (Localhost)
            # Since we are in a sidecar, localhost:30000 is the main container
            stats = check_players("localhost:30000", admin_key)
            
            print(f"DEBUG: stats received: {stats}", flush=True)
            
            # 3. Update FoundryInstance Status
            # We use a patch to update just the status fields
            body = {
                "status": {
                    "connectedPlayers": stats.get("connectedPlayers", 0),
                    "activeWorld": stats.get("worldActive", False),
                    "lastSidecarUpdate": stats.get("checkedAt"),
                    "error": stats.get("error") # Track if there was an error
                }
            }
            
            print(f"Updating status for {instance_name}: players={body['status']['connectedPlayers']}, world={body['status']['activeWorld']}, error={body['status']['error']}", flush=True)
            
            custom_api.patch_namespaced_custom_object_status(
                group="foundry.platform",
                version="v1alpha1",
                namespace=namespace,
                plural="foundryinstances",
                name=instance_name,
                body=body
            )
            print("Successfully patched status.", flush=True)
            
        except Exception as e:
            print(f"Error in monitor loop: {str(e)}", flush=True)
            
        # Poll every 60 seconds
        time.sleep(60)

if __name__ == "__main__":
    monitor_loop()
