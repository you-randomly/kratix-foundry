"""
Discord UI components (Views, Buttons, Modals) for the Foundry bot.
"""

from datetime import datetime, timedelta, timezone

import discord

from config import ANNOTATION_SCHEDULED_DELETE, CRD_GROUP, CRD_VERSION, CRD_INSTANCE_PLURAL
import k8s_client as k8s


class DeleteManagementView(discord.ui.View):
    """View with buttons to cancel or extend a scheduled deletion."""
    
    def __init__(self, instance_name: str, owner_id: str, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.instance_name = instance_name
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.owner_id:
            await interaction.response.send_message(
                "Only the owner of this instance can manage its deletion.", 
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Cancel Deletion", style=discord.ButtonStyle.success)
    async def cancel_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Removes the scheduled deletion annotation."""
        # Get instance to find namespace
        inst = k8s.get_foundry_instance(self.instance_name)
        if not inst:
            await interaction.response.send_message(
                f"Instance **{self.instance_name}** not found.", 
                ephemeral=True
            )
            return

        namespace = inst['metadata']['namespace']
        
        # Merge patch to remove annotation
        result = k8s.patch_instance_annotations(
            self.instance_name,
            {ANNOTATION_SCHEDULED_DELETE: None},
            namespace
        )
        
        if result['success']:
            embed = discord.Embed(
                title='🛡️ Deletion Cancelled',
                description=f'The scheduled deletion for instance **{self.instance_name}** has been cancelled.',
                color=discord.Color.blue()
            )
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await interaction.response.send_message(
                f"Failed to cancel deletion: {result['message']}", 
                ephemeral=True
            )

    @discord.ui.button(label="Extend (7 Days)", style=discord.ButtonStyle.secondary)
    async def extend_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Updates the scheduled deletion date to 7 days from now."""
        # Get instance to find namespace
        inst = k8s.get_foundry_instance(self.instance_name)
        if not inst:
            await interaction.response.send_message(
                f"Instance **{self.instance_name}** not found.", 
                ephemeral=True
            )
            return

        namespace = inst['metadata']['namespace']
        
        # Calculate new deletion date
        new_date = datetime.now(timezone.utc) + timedelta(days=7)
        new_date_str = new_date.isoformat()
        
        result = k8s.patch_instance_annotations(
            self.instance_name,
            {ANNOTATION_SCHEDULED_DELETE: new_date_str},
            namespace
        )
        
        if result['success']:
            embed = discord.Embed(
                title='⏳ Deletion Extended',
                description=f'The grace period for instance **{self.instance_name}** has been extended by 7 days.',
                color=discord.Color.orange()
            )
            embed.add_field(name='New Deletion Date', value=f"UTC {new_date_str}")
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await interaction.response.send_message(
                f"Failed to extend deletion: {result['message']}", 
                ephemeral=True
            )
