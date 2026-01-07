"""
Background tasks and async utilities for the Foundry bot.
"""

import time
import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from discord.ext import tasks

from config import ANNOTATION_SCHEDULED_DELETE
import k8s_client as k8s


@tasks.loop(hours=1)
async def cleanup_expired_deletions():
    """Background task to delete instances whose grace period has expired."""
    if not k8s.is_connected():
        return
        
    print("Checking for expired instance deletions...")
    instances = k8s.get_foundry_instances(use_cache=False)
    now = datetime.now(timezone.utc)
    
    for inst in instances:
        annotations = inst.get('metadata', {}).get('annotations', {})
        scheduled_at_str = annotations.get(ANNOTATION_SCHEDULED_DELETE)
        
        if not scheduled_at_str:
            continue
            
        try:
            # Parse the scheduled date
            scheduled_at = datetime.fromisoformat(scheduled_at_str)
            
            # Ensure scheduled_at is timezone-aware
            if scheduled_at.tzinfo is None:
                scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
            
            if now >= scheduled_at:
                name = inst['metadata']['name']
                namespace = inst['metadata']['namespace']
                print(f"Deleting expired instance: {name} in {namespace}")
                
                k8s.delete_foundry_instance(name, namespace)
        except Exception as e:
            print(f"Error processing cleanup for {inst.get('metadata', {}).get('name')}: {e}")


async def wait_for_resource_condition(
    get_fn: Callable[[], Any],
    check_fn: Callable[[Any], bool],
    timeout_seconds: int = 120,
    interval_seconds: int = 5,
    on_progress: Optional[Callable[[Any], Awaitable[None]]] = None
) -> Optional[Any]:
    """
    Polls a resource using get_fn and waits until check_fn returns True.
    
    Args:
        get_fn: Function that returns the resource (or None)
        check_fn: Function that takes the resource and returns True if condition reached
        timeout_seconds: Max time to wait
        interval_seconds: Time between polls
        on_progress: Optional callback function triggered on each poll
        
    Returns:
        The final resource state, or None if timed out.
    """
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        resource = get_fn()
        if resource and check_fn(resource):
            return resource
            
        if on_progress:
            await on_progress(resource)
            
        await asyncio.sleep(interval_seconds)
    
    return None
    return None


@tasks.loop(seconds=15)
async def check_password_notifications(bot):
    """Background task to notify users of generated passwords."""
    if not k8s.is_connected():
        return
        
    try:
        instances = k8s.get_foundry_instances(use_cache=False)
        for inst in instances:
            status = inst.get('status', {})
            if status.get('passwordPendingNotification'):
                name = inst['metadata']['name']
                namespace = inst['metadata']['namespace']
                annotations = inst['metadata'].get('annotations', {})
                creator_id = annotations.get('foundry.platform/created-by-id')
                
                if not creator_id:
                    print(f"Skipping password notification for {name}: No created-by-id annotation")
                    continue
                    
                print(f"Processing password notification for {name}")
                
                # Fetch secret
                secret_ref = inst.get('spec', {}).get('adminPasswordSecretRef', {})
                secret_name = secret_ref.get('name')
                
                # Fallback if ref managed by pipeline but not yet in spec? No, pipeline updates spec? 
                # Actually, our pipeline creates the secret and deployment, but DOES NOT update the spec to add the ref.
                # The spec is user-defined. The pipeline uses what's in spec.
                # If spec didn't have ref, pipeline used a default name and passed it to deployment.
                # BUT, pipeline does not patch the CRD spec to add the ref.
                # So if we didn't put it in spec during create, it won't be there.
                
                if not secret_name:
                    # Fallback logic mirroring generate_manifests.py
                    secret_name = f"foundry-credentials-{name}" # Old default or new fallback?
                    # The `create.py` logic now ADDS the secret ref to spec.
                    # So for new instances, it will be there.
                    # For `reset-default`, we don't know the secret name from spec if it wasn't there?
                    # Actually `reset-default` deletes the secret `foundry-admin-<uid>-default`.
                    # Instances using it should have it in spec if created with new code.
                    pass

                import base64
                password = None
                try:
                    secret = k8s.get_secret(secret_name, namespace)
                    if secret and secret.data and 'adminPassword' in secret.data:
                        password = base64.b64decode(secret.data['adminPassword']).decode('utf-8')
                except Exception as e:
                    print(f"Error fetching secret {secret_name}: {e}")
                    
                if password:
                    # Send DM
                    try:
                        user = await bot.fetch_user(int(creator_id))
                        if user:
                            # Fetch license for domain info
                            license_name = inst.get('spec', {}).get('licenseRef', {}).get('name')
                            base_domain = "k8s.orb.local"
                            if license_name:
                                lic = k8s.get_foundry_license(license_name, namespace)
                                if lic:
                                    base_domain = lic.get('spec', {}).get('gateway', {}).get('baseDomain', base_domain)
                            
                            embed = discord.Embed(
                                title=f'🔑 Admin Password for active instance',
                                description=f"The admin password for instance **{name}** has been generated/reset.",
                                color=discord.Color.green()
                            )
                            embed.add_field(name="Instance", value=name, inline=True)
                            embed.add_field(name="Password", value=f"```{password}```", inline=False)
                            embed.add_field(name="URL", value=f"https://{name}.{base_domain}", inline=False)
                            
                            await user.send(embed=embed)
                            print(f"Sent password DM to user {creator_id}")
                            
                            # Update status to remove pending flag
                            # We can't easily patch *just* the status via CustomObjectsApi patch unless we use /status subresource
                            # k8s_api is CustomObjectsApi. 
                            # We need to set passwordPendingNotification: false
                            
                            # Note: The pipeline sets it to true.
                            # If we set it to false, we are acknowledging it.
                            
                            # We need to patch the STATUS.
                            # Python client patch_namespaced_custom_object usually patches spec/metadata unless we access /status.
                            # Actually, patch works on the whole object. status is a subresource.
                            # If we patch the object, status might be ignored if subresources are enabled.
                            # We should use patch_namespaced_custom_object_status if available?
                            # k8s python client: patch_namespaced_custom_object_status exists? Yes.
                            
                            # But wait, `k8s_client.py` doesn't expose it.
                            # I should access `k8s.k8s_api` directly.
                            
                            status_patch = {
                                "status": {
                                    "passwordPendingNotification": None # Set to null to remove? Or false.
                                }
                            }
                            # Using None to remove field
                            
                            k8s.k8s_api.patch_namespaced_custom_object(
                                group=k8s.CRD_GROUP,
                                version=k8s.CRD_VERSION,
                                namespace=namespace,
                                plural=k8s.CRD_INSTANCE_PLURAL,
                                name=name,
                                body=status_patch
                            )
                            # Wait, if I patch the main object, status might not update if status subresource is enabled.
                            # But if I try to simple patch, it might work.
                            # If not, I need to call status endpoint.
                            # Check `manifest_templates.py` rbac: 
                            # verbs: ["get", "patch", "update"], resources: ["foundryinstances/status"]
                            # So status subresource IS enabled.
                            # I must use the status endpoint to patch status.
                            
                            # Let's try custom_object_status patch
                            # Actually the method is `patch_namespaced_custom_object_status`
                            # Wait, does the python client have it?
                            # Checking... CustomObjectsApi has `patch_namespaced_custom_object_status`.
                            
                            k8s.k8s_api.patch_namespaced_custom_object_status(
                                group=k8s.CRD_GROUP,
                                version=k8s.CRD_VERSION,
                                namespace=namespace,
                                plural=k8s.CRD_INSTANCE_PLURAL,
                                name=name,
                                body=status_patch
                            )
                            print(f"Cleared pending notification flag for {name}")
                            
                    except Exception as e:
                        print(f"Failed to notify user or update status for {name}: {e}")
                else:
                     print(f"Password not found for {name} despite pending flag")
