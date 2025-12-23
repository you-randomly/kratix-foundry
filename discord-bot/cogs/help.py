"""
/vtt-help command cog - Display usage guidance.
"""

import discord
from discord import app_commands
from discord.ext import commands


class HelpCog(commands.Cog):
    """Cog containing the /vtt-help command."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    @app_commands.command(name='vtt-help', description='How to use the self-service Foundry platform')
    async def vtt_help(self, interaction: discord.Interaction):
        """Display guidance on using the Foundry VTT self-service platform."""
        embed = discord.Embed(
            title='🎲 Foundry VTT Self-Service Guide',
            description=(
                'Welcome to the self-service Foundry platform! This bot allows you to create, '
                'manage, and switch between multiple Foundry instances.'
            ),
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name='🟢 Active vs 🟠 Standby',
            value=(
                'Due to license restrictions, only **one instance per license** can be active at a time.\n'
                '• **Active**: Fully accessible via its URL.\n'
                '• **Standby**: Shows a status page with the current active instance and player count.'
            ),
            inline=False
        )
        
        embed.add_field(
            name='🎮 Essential Commands',
            value=(
                '• `/vtt-status`: View all instances and see who is currently live.\n'
                '• `/vtt-create`: Create a new instance (world) with custom specs.\n'
                '• `/vtt-update`: Switch the license to a different instance.\n'
                '• `/vtt-delete`: Remove an instance (only for the creator).'
            ),
            inline=False
        )
        
        embed.add_field(
            name='🔄 Switching Instances',
            value=(
                'Use `/vtt-update` with the `activate` action to go live. '
                'If players are currently connected to the active instance, '
                'the switch may be blocked to prevent disruption.'
            ),
            inline=False
        )
        
        embed.add_field(
            name='🛠️ Ownership',
            value=(
                'Instances you create are tagged with your Discord ID. '
                'Only you (or a cluster admin) can delete your instances.'
            ),
            inline=False
        )
        
        embed.set_footer(text='Powered by Kratix Foundry Platform')
        
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    """Load the HelpCog."""
    await bot.add_cog(HelpCog(bot))
