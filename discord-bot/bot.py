#!/usr/bin/env python3
"""
Kratix Foundry Discord Bot

A Discord bot for managing Foundry VTT instances via Kratix.
"""

import os
import sys
import time
from typing import Optional, List, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from kubernetes import client, config
from kubernetes.client.rest import ApiException

# Add shared library to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
from foundry_lib import foundry_api  # noqa: E402

# Load environment variables
load_dotenv()

# Bot configuration
DISCORD_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
GUILD_ID = os.getenv('DISCORD_GUILD_ID')
FOUNDRY_NAMESPACE = os.getenv('FOUNDRY_NAMESPACE', 'default')

# Kubernetes API configuration
CRD_GROUP = 'foundry.platform'
CRD_VERSION = 'v1alpha1'
CRD_INSTANCE_PLURAL = 'foundryinstances'
CRD_LICENSE_PLURAL = 'foundrylicenses'

# Set up intents (only default - no privileged intents needed for slash commands)
intents = discord.Intents.default()

# Create bot instance
bot = commands.Bot(command_prefix='!', intents=intents)

# Kubernetes client (initialized on startup)
k8s_api: Optional[client.CustomObjectsApi] = None

# Simple TTL cache for autocomplete (reduces API server load)
CACHE_TTL_SECONDS = 5  # How long to cache results
_instances_cache: Dict[str, Any] = {'data': None, 'timestamp': 0}
_licenses_cache: Dict[str, Dict[str, Any]] = {}  # name -> {data, timestamp}


def init_kubernetes() -> bool:
    """Initialize Kubernetes client. Returns True if successful."""
    global k8s_api
    try:
        # Try in-cluster config first, fall back to kubeconfig
        try:
            config.load_incluster_config()
            print('Loaded in-cluster Kubernetes config')
        except config.ConfigException:
            config.load_kube_config()
            print('Loaded kubeconfig from default location')
        
        k8s_api = client.CustomObjectsApi()
        return True
    except Exception as e:
        print(f'WARNING: Failed to initialize Kubernetes client: {e}')
        print('Bot will run but Kubernetes commands will not work')
        return False


def get_foundry_instances(namespace: str = None, use_cache: bool = True) -> List[Dict[str, Any]]:
    """Get all FoundryInstance resources.
    
    Args:
        namespace: Namespace to search in (None for cluster-wide)
        use_cache: If True, use cached results for autocomplete (5 second TTL)
    """
    global _instances_cache
    
    if not k8s_api:
        return []
    
    # Check cache first (for autocomplete)
    if use_cache and _instances_cache['data'] is not None:
        age = time.time() - _instances_cache['timestamp']
        if age < CACHE_TTL_SECONDS:
            return _instances_cache['data']
    
    try:
        if namespace:
            result = k8s_api.list_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural=CRD_INSTANCE_PLURAL
            )
        else:
            result = k8s_api.list_cluster_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                plural=CRD_INSTANCE_PLURAL
            )
        items = result.get('items', [])
        
        # Update cache
        _instances_cache['data'] = items
        _instances_cache['timestamp'] = time.time()
        
        return items
    except ApiException as e:
        print(f'Error listing FoundryInstances: {e}')
        return []


def get_foundry_instance(name: str, namespace: str = None) -> Optional[Dict[str, Any]]:
    """Get a specific FoundryInstance by name."""
    if not k8s_api:
        return None
    
    ns = namespace or FOUNDRY_NAMESPACE
    try:
        return k8s_api.get_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=ns,
            plural=CRD_INSTANCE_PLURAL,
            name=name
        )
    except ApiException as e:
        if e.status == 404:
            return None
        print(f'Error getting FoundryInstance {name}: {e}')
        return None


def get_foundry_license(name: str, namespace: str = None, use_cache: bool = True) -> Optional[Dict[str, Any]]:
    """Get a specific FoundryLicense by name.
    
    Args:
        name: License name
        namespace: Namespace to search in
        use_cache: If True, use cached results (5 second TTL)
    """
    global _licenses_cache
    
    if not k8s_api:
        return None
    
    # Check cache first
    if use_cache and name in _licenses_cache:
        cache_entry = _licenses_cache[name]
        age = time.time() - cache_entry['timestamp']
        if age < CACHE_TTL_SECONDS:
            return cache_entry['data']
    
    ns = namespace or FOUNDRY_NAMESPACE
    try:
        result = k8s_api.get_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=ns,
            plural=CRD_LICENSE_PLURAL,
            name=name
        )
        
        # Update cache
        _licenses_cache[name] = {'data': result, 'timestamp': time.time()}
        
        return result
    except ApiException as e:
        if e.status == 404:
            return None
        print(f'Error getting FoundryLicense {name}: {e}')
        return None


def activate_instance(instance_name: str, namespace: str = None) -> Dict[str, Any]:
    """
    Activate a FoundryInstance by patching its license's activeInstanceName.
    
    Returns dict with 'success', 'message', and optionally 'license_name'.
    """
    if not k8s_api:
        return {'success': False, 'message': 'Kubernetes not connected'}
    
    ns = namespace or FOUNDRY_NAMESPACE
    
    # Get the instance to find its license
    instance = get_foundry_instance(instance_name, ns)
    if not instance:
        return {'success': False, 'message': f'Instance "{instance_name}" not found'}
    
    # Get the license name from the instance
    license_name = instance.get('spec', {}).get('licenseRef', {}).get('name')
    if not license_name:
        return {'success': False, 'message': f'Instance "{instance_name}" has no licenseRef'}
    license = get_foundry_license(license_name, ns)
    if not license:
        return {'success': False, 'message': f'License "{license_name}" not found'}

    # Check if already active
    is_active = license.get('spec', {}).get('activeInstanceName') == instance_name
    if is_active:
        return {'success': True, 'message': f'Instance "{instance_name}" is already active', 'license_name': license_name}
    
    # Patch the license to set activeInstanceName
    try:
        patch_body = {
            'spec': {
                'activeInstanceName': instance_name
            }
        }
        k8s_api.patch_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=ns,
            plural=CRD_LICENSE_PLURAL,
            name=license_name,
            body=patch_body
        )
        return {
            'success': True, 
            'message': f'Activated "{instance_name}" via license "{license_name}"',
            'license_name': license_name
        }
    except ApiException as e:
        error_msg = str(e.reason) if hasattr(e, 'reason') else str(e)
        # Check for CEL validation error (players connected + block mode)
        if e.status == 422:
            return {
                'success': False, 
                'message': f'Switch blocked: Players may be connected and switchMode is "block"',
                'license_name': license_name
            }
        return {'success': False, 'message': f'Failed to patch license: {error_msg}', 'license_name': license_name}


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


@bot.event
async def on_ready():
    """Called when the bot is ready."""
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')
    
    # Initialize Kubernetes
    init_kubernetes()
    
    # Sync commands to guild for faster updates during development
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        # Clear existing commands first to ensure autocomplete updates are picked up
        bot.tree.clear_commands(guild=guild)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f'Commands synced to guild {GUILD_ID} ({len(synced)} commands)')
    else:
        synced = await bot.tree.sync()
        print(f'Commands synced globally ({len(synced)} commands)')




@bot.tree.command(name='vtt-status', description='Show Foundry instance status')
@app_commands.describe(instance='Name of the instance to check (optional)')
async def vtt_status(interaction: discord.Interaction, instance: Optional[str] = None):
    """
    Show instance status (active/standby, players, version).
    
    If no instance is specified, shows status of all instances.
    """
    await interaction.response.defer(thinking=True)
    
    if not k8s_api:
        embed = discord.Embed(
            title='❌ Kubernetes Not Connected',
            description='The bot is not connected to a Kubernetes cluster.',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed)
        return
    
    if instance:
        # Get specific instance
        inst = get_foundry_instance(instance)
        if inst:
            # Look up the license to determine true active status
            license_name = inst.get('spec', {}).get('licenseRef', {}).get('name')
            is_active_from_license = None
            if license_name:
                license_obj = get_foundry_license(license_name)
                if license_obj:
                    active_instance = license_obj.get('spec', {}).get('activeInstanceName')
                    is_active_from_license = (active_instance == instance)
            
            embed = format_instance_embed(inst, is_active_override=is_active_from_license)
            await interaction.followup.send(embed=embed)
        else:
            embed = discord.Embed(
                title='❌ Instance Not Found',
                description=f'No instance named **{instance}** found in namespace **{FOUNDRY_NAMESPACE}**.',
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
    else:
        # List all instances
        instances = get_foundry_instances()
        
        if not instances:
            embed = discord.Embed(
                title='🎲 Foundry Instances',
                description='No instances found. Use `/vtt-create` to create one.',
                color=discord.Color.blue()
            )
            embed.set_footer(text='Powered by Kratix Foundry')
            await interaction.followup.send(embed=embed)
            return
        
        # Build a map of license -> active instance for quick lookup
        license_active_map = {}
        for inst in instances:
            license_name = inst.get('spec', {}).get('licenseRef', {}).get('name')
            if license_name and license_name not in license_active_map:
                license_obj = get_foundry_license(license_name)
                if license_obj:
                    license_active_map[license_name] = license_obj.get('spec', {}).get('activeInstanceName')
        
        # Count active instances using license as source of truth
        active_count = 0
        for inst in instances:
            license_name = inst.get('spec', {}).get('licenseRef', {}).get('name')
            inst_name = inst['metadata']['name']
            if license_name and license_active_map.get(license_name) == inst_name:
                active_count += 1
        
        total_count = len(instances)
        
        summary = discord.Embed(
            title='🎲 Foundry Instances',
            description=f'Found **{total_count}** instance(s) ({active_count} active)',
            color=discord.Color.blue()
        )
        summary.set_footer(text='Powered by Kratix Foundry')
        
        # Add compact list
        for inst in instances[:10]:  # Limit to 10
            name = inst['metadata']['name']
            status = inst.get('status', {})
            players = status.get('connectedPlayers', 0)
            
            # Check license for active status
            license_name = inst.get('spec', {}).get('licenseRef', {}).get('name')
            is_active = (license_name and license_active_map.get(license_name) == name)
            
            status_emoji = '🟢' if is_active else '🟠'
            player_text = f'{players}p' if players >= 0 else '?p'
            
            summary.add_field(
                name=f'{status_emoji} {name}',
                value=f'{player_text}',
                inline=True
            )
        
        await interaction.followup.send(embed=summary)


# Autocomplete handler for vtt_status (method-based pattern from discord.py docs)
@vtt_status.autocomplete('instance')
async def vtt_status_instance_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """Autocomplete callback for instance names."""
    try:
        instances = get_foundry_instances()
        if not instances:
            print(f'Autocomplete: No instances found')
            return []
        
        choices = []
        for inst in instances:
            name = inst['metadata']['name']
            # Filter by current input (case-insensitive)
            if current.lower() in name.lower():
                # Add status indicator to the display name
                license_name = inst.get('spec', {}).get('licenseRef', {}).get('name')
                license_obj = get_foundry_license(license_name) if license_name else None
                is_active = False
                if license_obj:
                    is_active = license_obj.get('spec', {}).get('activeInstanceName') == name
                
                status_emoji = '🟢' if is_active else '🟠'
                choices.append(app_commands.Choice(name=f'{status_emoji} {name}', value=name))
        
        print(f'Autocomplete: returning {len(choices)} choices for "{current}"')
        # Discord limits to 25 choices
        return choices[:25]
    except Exception as e:
        print(f'Autocomplete error: {e}')
        return []


@bot.tree.command(name='vtt-create', description='Create a new Foundry instance')
@app_commands.describe(
    name='Name for the new instance',
    license_name='License to associate with this instance'
)
async def vtt_create(
    interaction: discord.Interaction, 
    name: str,
    license_name: str
):
    """
    Create a new FoundryInstance resource.
    """
    await interaction.response.defer(thinking=True)
    
    # TODO: Create FoundryInstance via Kubernetes API
    embed = discord.Embed(
        title='🎲 Creating Instance',
        description=f'Creating instance **{name}** with license **{license_name}**...',
        color=discord.Color.orange()
    )
    embed.add_field(
        name='⚠️ Not Implemented',
        value='This command is a placeholder. Kubernetes integration coming soon.',
        inline=False
    )
    embed.set_footer(text='Powered by Kratix Foundry')
    
    await interaction.followup.send(embed=embed)


@bot.tree.command(name='vtt-update', description='Update a Foundry instance')
@app_commands.describe(
    instance='Name of the instance to update',
    action='Action to perform (activate)'
)
@app_commands.choices(action=[
    app_commands.Choice(name='activate', value='activate'),
])
async def vtt_update(
    interaction: discord.Interaction, 
    instance: str,
    action: app_commands.Choice[str]
):
    """
    Update a FoundryInstance (e.g., activate it).
    """
    await interaction.response.defer(thinking=True)
    
    if not k8s_api:
        embed = discord.Embed(
            title='❌ Kubernetes Not Connected',
            description='The bot is not connected to a Kubernetes cluster.',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed)
        return
    
    if action.value == 'activate':
        result = activate_instance(instance)
        
        if result['success']:
            embed = discord.Embed(
                title='✅ Instance Activated',
                description=result['message'],
                color=discord.Color.green()
            )
            if 'license_name' in result:
                embed.add_field(name='License', value=result['license_name'], inline=True)
            embed.set_footer(text='The Kratix pipeline will update routes shortly')
        else:
            embed = discord.Embed(
                title='❌ Activation Failed',
                description=result['message'],
                color=discord.Color.red()
            )
            if 'license_name' in result:
                embed.add_field(name='License', value=result['license_name'], inline=True)
        
        await interaction.followup.send(embed=embed)
    else:
        embed = discord.Embed(
            title='❓ Unknown Action',
            description=f'Unknown action: {action.value}',
            color=discord.Color.orange()
        )
        await interaction.followup.send(embed=embed)


@bot.tree.command(name='vtt-delete', description='Delete a Foundry instance')
@app_commands.describe(name='Name of the instance to delete')
async def vtt_delete(interaction: discord.Interaction, name: str):
    """
    Mark instance for deletion (with grace period).
    """
    await interaction.response.defer(thinking=True)
    
    # TODO: Delete FoundryInstance via Kubernetes API
    embed = discord.Embed(
        title='🗑️ Deleting Instance',
        description=f'Scheduling deletion of **{name}**...',
        color=discord.Color.red()
    )
    embed.add_field(
        name='⚠️ Not Implemented',
        value='This command is a placeholder. Kubernetes integration coming soon.',
        inline=False
    )
    embed.set_footer(text='Powered by Kratix Foundry')
    
    await interaction.followup.send(embed=embed)


def main():
    """Run the bot."""
    if not DISCORD_TOKEN:
        print('ERROR: DISCORD_BOT_TOKEN environment variable not set')
        print('Copy .env.example to .env and add your bot token')
        sys.exit(1)
    
    bot.run(DISCORD_TOKEN)


if __name__ == '__main__':
    main()

