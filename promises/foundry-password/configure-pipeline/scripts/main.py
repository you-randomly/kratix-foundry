#!/usr/bin/env python3
"""
FoundryPassword Pipeline - Main entry point.

This pipeline generates an ExternalSecret that uses the ClusterGenerator
to create a random password. The ExternalSecret won't auto-refresh; 
manual refresh is triggered by annotating with force-sync.
"""
import sys
from datetime import datetime, timezone

sys.path.append("/app")

from foundry_lib.kratix_helpers import Pipeline


def external_secret_template(password_name: str, namespace: str, secret_name: str, labels: dict = None) -> dict:
    """
    Generates an ExternalSecret resource that uses the ClusterGenerator to create passwords.
    The secret won't auto-refresh (refreshInterval: 0).
    Manual refresh is triggered by annotating with force-sync.
    """
    template_labels = {
        "foundry.platform/password": password_name,
        "managed-by": "kratix"
    }
    if labels:
        template_labels.update(labels)

    return {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {
            "name": secret_name,
            "namespace": namespace,
            "labels": template_labels
        },
        "spec": {
            "refreshInterval": "0",  # No automatic refresh
            "target": {
                "name": secret_name,
                "template": {
                    "data": {
                        "adminPassword": "{{ .password }}"
                    }
                }
            },
            "dataFrom": [
                {
                    "sourceRef": {
                        "generatorRef": {
                            "apiVersion": "generators.external-secrets.io/v1alpha1",
                            "kind": "ClusterGenerator",
                            "name": "foundry-password"
                        }
                    }
                }
            ]
        }
    }


def main():
    try:
        pipeline = Pipeline()
        resource = pipeline.resource()
        
        password_name = resource["metadata"]["name"]
        namespace = resource["metadata"]["namespace"]
        metadata = resource.get("metadata", {})
        spec = resource.get("spec", {})
        
        password_type = spec.get("type", "default")
        instance_ref = spec.get("instanceRef", {})
        instance_name = instance_ref.get("name", "")
        
        # Propagate labels from the FoundryPassword resource
        # These help the Discord bot background task identify the owner and type
        labels = {
            "foundry.platform/password-type": password_type
        }
        if instance_name:
            labels["foundry.platform/instance"] = instance_name
        
        # Capture owner info if present
        owner_id = metadata.get("labels", {}).get("foundry.platform/owner-id")
        if not owner_id:
            # Fallback to annotation
            owner_id = metadata.get("annotations", {}).get("foundry.platform/owner-id")
        
        if owner_id:
            labels["foundry.platform/owner-id"] = owner_id

        # Determine secret name: use the resource name directly
        # It's already prefixed by the bot (e.g., foundry-password-user-123 or foundry-password-my-inst)
        secret_name = password_name
        
        print(f"Configuring FoundryPassword: {password_name}")
        print(f"  Type: {password_type}")
        print(f"  Secret Name: {secret_name}")
        if instance_name:
            print(f"  Instance: {instance_name}")
        
        # Generate ExternalSecret
        external_secret = external_secret_template(password_name, namespace, secret_name, labels)
        pipeline.write_output("external-secret.yaml", external_secret)
        print(f"Generated ExternalSecret: {secret_name} with labels: {labels}")
        
        # Update status
        now = datetime.now(timezone.utc).isoformat()
        status = {
            "phase": "Ready",
            "secretName": secret_name,
            "lastRefreshed": now,
        }
        
        # Only set createdAt if this is a new resource (check if status exists)
        existing_status = resource.get("status", {})
        if not existing_status.get("createdAt"):
            status["createdAt"] = now
            # Flag for Discord bot to send notification
            status["passwordPendingNotification"] = True
            print("New password generated - flagging for Discord notification")
        else:
            status["createdAt"] = existing_status["createdAt"]
            # Clear flag if it was there (bot should have handled it or we don't want to re-notify)
            status["passwordPendingNotification"] = False
        
        pipeline.write_status(status)
        print(f"Password configuration complete. Secret: {secret_name}")
        
    except Exception as e:
        print(f"FATAL ERROR: {str(e)}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
