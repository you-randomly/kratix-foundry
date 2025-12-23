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
