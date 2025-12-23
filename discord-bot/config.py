"""
Configuration constants and environment variables for the Kratix Foundry Discord Bot.
"""

import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Bot configuration
DISCORD_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
GUILD_ID = os.getenv('DISCORD_GUILD_ID')
FOUNDRY_NAMESPACE = os.getenv('FOUNDRY_NAMESPACE', 'foundry-vtt')

# Kubernetes CRD configuration
CRD_GROUP = 'foundry.platform'
CRD_VERSION = 'v1alpha1'
CRD_INSTANCE_PLURAL = 'foundryinstances'
CRD_LICENSE_PLURAL = 'foundrylicenses'

# Annotation keys
ANNOTATION_SCHEDULED_DELETE = 'foundry.platform/scheduled-delete-at'
ANNOTATION_CREATED_BY_ID = 'foundry.platform/created-by-id'
ANNOTATION_CREATED_BY_NAME = 'foundry.platform/created-by-name'

# Cache settings
CACHE_TTL_SECONDS = 5  # How long to cache autocomplete results
CRD_CACHE_TTL_SECONDS = 300  # 5 minutes for CRD schema cache
