#!/usr/bin/env python3
"""
Delete pipeline for FoundryInstance.
Cleans up resources before the instance is removed.
"""
import sys
sys.path.append("/app")

from kubernetes import client, config
from foundry_lib.kratix_helpers import Pipeline

def main():
    pipeline = Pipeline()
    resource = pipeline.resource()
    
    instance_name = resource["metadata"]["name"]
    namespace = resource["metadata"]["namespace"]
    license_name = resource.get("spec", {}).get("licenseRef", {}).get("name")
    storage_backend = resource.get("spec", {}).get("storageBackend", "nfs")
    
    print(f"Delete pipeline running for instance: {instance_name}")
    
    # Initialize k8s client
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    
    custom_api = client.CustomObjectsApi()
    core_api = client.CoreV1Api()
    
    # Step 1: Deactivate if currently active
    if license_name:
        try:
            license_obj = custom_api.get_namespaced_custom_object(
                group="foundry.platform",
                version="v1alpha1",
                namespace=namespace,
                plural="foundrylicenses",
                name=license_name
            )
            
            current_active = license_obj.get("spec", {}).get("activeInstanceName")
            if current_active == instance_name:
                print(f"Instance {instance_name} is currently active, deactivating...")
                # We use a merge patch to set it to null/None
                patch = {"spec": {"activeInstanceName": None}}
                custom_api.patch_namespaced_custom_object(
                    group="foundry.platform",
                    version="v1alpha1",
                    namespace=namespace,
                    plural="foundrylicenses",
                    name=license_name,
                    body=patch
                )
                print(f"Deactivated instance from license {license_name}")
            else:
                print(f"Instance not currently active (active={current_active})")
                
        except Exception as e:
            # If license not found or other error, just log it. 
            # We don't want to fail deletion because of this.
            print(f"Warning: Could not update license: {e}")
    
    # Step 2: Delete PVC if using pvc storage
    if storage_backend == "pvc":
        pvc_name = f"foundry-{instance_name}-data"
        try:
            print(f"Attempting to delete PVC: {pvc_name}")
            core_api.delete_namespaced_persistent_volume_claim(
                name=pvc_name,
                namespace=namespace
            )
            print(f"Deleted PVC: {pvc_name}")
        except client.exceptions.ApiException as e:
            if e.status == 404:
                print(f"PVC {pvc_name} not found (already deleted)")
            else:
                print(f"Warning: Could not delete PVC: {e}")
    else:
        print(f"Storage backend is {storage_backend}, no PVC to delete")
    
    print("Delete cleanup complete")

if __name__ == "__main__":
    main()
