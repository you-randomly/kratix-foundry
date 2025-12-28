"""
/vtt-status command cog - Show instance status.
"""

from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import FOUNDRY_NAMESPACE
from embeds import format_instance_embed
import k8s_client as k8s


class StatusCog(commands.Cog):
    """Cog containing the /vtt-status command."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    @app_commands.command(name='vtt-status', description='Show Foundry instance status')
    @app_commands.describe(instance='Name of the instance to check (optional)')
    async def vtt_status(self, interaction: discord.Interaction, instance: Optional[str] = None):
        """Show instance status (active/standby, players, version).
        
        If no instance is specified, shows status of all instances.
        """
        await interaction.response.defer(thinking=True)
        
        if not k8s.is_connected():
            embed = discord.Embed(
                title='❌ Kubernetes Not Connected',
                description='The bot is not connected to a Kubernetes cluster.',
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return
        
        if instance:
            # Get specific instance
            inst = k8s.get_foundry_instance(instance)
            if inst:
                # Look up the license to determine true active status
                license_name = inst.get('spec', {}).get('licenseRef', {}).get('name')
                is_active_from_license = None
                if license_name:
                    license_obj = k8s.get_foundry_license(license_name)
                    if license_obj:
                        active_instance = (
                            license_obj.get('status', {}).get('activeInstance') or 
                            license_obj.get('spec', {}).get('activeInstanceName')
                        )
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
            # List all instances - show summary view
            instances = k8s.get_foundry_instances()
            
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
                    license_obj = k8s.get_foundry_license(license_name)
                    if license_obj:
                        active_name = (
                            license_obj.get('status', {}).get('activeInstance') or
                            license_obj.get('spec', {}).get('activeInstanceName')
                        )
                        license_active_map[license_name] = active_name
            
            # Build summary table
            summary_lines = []
            for inst in instances:
                inst_name = inst['metadata']['name']
                spec = inst.get('spec', {})
                status = inst.get('status', {})
                license_name = spec.get('licenseRef', {}).get('name')
                is_active = license_active_map.get(license_name) == inst_name
                
                # Status emoji
                status_emoji = '🟢' if is_active else '🟠'
                
                # Player count
                players = status.get('connectedPlayers', 0)
                if players < 0:
                    player_text = '❓'
                else:
                    player_text = f'{players}👥'
                
                # World
                world_name = status.get('activeWorld')
                if world_name and world_name is not True:
                    world_text = f'🌍 {world_name}'
                else:
                    world_text = '⚫ -'
                
                # Version
                version = spec.get('foundryVersion', '-')
                
                summary_lines.append(
                    f"{status_emoji} **{inst_name}** • {player_text} • {world_text} • v{version}"
                )
            
            embed = discord.Embed(
                title=f'🎲 Foundry Instances ({len(instances)})',
                description='\n'.join(summary_lines),
                color=discord.Color.blue()
            )
            embed.set_footer(text='Use /vtt-status <instance> for details • Powered by Kratix Foundry')
            await interaction.followup.send(embed=embed)
    
    @vtt_status.autocomplete('instance')
    async def instance_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """Autocomplete callback for instance names."""
        try:
            instances = k8s.get_foundry_instances()
            if not instances:
                return []
            
            choices = []
            for inst in instances:
                name = inst['metadata']['name']
                # Filter by current input (case-insensitive)
                if current.lower() in name.lower():
                    # Add status indicator
                    license_name = inst.get('spec', {}).get('licenseRef', {}).get('name')
                    license_obj = k8s.get_foundry_license(license_name) if license_name else None
                    is_active = False
                    if license_obj:
                        active_name = (
                            license_obj.get('status', {}).get('activeInstance') or
                            license_obj.get('spec', {}).get('activeInstanceName')
                        )
                        is_active = active_name == name
                    
                    status_emoji = '🟢' if is_active else '🟠'
                    choices.append(app_commands.Choice(name=f'{status_emoji} {name}', value=name))
            
            return choices[:25]  # Discord limit
        except Exception as e:
            print(f'Status autocomplete error: {e}')
            return []


async def setup(bot: commands.Bot):
    """Load the StatusCog."""
    await bot.add_cog(StatusCog(bot))
