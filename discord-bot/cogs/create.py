"""
/vtt-create command cog - Create new Foundry instances.
"""

from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from embeds import format_instance_embed
from tasks import wait_for_resource_condition
import k8s_client as k8s
from utils.versions import get_foundry_versions


class CreateCog(commands.Cog):
    """Cog containing the /vtt-create command."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    @app_commands.command(name='vtt-create', description='Create a new Foundry instance')
    @app_commands.describe(
        name='Unique name for this instance (e.g., "my-campaign")',
        license_name='The license to use for this instance',
        foundry_version='Foundry version (e.g., "12.331")',
        storage_backend='Storage type for data',
        cpu='CPU request/limit (e.g., "500m")',
        memory='Memory request/limit (e.g., "1Gi")',
        unique_password='Generate a unique password for this instance instead of using your default'
    )
    @app_commands.choices(storage_backend=[
        app_commands.Choice(name='NFS (shared storage)', value='nfs'),
        app_commands.Choice(name='PVC (dedicated volume)', value='pvc'),
    ])
    async def vtt_create(
        self,
        interaction: discord.Interaction, 
        name: str,
        license_name: str,
        foundry_version: Optional[str] = None,
        storage_backend: Optional[app_commands.Choice[str]] = None,
        cpu: Optional[str] = None,
        memory: Optional[str] = None,
        unique_password: bool = False
    ):
        """Create a new FoundryInstance resource."""
        await interaction.response.defer(thinking=True)
        
        # Get valid versions
        valid_versions, stable_version = await get_foundry_versions()
        
        # Default to stable version if not provided
        if not foundry_version:
            if stable_version:
                foundry_version = stable_version
            else:
                # Fallback if fetching failed
                foundry_version = "12.331" 
        
        # Validate version
        if valid_versions and foundry_version not in valid_versions:
            # Check if it's at least a valid format (numeric) as a fallback
            import re
            if not re.match(r'^\d+(\.\d+)+$', foundry_version):
                embed = discord.Embed(
                    title='❌ Invalid Version',
                    description=f'Version **{foundry_version}** is not valid. Please select a version from the list.',
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return
            
            # Warn but allow if it looks like a version (could be a new one not yet in cache)
            embed = discord.Embed(
                title='❌ Invalid Version',
                description=f'Version **{foundry_version}** is not in the supported list.\nSupported versions: {", ".join(valid_versions[:5])}...',
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return
        
        if not k8s.is_connected():
            embed = discord.Embed(
                title='❌ Kubernetes Not Connected',
                description='The bot is not connected to a Kubernetes cluster.',
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return
        
        # Validate license exists
        lic = k8s.get_foundry_license(license_name)
        if not lic:
            embed = discord.Embed(
                title='❌ License Not Found',
                description=f'No license named **{license_name}** exists. Check available licenses with autocomplete.',
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return
            
        # Determine Secret Name
        admin_secret_name = None
        if unique_password:
            admin_secret_name = f"foundry-admin-{name}"
        else:
            admin_secret_name = f"foundry-admin-{interaction.user.id}-default"
        
        # Create the instance
        result = k8s.create_foundry_instance(
            name=name,
            license_name=license_name,
            foundry_version=foundry_version,
            storage_backend=storage_backend.value if storage_backend else None,
            cpu=cpu,
            memory=memory,
            created_by_id=str(interaction.user.id),
            created_by_name=str(interaction.user),
            admin_password_secret_name=admin_secret_name
        )
        
        if result['success']:
            # Send initial progress message
            progress_embed = discord.Embed(
                title='🔧 Creating Instance...',
                description=f'Instance **{name}** is being provisioned. This usually takes 30-60 seconds.',
                color=discord.Color.blue()
            )
            progress_embed.add_field(name='Name', value=name, inline=True)
            progress_embed.add_field(name='License', value=license_name, inline=True)
            if unique_password:
                progress_embed.add_field(name='Password', value="Generating unique...", inline=True)
            else:
                progress_embed.add_field(name='Password', value="Using default key", inline=True)
                
            progress_embed.set_footer(text='Waiting for Kratix pipeline to reconcile...')
            
            msg = await interaction.followup.send(embed=progress_embed)
            
            # Wait for the instance to be ready
            def check_ready(inst):
                status = inst.get('status', {})
                # Consider ready if we have a lastSidecarUpdate or connectedPlayers field
                return status.get('lastSidecarUpdate') is not None or 'connectedPlayers' in status
            
            final_inst = await wait_for_resource_condition(
                get_fn=lambda: k8s.get_foundry_instance(name),
                check_fn=check_ready,
                timeout_seconds=120,
                interval_seconds=10
            )
            
            password_status_msg = ""
            
            if final_inst:
                # Check for password notification
                status = final_inst.get('status', {})
                if status.get('passwordPendingNotification'):
                    import base64
                    try:
                        secret = k8s.get_secret(admin_secret_name)
                        if secret and secret.data and 'adminPassword' in secret.data:
                            pw = base64.b64decode(secret.data['adminPassword']).decode('utf-8')
                            
                            dm_embed = discord.Embed(
                                title=f'🔑 Admin Password for {name}',
                                color=discord.Color.green()
                            )
                            if unique_password:
                                dm_embed.description = f"Here is the **unique** admin password for instance **{name}**:"
                            else:
                                dm_embed.description = (
                                    f"Here is your **default** admin password.\n"
                                    "This password will be used for all future instances unless you choose otherwise."
                                )
                                
                            dm_embed.add_field(name="Password", value=f"```{pw}```", inline=False)
                            dm_embed.add_field(name="Instance URL", value=f"https://{name}.{lic.get('spec', {}).get('gateway', {}).get('baseDomain', 'k8s.orb.local')}", inline=False)
                            
                            try:
                                await interaction.user.send(embed=dm_embed)
                                password_status_msg = "\n🔑 **Password sent via DM!**"
                            except discord.Forbidden:
                                password_status_msg = "\n⚠️ **Could not DM password. Please enable DMs.**"
                    except Exception as e:
                        print(f"Error fetching/sending password: {e}")
                        password_status_msg = "\n⚠️ **Error retrieving password.**"

                embed = format_instance_embed(final_inst, is_active_override=False)
                embed.title = f'✅ Instance Created: {name}'
                # Get baseDomain from license
                base_domain = lic.get('spec', {}).get('gateway', {}).get('baseDomain', 'k8s.orb.local')
                embed.description = (
                    'Your instance is ready! Use `/vtt-update` with `activate` to make it live.\n'
                    f'🔗 Once active, access at: https://{name}.{base_domain}'
                    f'{password_status_msg}'
                )
                await msg.edit(embed=embed)
            else:
                # Timeout but created
                timeout_embed = discord.Embed(
                    title='⏳ Instance Created (Provisioning)',
                    description=(
                        f'Instance **{name}** was created, but is still provisioning. '
                        'Check `/vtt-status` in a moment.'
                    ),
                    color=discord.Color.orange()
                )
                await msg.edit(embed=timeout_embed)
        else:
            embed = discord.Embed(
                title='❌ Creation Failed',
                description=result['message'],
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
    
    @vtt_create.autocomplete('foundry_version')
    async def version_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """Autocomplete callback for foundry versions."""
        try:
            versions, stable = await get_foundry_versions()
            if not versions:
                return []
            
            choices = []
            for ver in versions:
                # Filter by current input
                if current.lower() in ver.lower():
                    name = ver
                    if ver == stable:
                        name = f"{ver} (Stable)"
                    choices.append(app_commands.Choice(name=name, value=ver))
            
            return choices[:25]
        except Exception as e:
            print(f'Version autocomplete error: {e}')
            return []

    @vtt_create.autocomplete('license_name')
    async def license_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """Autocomplete callback for license names."""
        try:
            licenses = k8s.get_foundry_licenses()
            if not licenses:
                return []
            
            choices = []
            for lic in licenses:
                name = lic['metadata']['name']
                # Filter by current input (case-insensitive)
                if current.lower() in name.lower():
                    choices.append(app_commands.Choice(name=name, value=name))
            
            return choices[:25]  # Discord limit
        except Exception as e:
            print(f'License autocomplete error: {e}')
            return []


async def setup(bot: commands.Bot):
    """Load the CreateCog."""
    await bot.add_cog(CreateCog(bot))
