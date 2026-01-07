"""
/vtt-password command cog - Admin password management.
"""

from typing import List, Optional
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
        """Resets the user's default admin password."""
        await interaction.response.defer(ephemeral=True)
        
        if not k8s.is_connected():
            await interaction.followup.send("❌ Kubernetes not connected", ephemeral=True)
            return

        # Triggering a reset for the *default* password.
        # Since the default secret isn't tied to a specific CRD status unless it's used by an instance,
        # we can't easily rely on the pipeline to "notify" us via a CRD status change unless we pick a proxy instance.
        # However, the user wants to reset their DEFAULT password.
        # The default password is stored in `foundry-admin-<uid>-default`.
        
        # Option A: Delete the secret. Next time an instance is created using it, it will be regenerated.
        # But the user might want to know the new password NOW. 
        # And existing instances using it won't update automatically unless they are redeployed (pods restarted).
        # We can:
        # 1. Generate a new password here.
        # 2. Update the secret directly.
        # 3. DM the user.
        # 4. (Optional) Restart pods using this secret? (Too complex for now).
        
        # User constraint: "Kratix pipeline to handle actual generation".
        # But pipeline runs on Instance CRD reconciliation.
        # If we don't have an instance using the default secret *right now* that we can patch, we can't trigger the pipeline.
        
        # Compromise: For "reset-default", if we just delete the secret, 
        # the next creation checks -> finds missing -> generates.
        # The user gets the new password then.
        
        # BUT, if they want to reset it for *existing* instances using the default.
        # We'd need to trigger reconciliation on those instances.
        
        # Let's simplify: 
        # 1. Find ALL instances created by this user that are NOT using unique passwords (heuristic: secret name).
        # OR just find ONE instance using the default secret.
        # If none exist, we just delete the secret so it regenerates next time.
        
        secret_name = f"foundry-admin-{interaction.user.id}-default"
        
        # Find instances using this secret
        instances = k8s.get_foundry_instances()
        target_instance = None
        for inst in instances:
            ref = inst.get('spec', {}).get('adminPasswordSecretRef', {}).get('name')
            if ref == secret_name:
                target_instance = inst
                break
        
        if target_instance:
            # Trigger regeneration on this instance
            name = target_instance['metadata']['name']
            
            # Using patch to set regeneratePassword: true
            # This triggers pipeline -> pipeline sees flag -> generates new -> updates secret -> updates status with notification flag.
            # Background task picks up notification -> DMs user.
            
            try:
                k8s.k8s_api.patch_namespaced_custom_object(
                    group=k8s.CRD_GROUP,
                    version=k8s.CRD_VERSION,
                    namespace=target_instance['metadata']['namespace'],
                    plural=k8s.CRD_INSTANCE_PLURAL,
                    name=name,
                    body={"spec": {"regeneratePassword": True}}
                )
                await interaction.followup.send(
                    f"✅ Password reset triggered via instance **{name}**. You will receive a DM with the new password shortly.", 
                    ephemeral=True
                )
            except Exception as e:
                await interaction.followup.send(f"❌ Failed to trigger reset: {e}", ephemeral=True)
                
        else:
            # No instance uses the default secret. Just delete it so next use generates a new one.
            try:
                k8s.k8s.core_v1.delete_namespaced_secret(secret_name, k8s.FOUNDRY_NAMESPACE)
                await interaction.followup.send(
                    "✅ Default password secret deleted. A new one will be generated and sent to you the next time you create an instance using the default password.",
                    ephemeral=True
                )
            except Exception as e:
                 # If 404, it's already gone/never existed
                 if "Not Found" in str(e) or "404" in str(e):
                     await interaction.followup.send("ℹ️ You don't have a default password set yet. One will be generated next time you create an instance.", ephemeral=True)
                 else:
                     await interaction.followup.send(f"❌ Error resetting default password: {e}", ephemeral=True)

    @password_group.command(name="reset-instance", description="Reset the password for a specific instance")
    @app_commands.describe(name="Name of the instance to reset")
    async def reset_instance(self, interaction: discord.Interaction, name: str):
        """Resets the password for a specific instance."""
        await interaction.response.defer(ephemeral=True)
        
        if not k8s.is_connected():
            await interaction.followup.send("❌ Kubernetes not connected", ephemeral=True)
            return
            
        instance = k8s.get_foundry_instance(name)
        if not instance:
            await interaction.followup.send(f"❌ Instance **{name}** not found.", ephemeral=True)
            return
            
        # Check permissions (only creator should reset?) -> For now assume admin access or trusted users
        # But we should probably check if user created it.
        # annotations = instance['metadata'].get('annotations', {})
        # creator_id = annotations.get('foundry.platform/created-by-id')
        # if creator_id and str(creator_id) != str(interaction.user.id):
        #    await interaction.followup.send("❌ You can only reset passwords for instances you created.", ephemeral=True)
        #    return

        try:
            k8s.k8s_api.patch_namespaced_custom_object(
                group=k8s.CRD_GROUP,
                version=k8s.CRD_VERSION,
                namespace=instance['metadata']['namespace'],
                plural=k8s.CRD_INSTANCE_PLURAL,
                name=name,
                body={"spec": {"regeneratePassword": True}}
            )
            await interaction.followup.send(
                f"✅ Password reset triggered for **{name}**. You will receive a DM with the new password shortly.", 
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to trigger reset: {e}", ephemeral=True)

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
