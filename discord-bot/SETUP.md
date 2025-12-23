# Discord Bot Setup Guide

This guide will help you set up and run the Kratix Foundry Discord Bot.

## Prerequisites

1.  **Kubernetes Cluster**: You need access to a cluster where the Kratix Foundry Promises are installed.
2.  **Discord Bot Token**: Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications).
    - Enable **Server Members Intent** and **Message Content Intent** under the "Bot" tab.
    - Copy the `Token`.
3.  **Python 3.10+**: Ensure you have Python installed.

## Installation

1.  **Clone the Repository**:
    ```bash
    git clone https://github.com/your-repo/kratix-foundry.git
    cd kratix-foundry/discord-bot
    ```

2.  **Create a Virtual Environment**:
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```

3.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure Environment**:
    Copy the example environment file and fill in your details:
    ```bash
    cp .env.example .env
    ```
    
    Update `.env` with:
    - `DISCORD_BOT_TOKEN`: Your bot token.
    - `DISCORD_GUILD_ID`: Your Discord server (guild) ID.
    - `FOUNDRY_NAMESPACE`: The namespace where Foundry resources live (default: `foundry-vtt`).

## Running the Bot

```bash
python bot.py
```

On first run, the bot will sync commands to your guild. Use `/vtt-help` in Discord to see the available commands.

## Bot Commands

- `/vtt-help`: Show this setup guide and command help.
- `/vtt-status`: View Foundry instance status (active/standby, players).
- `/vtt-create`: Create a new Foundry instance resource.
- `/vtt-update`: Activate or deactivate an instance.
- `/vtt-delete`: Delete an instance you created.
