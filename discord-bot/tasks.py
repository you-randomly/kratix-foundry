"""
Background tasks and async utilities for the Foundry bot.
"""

import time
import asyncio
import base64
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from discord.ext import tasks
import discord

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


# Annotation used to track when we last notified about a password
PASSWORD_NOTIFIED_ANNOTATION = "foundry.platform/password-notified-at"


@tasks.loop(seconds=15)
async def check_password_notifications(bot):
    """Background task to notify users of generated/refreshed passwords via ESO.
    
    Monitors ExternalSecrets for Foundry instances and sends DM notifications
    when a password has been synced (either created or refreshed).
    """
    if not k8s.is_connected():
        return
        
    try:
        # Get all ExternalSecrets with foundry.platform/instance label
        es_list = k8s.list_external_secrets(
            namespace=k8s.FOUNDRY_NAMESPACE,
            label_selector="foundry.platform/instance"
        )
        
        for es in es_list:
            try:
                # Check if synced (Ready=True)
                if not k8s.is_external_secret_synced(es):
                    continue
                
                es_name = es['metadata']['name']
                namespace = es['metadata']['namespace']
                annotations = es.get('metadata', {}).get('annotations', {})
                
                # Get the refresh time from ESO status
                refresh_time = k8s.get_external_secret_refresh_time(es)
                if not refresh_time:
                    continue
                
                # Check if we already notified for this refresh
                last_notified = annotations.get(PASSWORD_NOTIFIED_ANNOTATION)
                if last_notified and last_notified >= refresh_time:
                    continue
                
                # Get instance name from label
                instance_name = es['metadata'].get('labels', {}).get('foundry.platform/instance')
                if not instance_name:
                    continue
                
                # Get the instance to find creator
                instance = k8s.get_foundry_instance(instance_name, namespace)
                if not instance:
                    print(f"Instance {instance_name} not found for ExternalSecret {es_name}")
                    continue
                
                creator_id = instance['metadata'].get('annotations', {}).get('foundry.platform/created-by-id')
                if not creator_id:
                    print(f"No creator ID for instance {instance_name}, skipping notification")
                    continue
                
                # Get password from the secret created by ESO
                secret_name = es.get('spec', {}).get('target', {}).get('name')
                if not secret_name:
                    secret_name = es_name  # ESO defaults to ExternalSecret name
                
                secret = k8s.get_secret(secret_name, namespace)
                if not secret or not secret.data or 'adminPassword' not in secret.data:
                    print(f"Secret {secret_name} not found or missing adminPassword")
                    continue
                
                password = base64.b64decode(secret.data['adminPassword']).decode('utf-8')
                
                # Send DM to creator
                try:
                    user = await bot.fetch_user(int(creator_id))
                    if user:
                        # Get license for domain info
                        license_name = instance.get('spec', {}).get('licenseRef', {}).get('name')
                        base_domain = "k8s.orb.local"
                        if license_name:
                            lic = k8s.get_foundry_license(license_name, namespace)
                            if lic:
                                base_domain = lic.get('spec', {}).get('gateway', {}).get('baseDomain', base_domain)
                        
                        embed = discord.Embed(
                            title='🔑 Admin Password for Foundry Instance',
                            description=f"The admin password for instance **{instance_name}** has been generated/reset.",
                            color=discord.Color.green()
                        )
                        embed.add_field(name="Instance", value=instance_name, inline=True)
                        embed.add_field(name="Password", value=f"```{password}```", inline=False)
                        embed.add_field(name="URL", value=f"https://{instance_name}.{base_domain}", inline=False)
                        embed.set_footer(text="Keep this password safe! You can reset it anytime with /vtt-password reset-instance")
                        
                        await user.send(embed=embed)
                        print(f"Sent password DM to user {creator_id} for instance {instance_name}")
                        
                        # Mark as notified by annotating the ExternalSecret
                        k8s.annotate_external_secret(
                            es_name,
                            {PASSWORD_NOTIFIED_ANNOTATION: refresh_time},
                            namespace
                        )
                        print(f"Marked ExternalSecret {es_name} as notified at {refresh_time}")
                        
                except discord.NotFound:
                    print(f"User {creator_id} not found, cannot send password DM")
                except discord.Forbidden:
                    print(f"Cannot send DM to user {creator_id} (DMs disabled)")
                except Exception as e:
                    print(f"Failed to send DM to user {creator_id}: {e}")
                    
            except Exception as e:
                print(f"Error processing ExternalSecret {es.get('metadata', {}).get('name')}: {e}")
                
    except Exception as e:
        print(f"Error in check_password_notifications: {e}")
