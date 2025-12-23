"""
/vtt-delete command cog - Delete Foundry instances with grace period.
"""

from datetime import datetime, timedelta, timezone
from typing import List

import discord
from discord import app_commands
from discord.ext import commands

from config import ANNOTATION_SCHEDULED_DELETE
from views import DeleteManagementView
import k8s_client as k8s


class DeleteCog(commands.Cog):
    """Cog containing the /vtt-delete command."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    @app_commands.command(name='vtt-delete', description='Delete a Foundry instance')
    @app_commands.describe(instance='Name of the instance to delete')
    async def vtt_delete(self, interaction: discord.Interaction, instance: str):
        """Delete a FoundryInstance. Only the owner can delete their instance."""
        await interaction.response.defer(thinking=True)
        
        if not k8s.is_connected():
            embed = discord.Embed(
                title='❌ Kubernetes Not Connected',
                description='The bot is not connected to a Kubernetes cluster.',
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return
        
        # Get the instance to check ownership
        inst = k8s.get_foundry_instance(instance)
        if not inst:
            embed = discord.Embed(
                title='❌ Instance Not Found',
                description=f'No instance named **{instance}** found.',
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return
        
        # Check ownership - must be the creator to delete
        annotations = inst.get('metadata', {}).get('annotations', {})
        owner_id = annotations.get('foundry.platform/created-by-id')
        owner_name = annotations.get('foundry.platform/created-by-name', 'Unknown')
        caller_id = str(interaction.user.id)
        
        # Block deletion of unowned (legacy) instances via Discord
        if not owner_id:
            embed = discord.Embed(
                title='🚫 Cannot Delete Legacy Instance',
                description=f'Instance **{instance}** was not created via Discord and cannot be deleted here.',
                color=discord.Color.red()
            )
            embed.add_field(name='Alternative', value='Use `kubectl delete foundryinstance` to remove this instance.', inline=False)
            await interaction.followup.send(embed=embed)
            return
        
        if owner_id != caller_id:
            embed = discord.Embed(
                title='🚫 Permission Denied',
                description=f'You can only delete instances you created.',
                color=discord.Color.red()
            )
            embed.add_field(name='Instance Owner', value=owner_name, inline=True)
            embed.add_field(name='Your User', value=str(interaction.user), inline=True)
            await interaction.followup.send(embed=embed)
            return
        
        # Check if already marked for deletion
        scheduled_at = annotations.get(ANNOTATION_SCHEDULED_DELETE)
        
        if scheduled_at:
            # Prompt to cancel or extend
            embed = discord.Embed(
                title='⚠️ Instance Scheduled for Deletion',
                description=(
                    f'Instance **{instance}** is already scheduled for deletion.\n\n'
                    f'**Scheduled Date:** UTC {scheduled_at}\n\n'
                    'Would you like to cancel the deletion or extend the grace period?'
                ),
                color=discord.Color.yellow()
            )
            view = DeleteManagementView(instance, caller_id)
            await interaction.followup.send(embed=embed, view=view)
            return
        
        # Mark for deletion
        try:
            # Safety Check: Is it active?
            license_name = inst.get('spec', {}).get('licenseRef', {}).get('name')
            if license_name:
                lic = k8s.get_foundry_license(license_name, use_cache=False)
                if lic and lic.get('spec', {}).get('activeInstanceName') == instance:
                    # Force deactivate first
                    deactivate_result = k8s.deactivate_instance(instance)
                    if not deactivate_result['success']:
                        embed = discord.Embed(
                            title='❌ Deletion Failed',
                            description=f'Failed to deactivate active instance before deletion: {deactivate_result["message"]}',
                            color=discord.Color.red()
                        )
                        await interaction.followup.send(embed=embed)
                        return
                    
                    # Notify user about deactivation
                    embed = discord.Embed(
                        title='🟠 Instance Deactivated',
                        description=f'Instance **{instance}** was active and has been deactivated to prepare for deletion.',
                        color=discord.Color.orange()
                    )
                    await interaction.followup.send(embed=embed)

            # Calculate deletion date (7 days from now)
            deletion_date = datetime.now(timezone.utc) + timedelta(days=7)
            deletion_date_str = deletion_date.isoformat()
            
            result = k8s.patch_instance_annotations(
                instance,
                {ANNOTATION_SCHEDULED_DELETE: deletion_date_str},
                inst['metadata']['namespace']
            )
            
            if result['success']:
                embed = discord.Embed(
                    title='🗓️ Deletion Scheduled',
                    description=(
                        f'Instance **{instance}** has been marked for deletion.\n\n'
                        f'It will be automatically removed in **7 days** (UTC {deletion_date_str}) '
                        'unless cancelled or extended.'
                    ),
                    color=discord.Color.orange()
                )
                embed.set_footer(text='Run this command again to manage the deletion')
                await interaction.followup.send(embed=embed)
            else:
                embed = discord.Embed(
                    title='❌ Scheduling Failed',
                    description=f'Failed to schedule deletion: {result["message"]}',
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
        except Exception as e:
            embed = discord.Embed(
                title='❌ Scheduling Failed',
                description=f'Failed to schedule deletion: {e}',
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
    
    @vtt_delete.autocomplete('instance')
    async def instance_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """Autocomplete for instance names - only show instances owned by the user."""
        try:
            instances = k8s.get_foundry_instances()
            if not instances:
                return [app_commands.Choice(name='No instances exist', value='__none__')]
            
            caller_id = str(interaction.user.id)
            choices = []
            
            for inst in instances:
                name = inst['metadata']['name']
                annotations = inst.get('metadata', {}).get('annotations', {})
                owner_id = annotations.get('foundry.platform/created-by-id')
                
                # Only show instances owned by THIS user (not legacy/unowned)
                if not owner_id or owner_id != caller_id:
                    continue
                
                # Filter by current input
                if current.lower() in name.lower():
                    choices.append(app_commands.Choice(name=name, value=name))
            
            # Show helpful message if user has no instances
            if not choices:
                return [app_commands.Choice(name='You have no instances to delete', value='__none__')]
            
            return choices[:25]
        except Exception as e:
            print(f'Delete autocomplete error: {e}')
            return []


async def setup(bot: commands.Bot):
    """Load the DeleteCog."""
    await bot.add_cog(DeleteCog(bot))
