"""
/vtt-update command cog - Activate/deactivate Foundry instances.
"""

from typing import List

import discord
from discord import app_commands
from discord.ext import commands

from config import ANNOTATION_SCHEDULED_DELETE
from embeds import format_instance_embed
from tasks import wait_for_resource_condition
import k8s_client as k8s


class UpdateCog(commands.Cog):
    """Cog containing the /vtt-update command."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    @app_commands.command(name='vtt-update', description='Update a Foundry instance')
    @app_commands.describe(
        instance='Name of the instance to update',
        action='Action to perform (activate/deactivate)'
    )
    @app_commands.choices(action=[
        app_commands.Choice(name='activate', value='activate'),
        app_commands.Choice(name='deactivate', value='deactivate'),
    ])
    async def vtt_update(
        self,
        interaction: discord.Interaction, 
        instance: str,
        action: app_commands.Choice[str]
    ):
        """Update a FoundryInstance (e.g., activate it)."""
        await interaction.response.defer(thinking=True)
        
        if not k8s.is_connected():
            embed = discord.Embed(
                title='❌ Kubernetes Not Connected',
                description='The bot is not connected to a Kubernetes cluster.',
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return
        
        if action.value == 'activate':
            await self._handle_activate(interaction, instance)
        elif action.value == 'deactivate':
            await self._handle_deactivate(interaction, instance)
        else:
            embed = discord.Embed(
                title='❓ Unknown Action',
                description=f'Unknown action: {action.value}',
                color=discord.Color.orange()
            )
            await interaction.followup.send(embed=embed)
    
    async def _handle_activate(self, interaction: discord.Interaction, instance: str):
        """Handle the activate action."""
        # Check if instance is scheduled for deletion
        inst = k8s.get_foundry_instance(instance)
        if inst:
            annotations = inst.get('metadata', {}).get('annotations', {})
            scheduled_at = annotations.get(ANNOTATION_SCHEDULED_DELETE)
            if scheduled_at:
                embed = discord.Embed(
                    title='🚫 Activation Blocked',
                    description=f'Instance **{instance}** is scheduled for deletion on {scheduled_at} and cannot be activated.',
                    color=discord.Color.red()
                )
                embed.add_field(name='How to fix', value='Cancel the deletion first using `/vtt-delete`', inline=False)
                await interaction.followup.send(embed=embed)
                return

        result = k8s.activate_instance(instance)
        
        if result['success']:
            license_name = result.get('license_name')
            if not license_name:
                await interaction.followup.send(content=f"✅ {result['message']}")
                return
            
            # Send initial progress message
            switch_embed = discord.Embed(
                title='🔄 Switching Active Instance...',
                description=f"Switching license **{license_name}** to **{instance}**. This usually takes 10-20 seconds.",
                color=discord.Color.blue()
            )
            switch_embed.add_field(name='Target', value=instance, inline=True)
            switch_embed.set_footer(text='The Kratix pipeline is reconciling routes...')
            
            msg = await interaction.followup.send(embed=switch_embed)
            
            # Poll for the license status to update
            def check_active(lic):
                return lic.get('status', {}).get('activeInstance') == instance

            final_lic = await wait_for_resource_condition(
                get_fn=lambda: k8s.get_foundry_license(license_name),
                check_fn=check_active,
                timeout_seconds=60,
                interval_seconds=5
            )
            
            if final_lic:
                # Success! Now get the actual instance to show in the embed
                inst_data = k8s.get_foundry_instance(instance)
                if inst_data:
                    embed = format_instance_embed(inst_data, is_active_override=True)
                    embed.title = f'✅ Instance Live: {instance}'
                    url = f"https://{instance}.k8s.orb.local"
                    embed.description = f"Switch complete! The instance is now accessible.\n🔗 [**Access Instance**]({url})"
                    await msg.edit(embed=embed)
                else:
                    await msg.edit(content=f"✅ Switch complete! Instance **{instance}** is now active.")
            else:
                # Timeout
                timeout_embed = discord.Embed(
                    title='⚠️ Switch Taking Longer Than Expected',
                    description=(
                        f"The request to activate **{instance}** was sent, but the routing hasn't updated yet. "
                        "It should be ready shortly. Check `/vtt-status` in a moment."
                    ),
                    color=discord.Color.orange()
                )
                await msg.edit(embed=timeout_embed)
        else:
            embed = discord.Embed(
                title='❌ Activation Failed',
                description=result['message'],
                color=discord.Color.red()
            )
            if 'license_name' in result:
                embed.add_field(name='License', value=result['license_name'], inline=True)
            await interaction.followup.send(embed=embed)
    
    async def _handle_deactivate(self, interaction: discord.Interaction, instance: str):
        """Handle the deactivate action."""
        result = k8s.deactivate_instance(instance)
        
        if result['success']:
            license_name = result.get('license_name')
            if not license_name:
                await interaction.followup.send(content=f"✅ {result['message']}")
                return

            # Send initial progress message
            deactivate_embed = discord.Embed(
                title='🟠 Deactivating Instance...',
                description=f"Deactivating **{instance}** from license **{license_name}**. This usually takes 10-20 seconds.",
                color=discord.Color.orange()
            )
            deactivate_embed.set_footer(text='The Kratix pipeline is reconciling routes...')
            
            msg = await interaction.followup.send(embed=deactivate_embed)
            
            # Poll for the license status to update
            def check_inactive(lic):
                return lic.get('status', {}).get('activeInstance') != instance

            final_lic = await wait_for_resource_condition(
                get_fn=lambda: k8s.get_foundry_license(license_name),
                check_fn=check_inactive,
                timeout_seconds=60,
                interval_seconds=5
            )
            
            if final_lic:
                success_embed = discord.Embed(
                    title='🟠 Instance Standby',
                    description=f"Instance **{instance}** is now on standby. Users will see the status page.",
                    color=discord.Color.orange()
                )
                await msg.edit(embed=success_embed)
            else:
                # Timeout
                timeout_embed = discord.Embed(
                    title='⚠️ Deactivation Taking Longer Than Expected',
                    description=(
                        f"The request to deactivate **{instance}** was sent, but the routing hasn't updated yet. "
                        "Check `/vtt-status` in a moment."
                    ),
                    color=discord.Color.orange()
                )
                await msg.edit(embed=timeout_embed)
        else:
            embed = discord.Embed(
                title='❌ Deactivation Failed',
                description=result['message'],
                color=discord.Color.red()
            )
            if 'license_name' in result:
                embed.add_field(name='License', value=result['license_name'], inline=True)
            await interaction.followup.send(embed=embed)
    
    @vtt_update.autocomplete('instance')
    async def instance_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """Autocomplete for instance names - shows all instances with status."""
        try:
            instances = k8s.get_foundry_instances()
            if not instances:
                return [app_commands.Choice(name='No instances available', value='__none__')]
            
            choices = []
            for inst in instances:
                name = inst['metadata']['name']
                # Filter by current input
                if current.lower() in name.lower():
                    # Add status indicator
                    license_name = inst.get('spec', {}).get('licenseRef', {}).get('name')
                    license_obj = k8s.get_foundry_license(license_name) if license_name else None
                    is_active = False
                    if license_obj:
                        is_active = license_obj.get('spec', {}).get('activeInstanceName') == name
                    
                    status_emoji = '🟢' if is_active else '🟠'
                    choices.append(app_commands.Choice(name=f'{status_emoji} {name}', value=name))
            
            return choices[:25]
        except Exception as e:
            print(f'Update autocomplete error: {e}')
            return []


async def setup(bot: commands.Bot):
    """Load the UpdateCog."""
    await bot.add_cog(UpdateCog(bot))
