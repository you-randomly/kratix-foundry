"""
/vtt-password command cog - Admin password management via External Secrets Operator and kro.
"""

from typing import List, Optional
import discord
from discord import app_commands
from discord.ext import commands

import k8s_client as k8s
from config import FOUNDRY_NAMESPACE

class PasswordCog(commands.Cog):
    """Cog containing the /vtt-password commands."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
    password_group = app_commands.Group(name="vtt-password", description="Manage admin passwords")

    @password_group.command(name="reset-default", description="Reset your default admin password for instances")
    async def reset_default(self, interaction: discord.Interaction):
        """Resets the user's default admin password by requesting rotation on their FoundryPassword."""
        await interaction.response.defer(ephemeral=True)
        
        if not k8s.is_connected():
            await interaction.followup.send("❌ Kubernetes not connected", ephemeral=True)
            return

        password_name = f"foundry-password-user-{interaction.user.id}"
        result = k8s.request_password_rotation(password_name, FOUNDRY_NAMESPACE)
        
        if result['success']:
            await interaction.followup.send(
                "✅ Default password rotation requested. You will receive a DM with the new password once generated, "
                "and instances using it will restart to apply it once no active players or downloads are running.", 
                ephemeral=True
            )
        else:
            await interaction.followup.send(f"❌ {result['message']}", ephemeral=True)

    @password_group.command(name="reset-instance", description="Reset the password for a specific instance")
    @app_commands.describe(name="Name of the instance to reset")
    async def reset_instance(self, interaction: discord.Interaction, name: str):
        """Resets the password for a specific instance with owner verification."""
        await interaction.response.defer(ephemeral=True)
        
        if not k8s.is_connected():
            await interaction.followup.send("❌ Kubernetes not connected", ephemeral=True)
            return
            
        instance = k8s.get_foundry_instance(name)
        if not instance:
            await interaction.followup.send(f"❌ Instance **{name}** not found.", ephemeral=True)
            return

        # Ownership authorization check
        owner_id = k8s.get_instance_owner_id(instance)
        if owner_id and owner_id != str(interaction.user.id):
            await interaction.followup.send("❌ You do not have permission to reset the password for this instance.", ephemeral=True)
            return
        
        namespace = instance.get('metadata', {}).get('namespace', FOUNDRY_NAMESPACE)
        password_res_name = k8s.password_secret_name(instance)
        
        result = k8s.request_password_rotation(password_res_name, namespace)
        
        if result['success']:
            note = ""
            if password_res_name.startswith("foundry-password-user-"):
                note = "\n*(Note: This instance uses your default password, so other instances using it will also update.)*"

            activity = instance.get('metadata', {}).get('annotations', {}).get('foundry.platform/activity', 'idle')
            await interaction.followup.send(
                f"✅ Password rotation requested for **{name}**.{note}\n"
                f"You will receive a DM with the new password shortly. It will apply automatically once the instance "
                f"is safe to restart (current status: `{activity}`).", 
                ephemeral=True
            )
        else:
            await interaction.followup.send(f"❌ {result['message']}", ephemeral=True)

    @password_group.command(name="show", description="Show the current admin password via direct message")
    @app_commands.describe(name="Optional name of the instance (leave blank for default password)")
    async def show_password(self, interaction: discord.Interaction, name: Optional[str] = None):
        """Retrieve and DM the admin password to the verified owner."""
        await interaction.response.defer(ephemeral=True)

        if not k8s.is_connected():
            await interaction.followup.send("❌ Kubernetes not connected", ephemeral=True)
            return

        target_label = "Default Password"
        instance = None
        if name:
            instance = k8s.get_foundry_instance(name)
            if not instance:
                await interaction.followup.send(f"❌ Instance **{name}** not found.", ephemeral=True)
                return
            owner_id = k8s.get_instance_owner_id(instance)
            if owner_id and owner_id != str(interaction.user.id):
                await interaction.followup.send("❌ You do not have permission to view the password for this instance.", ephemeral=True)
                return
            secret_name = k8s.password_secret_name(instance)
            target_label = f"Instance '{name}'"
        else:
            secret_name = f"foundry-password-user-{interaction.user.id}"

        password = k8s.get_admin_password_plaintext(secret_name, FOUNDRY_NAMESPACE)
        pending_note = ""
        if instance and password:
            namespace = instance.get('metadata', {}).get('namespace', FOUNDRY_NAMESPACE)
            running = k8s.get_running_admin_password(name, namespace)
            if running and running != password:
                pending_note = (
                    "\n⚠️ A password reset is pending. The instance is still using the password below; "
                    "the new password will apply once it restarts (use `/vtt-password show` again afterwards)."
                )
                password = running
        if not password:
            await interaction.followup.send(
                f"❌ Could not retrieve password for `{secret_name}`. It may still be generating.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🔑 Foundry VTT Admin Password",
            description=f"Admin password for **{target_label}**:{pending_note}",
            color=discord.Color.blue()
        )
        embed.add_field(name="Password", value=f"||`{password}`||", inline=False)
        embed.set_footer(text="Keep this secret. Click to unmask.")

        try:
            await interaction.user.send(embed=embed)
            await interaction.followup.send("📬 Admin password has been sent to your DMs.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Unable to send you a direct message. Please enable DMs from server members and try again.",
                ephemeral=True
            )

    @reset_instance.autocomplete('name')
    @show_password.autocomplete('name')
    async def instance_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        try:
            instances = k8s.get_foundry_instances()
            choices = []
            user_id = str(interaction.user.id)
            for inst in instances:
                name = inst['metadata']['name']
                # Only show instances owned by user if owner info present
                owner = k8s.get_instance_owner_id(inst)
                if owner and owner != user_id:
                    continue
                if current.lower() in name.lower():
                    choices.append(app_commands.Choice(name=name, value=name))
            return choices[:25]
        except Exception:
            return []

async def setup(bot: commands.Bot):
    await bot.add_cog(PasswordCog(bot))
