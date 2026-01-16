"""
/vtt-password command cog - Admin password management via External Secrets Operator.
"""

from typing import List
import discord
from discord import app_commands
from discord.ext import commands

import k8s_client as k8s
from config import FOUNDRY_NAMESPACE
from embeds import format_instance_embed

class PasswordCog(commands.Cog):
    """Cog containing the /vtt-password commands."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
    password_group = app_commands.Group(name="vtt-password", description="Manage admin passwords")

    @password_group.command(name="reset-default", description="Reset your default admin password for future instances")
    async def reset_default(self, interaction: discord.Interaction):
        """Resets the user's default admin password by refreshing related ExternalSecrets."""
        await interaction.response.defer(ephemeral=True)
        
        if not k8s.is_connected():
            await interaction.followup.send("❌ Kubernetes not connected", ephemeral=True)
            return

        # Find the default FoundryPassword for this user
        password_name = f"foundry-password-user-{interaction.user.id}"
        
        # Refresh the ExternalSecret (managed by the FoundryPassword Promise)
        result = k8s.refresh_external_secret(password_name, FOUNDRY_NAMESPACE)
        
        if result['success']:
            await interaction.followup.send(
                f"✅ Default password reset triggered. You will receive a DM with the new password shortly.", 
                ephemeral=True
            )
        else:
            await interaction.followup.send(f"❌ {result['message']}", ephemeral=True)

    @password_group.command(name="reset-instance", description="Reset the password for a specific instance")
    @app_commands.describe(name="Name of the instance to reset")
    async def reset_instance(self, interaction: discord.Interaction, name: str):
        """Resets the password for a specific instance by refreshing its ExternalSecret."""
        await interaction.response.defer(ephemeral=True)
        
        if not k8s.is_connected():
            await interaction.followup.send("❌ Kubernetes not connected", ephemeral=True)
            return
            
        instance = k8s.get_foundry_instance(name)
        if not instance:
            await interaction.followup.send(f"❌ Instance **{name}** not found.", ephemeral=True)
            return
        
        namespace = instance['metadata']['namespace']
        
        # Determine the password resource name
        # If it was created as a unique password, it's foundry-password-{name}
        # If it's using default, the user should use reset-default instead, 
        # but let's try to find it.
        
        secret_ref = instance.get('spec', {}).get('adminPasswordSecretRef', {})
        secret_name = secret_ref.get('name')
        
        if not secret_name:
            # Fallback/Legacy
            secret_name = f"foundry-credentials-{name}"
        
        # Trigger refresh via ExternalSecret annotation
        result = k8s.refresh_external_secret(secret_name, namespace)
        
        if result['success']:
            await interaction.followup.send(
                f"✅ Password reset triggered for **{name}**. You will receive a DM with the new password shortly.", 
                ephemeral=True
            )
        else:
            await interaction.followup.send(f"❌ {result['message']}", ephemeral=True)

    @reset_instance.autocomplete('name')
    async def instance_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        try:
            instances = k8s.get_foundry_instances()
            choices = []
            for inst in instances:
                name = inst['metadata']['name']
                if current.lower() in name.lower():
                    choices.append(app_commands.Choice(name=name, value=name))
            return choices[:25]
        except:
            return []

async def setup(bot: commands.Bot):
    await bot.add_cog(PasswordCog(bot))
