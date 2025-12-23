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
        memory='Memory request/limit (e.g., "1Gi")'
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
        memory: Optional[str] = None
    ):
        """Create a new FoundryInstance resource."""
        await interaction.response.defer(thinking=True)
        
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
        
        # Create the instance
        result = k8s.create_foundry_instance(
            name=name,
            license_name=license_name,
            foundry_version=foundry_version,
            storage_backend=storage_backend.value if storage_backend else None,
            cpu=cpu,
            memory=memory,
            created_by_id=str(interaction.user.id),
            created_by_name=str(interaction.user)
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
            
            if final_inst:
                embed = format_instance_embed(final_inst, is_active_override=False)
                embed.title = f'✅ Instance Created: {name}'
                embed.description = (
                    'Your instance is ready! Use `/vtt-update` with `activate` to make it live.\n'
                    f'🔗 Once active, access at: https://{name}.k8s.orb.local'
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
