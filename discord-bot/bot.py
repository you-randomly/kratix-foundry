#!/usr/bin/env python3
"""
Kratix Foundry Discord Bot

A Discord bot for managing Foundry VTT instances via Kratix.
This is the main entry point that loads all cog modules.
"""

import sys

import discord
from discord.ext import commands

from config import DISCORD_TOKEN, GUILD_ID
from cogs import COGS
import k8s_client as k8s
from tasks import cleanup_expired_deletions
from utils import versions


# Set up intents (only default - no privileged intents needed for slash commands)
intents = discord.Intents.default()

# Create bot instance
bot = commands.Bot(command_prefix='!', intents=intents)


@bot.event
async def on_ready():
    """Called when the bot is ready."""
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')
    
    # Initialize Kubernetes
    k8s.init_kubernetes()
    
    # Load all cogs
    for cog in COGS:
        try:
            await bot.load_extension(cog)
            print(f'Loaded cog: {cog}')
        except Exception as e:
            print(f'Failed to load cog {cog}: {e}')
    
    # Sync commands to guild for faster updates during development
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f'Commands synced to guild {GUILD_ID} ({len(synced)} commands)')
    else:
        synced = await bot.tree.sync()
        print(f'Commands synced globally ({len(synced)} commands)')
    
    # Start background tasks
    if not cleanup_expired_deletions.is_running():
        cleanup_expired_deletions.start()
        print('Started cleanup_expired_deletions background task')
        
    from tasks import check_password_notifications
    if not check_password_notifications.is_running():
        check_password_notifications.start(bot)
        print('Started check_password_notifications background task')

    # Pre-warm caches
    bot.loop.create_task(versions.refresh_cache())
    bot.loop.run_in_executor(None, k8s.get_foundry_licenses)


def main():
    """Run the bot."""
    if not DISCORD_TOKEN:
        print('ERROR: DISCORD_BOT_TOKEN environment variable not set')
        print('Copy .env.example to .env and add your bot token')
        sys.exit(1)
    
    bot.run(DISCORD_TOKEN)


if __name__ == '__main__':
    main()
