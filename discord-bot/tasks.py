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
    """Background task to notify users of generated/refreshed passwords.
    
    Monitors FoundryPassword resources and sends DM notifications
    when a password has been generated or refreshed by kro and ESO.
    """
    if not k8s.is_connected():
        return
        
    try:
        passwords = k8s.get_foundry_passwords(namespace=k8s.FOUNDRY_NAMESPACE)
        
        for fp in passwords:
            try:
                status = fp.get('status', {})
                # Check if ready (phase is Ready or ready condition is True)
                if status.get('phase') != 'Ready' and not status.get('ready'):
                    continue
                
                fp_name = fp['metadata']['name']
                namespace = fp['metadata'].get('namespace', k8s.FOUNDRY_NAMESPACE)
                metadata = fp.get('metadata', {})
                labels = metadata.get('labels', {}) or {}
                annotations = metadata.get('annotations', {}) or {}
                
                refresh_time = status.get('lastRefreshed')
                if not refresh_time:
                    continue
                
                # Check if we already notified for this refresh
                last_notified = annotations.get(PASSWORD_NOTIFIED_ANNOTATION)
                if last_notified and str(last_notified) >= str(refresh_time):
                    continue
                
                # Identification
                spec = fp.get('spec', {})
                password_type = spec.get('type', 'default')
                instance_name = spec.get('instanceRef', {}).get('name') or labels.get('foundry.platform/instance')
                creator_id = labels.get('foundry.platform/owner-id') or annotations.get('foundry.platform/owner-id')
                
                if not creator_id and fp_name.startswith("foundry-password-user-"):
                    creator_id = fp_name[len("foundry-password-user-"):]
                
                if not creator_id and instance_name:
                    instance = k8s.get_foundry_instance(instance_name, namespace)
                    if instance:
                        creator_id = k8s.get_instance_owner_id(instance)
                
                if not creator_id:
                    continue
                
                # Retrieve plaintext password
                password = k8s.get_admin_password_plaintext(fp_name, namespace)
                if not password:
                    continue
                
                # Send DM to creator
                try:
                    user = await bot.fetch_user(int(creator_id))
                    if user:
                        if password_type == 'instance' and instance_name:
                            title = '🔑 Admin Password for Foundry Instance'
                            description = f"The admin password for instance **{instance_name}** has been generated/reset."
                            color = discord.Color.green()
                        else:
                            title = '🔑 Default Admin Password'
                            description = "Your **default** admin password has been generated/reset."
                            color = discord.Color.blue()
                            
                        embed = discord.Embed(title=title, description=description, color=color)
                        
                        if instance_name:
                            embed.add_field(name="Instance", value=instance_name, inline=True)
                        
                        embed.add_field(name="Password", value=f"||`{password}`||", inline=False)
                        
                        if instance_name:
                            instance = k8s.get_foundry_instance(instance_name, namespace)
                            if instance:
                                license_name = instance.get('spec', {}).get('licenseRef', {}).get('name')
                                base_domain = "lab.jake-watson.co.uk"
                                if license_name:
                                    lic = k8s.get_foundry_license(license_name, namespace)
                                    if lic:
                                        base_domain = lic.get('spec', {}).get('gateway', {}).get('baseDomain', base_domain)
                                embed.add_field(name="URL", value=f"https://{instance_name}.{base_domain}", inline=False)

                        footer = "Keep this password safe! Click to unmask. Applies automatically once safe."
                        embed.set_footer(text=footer)
                        
                        await user.send(embed=embed)
                        print(f"Sent {password_type} password DM to user {creator_id} for {instance_name or 'default'}")
                        
                        # Mark as notified by annotating the FoundryPassword
                        k8s.annotate_foundry_password(
                            fp_name,
                            {PASSWORD_NOTIFIED_ANNOTATION: str(refresh_time)},
                            namespace
                        )
                        print(f"Marked FoundryPassword {fp_name} as notified at {refresh_time}")
                        
                except discord.NotFound:
                    print(f"User {creator_id} not found, cannot send password DM")
                except discord.Forbidden:
                    print(f"Cannot send DM to user {creator_id} (DMs disabled)")
                    # Annotate so we don't retry endlessly
                    k8s.annotate_foundry_password(
                        fp_name,
                        {PASSWORD_NOTIFIED_ANNOTATION: str(refresh_time)},
                        namespace
                    )
                except Exception as e:
                    print(f"Failed to send DM to user {creator_id}: {e}")
                    
            except Exception as e:
                print(f"Error processing FoundryPassword {fp.get('metadata', {}).get('name')}: {e}")
                
    except Exception as e:
        print(f"Error in check_password_notifications: {e}")
