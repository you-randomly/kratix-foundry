"""
/vtt-password command cog - Admin password management via External Secrets Operator.
"""

from typing import List
import discord
from discord import app_commands
from discord.ext import commands

import k8s_client as k8s
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

        # Find instances created by this user and refresh their ExternalSecrets
        instances = k8s.get_foundry_instances()
        refreshed_count = 0
        errors = []
        
        for inst in instances:
            annotations = inst['metadata'].get('annotations', {})
            creator_id = annotations.get('foundry.platform/created-by-id')
            
            if creator_id == str(interaction.user.id):
                instance_name = inst['metadata']['name']
                namespace = inst['metadata']['namespace']
                
                # Get the secret name for this instance
                secret_ref = inst.get('spec', {}).get('adminPasswordSecretRef', {})
                secret_name = secret_ref.get('name')
                if not secret_name:
                    secret_name = f"foundry-credentials-{instance_name}"
                
                # Refresh the ExternalSecret (which has the same name as the secret)
                result = k8s.refresh_external_secret(secret_name, namespace)
                if result['success']:
                    refreshed_count += 1
                else:
                    errors.append(f"{instance_name}: {result['message']}")
        
        if refreshed_count > 0:
            msg = f"✅ Password reset triggered for {refreshed_count} instance(s). You will receive DM(s) with the new password(s) shortly."
            if errors:
                msg += f"\n\n⚠️ Some ExternalSecrets could not be refreshed:\n" + "\n".join(errors)
            await interaction.followup.send(msg, ephemeral=True)
        elif errors:
            await interaction.followup.send(
                f"❌ Failed to refresh ExternalSecrets:\n" + "\n".join(errors),
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "ℹ️ You don't have any instances with passwords to reset. Create an instance first.",
                ephemeral=True
            )

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
        
        # Get the secret/ExternalSecret name for this instance
        secret_ref = instance.get('spec', {}).get('adminPasswordSecretRef', {})
        secret_name = secret_ref.get('name')
        if not secret_name:
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
