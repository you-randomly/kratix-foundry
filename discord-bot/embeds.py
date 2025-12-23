"""
Discord embed formatting utilities for Foundry instances.
"""

from typing import Any, Dict, Optional

import discord


def format_instance_embed(instance: Dict[str, Any], is_active_override: Optional[bool] = None) -> discord.Embed:
    """Format a FoundryInstance as a Discord embed.
    
    Args:
        instance: The FoundryInstance resource dict
        is_active_override: If provided, use this instead of instance status (for license-based truth)
    """
    name = instance['metadata']['name']
    namespace = instance['metadata']['namespace']
    spec = instance.get('spec', {})
    status = instance.get('status', {})
    
    # Determine status color and emoji
    # Use override if provided (license is source of truth), otherwise fall back to instance status
    if is_active_override is not None:
        is_active = is_active_override
    else:
        is_active = status.get('isActive', False)
    
    if is_active:
        color = discord.Color.green()
        status_text = '🟢 Active'
    else:
        color = discord.Color.orange()
        status_text = '🟠 Standby'
    
    embed = discord.Embed(
        title=f'🎲 {name}',
        color=color
    )
    
    # Status fields
    embed.add_field(name='Status', value=status_text, inline=True)
    
    # Player count
    players = status.get('connectedPlayers', 0)
    if players < 0:
        player_text = '❓ Unknown'
    elif players == 0:
        player_text = '0 connected'
    else:
        player_text = f'{players} connected'
    embed.add_field(name='Players', value=player_text, inline=True)
    
    # World status - show world name if available
    world_name = status.get('activeWorld')  # This is now the world name string
    if world_name and world_name is not True:  # Handle both string and legacy boolean
        world_text = f'🌍 {world_name}'
    else:
        world_text = '⚫ No active world'
    embed.add_field(name='World', value=world_text, inline=True)
    
    # Spec info
    version = spec.get('foundryVersion', 'Unknown')
    embed.add_field(name='Version', value=version, inline=True)
    
    license_ref = spec.get('licenseRef', {}).get('name', 'Unknown')
    embed.add_field(name='License', value=license_ref, inline=True)
    
    storage = spec.get('storageBackend', 'nfs')
    embed.add_field(name='Storage', value=storage.upper(), inline=True)
    
    # Last update
    last_update = status.get('lastSidecarUpdate')
    if last_update:
        embed.set_footer(text=f'Namespace: {namespace} • Last update: {last_update}')
    else:
        embed.set_footer(text=f'Namespace: {namespace}')
    
    return embed
