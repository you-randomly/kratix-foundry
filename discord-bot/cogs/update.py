"""
/vtt-update command cog - Activate/deactivate Foundry instances.
"""

import asyncio
from typing import List

import discord
from discord import app_commands
from discord.ext import commands

from config import ANNOTATION_SCHEDULED_DELETE
from embeds import format_instance_embed
import k8s_client as k8s
from switch_tracker import switch_tracker


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
        inst = await asyncio.to_thread(k8s.get_foundry_instance, instance)
        if inst:
            annotations = inst.get('metadata', {}).get('annotations', {}) or {}
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

        result = await asyncio.to_thread(k8s.activate_instance, instance)
        
        if result['success']:
            license_name = result.get('license_name')
            if not license_name:
                await interaction.followup.send(content=f"✅ {result['message']}")
                return

            inst_ns = (inst or {}).get('metadata', {}).get('namespace') or k8s.FOUNDRY_NAMESPACE

            # Fast path if already active
            if result.get('already_active'):
                lic = await asyncio.to_thread(k8s.get_foundry_license, license_name, inst_ns, False) or {}
                base_domain = lic.get('spec', {}).get('gateway', {}).get('baseDomain', 'k8s.orb.local')
                url = f"https://{instance}.{base_domain}"
                if inst:
                    embed = format_instance_embed(inst, is_active_override=True)
                    embed.title = f'✅ Instance Already Live: {instance}'
                else:
                    embed = discord.Embed(title=f'✅ Instance Already Live: {instance}', color=discord.Color.green())
                embed.description = f"Instance **{instance}** is already active.\n🔗 [**Access Instance**]({url})"
                await interaction.followup.send(embed=embed)
                return

            # Send initial progress message and register for continuous tracking
            switch_embed = discord.Embed(
                title='🔄 Switching Active Instance...',
                description=f"Switching license **{license_name}** to **{instance}**. Checking active session state...",
                color=discord.Color.blue()
            )
            switch_embed.add_field(name='Target', value=instance, inline=True)
            switch_embed.set_footer(text='Reconciling license lease & routes...')
            
            msg = await interaction.followup.send(embed=switch_embed)

            await switch_tracker.register_pending(
                bot=self.bot,
                license_name=license_name,
                namespace=inst_ns,
                action='activate',
                instance_name=instance,
                user_id=str(interaction.user.id),
                channel_id=interaction.channel_id,
                message_id=msg.id,
                webhook_msg=msg,
                interaction=interaction,
            )

            # Poll briskly for fast uncontested switches or immediate contention status;
            # reconcile_license_messages continues tracking in the background until live.
            for _ in range(8):
                state = await switch_tracker.reconcile_license(self.bot, license_name, inst_ns)
                if state in ('completed', 'superseded', 'queued'):
                    return
                await asyncio.sleep(3)

            # One final check at 24s transitions 'switching' -> 'completing' (queued) so background loop pings on completion
            await switch_tracker.reconcile_license(self.bot, license_name, inst_ns)
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
        inst = await asyncio.to_thread(k8s.get_foundry_instance, instance)
        inst_ns = (inst or {}).get('metadata', {}).get('namespace') or k8s.FOUNDRY_NAMESPACE
        result = await asyncio.to_thread(k8s.deactivate_instance, instance, inst_ns)
        
        if result['success']:
            license_name = result.get('license_name')
            if not license_name:
                await interaction.followup.send(content=f"✅ {result['message']}")
                return

            if result.get('already_inactive'):
                standby_embed = discord.Embed(
                    title='🟠 Instance Already on Standby',
                    description=f"Instance **{instance}** is already on standby.",
                    color=discord.Color.orange()
                )
                await interaction.followup.send(embed=standby_embed)
                return

            deactivate_embed = discord.Embed(
                title='🟠 Deactivating Instance...',
                description=f"Deactivating **{instance}** from license **{license_name}**. Checking active session state...",
                color=discord.Color.orange()
            )
            deactivate_embed.set_footer(text='Reconciling license lease & routes...')
            
            msg = await interaction.followup.send(embed=deactivate_embed)

            await switch_tracker.register_pending(
                bot=self.bot,
                license_name=license_name,
                namespace=inst_ns,
                action='deactivate',
                instance_name=instance,
                user_id=str(interaction.user.id),
                channel_id=interaction.channel_id,
                message_id=msg.id,
                webhook_msg=msg,
                interaction=interaction,
            )

            for _ in range(8):
                state = await switch_tracker.reconcile_license(self.bot, license_name, inst_ns)
                if state in ('completed', 'superseded', 'queued'):
                    return
                await asyncio.sleep(3)

            await switch_tracker.reconcile_license(self.bot, license_name, inst_ns)
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
        """Autocomplete for instance names - shows all instances with live/queued/standby status."""
        try:
            instances = k8s.get_foundry_instances()
            if not instances:
                return [app_commands.Choice(name='No instances available', value='__none__')]
            
            choices = []
            for inst in instances:
                name = inst['metadata']['name']
                if current.lower() in name.lower():
                    license_name = inst.get('spec', {}).get('licenseRef', {}).get('name')
                    license_obj = k8s.get_foundry_license(license_name) if license_name else None
                    status_emoji = '🟠'
                    if license_obj:
                        active_name = (license_obj.get('status', {}).get('activeInstance') or '').strip()
                        req_name = (license_obj.get('spec', {}).get('activeInstanceName') or '').strip()
                        if active_name == name:
                            status_emoji = '🟢'
                        elif req_name == name:
                            status_emoji = '⏳'
                    
                    choices.append(app_commands.Choice(name=f'{status_emoji} {name}', value=name))
            
            return choices[:25]
        except Exception as e:
            print(f'Update autocomplete error: {e}')
            return []


async def setup(bot: commands.Bot):
    """Load the UpdateCog."""
    await bot.add_cog(UpdateCog(bot))
