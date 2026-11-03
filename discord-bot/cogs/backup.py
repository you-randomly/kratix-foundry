"""
/vtt-backup command cog - Request an archive backup of a Foundry instance via Discord DM.
"""

import asyncio
import io
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from config import (
    BACKUP_INTERNAL_URL_TEMPLATE,
    BACKUP_SERVICE_PORT,
    DISCORD_DM_MAX_ATTACHMENT_BYTES,
    FOUNDRY_NAMESPACE,
    GUILD_ID,
)
import k8s_client as k8s


SCOPE_LABELS: Dict[str, str] = {
    "default": "Worlds + User Assets (excl. systems & modules)",
    "worlds": "Worlds Only (Data/worlds)",
    "full": "Full Data Directory (incl. systems & modules)",
}


def format_bytes(num_bytes: Optional[int]) -> str:
    """Format a byte count as a human-readable string."""
    if num_bytes is None or num_bytes < 0:
        return "Unknown"
    if num_bytes < 1024:
        return f"{num_bytes} B"
    units = ("KB", "MB", "GB", "TB")
    val = float(num_bytes)
    for unit in units:
        val /= 1024.0
        if val < 1024.0 or unit == units[-1]:
            return f"{val:.1f} {unit}" if val >= 10 else f"{val:.2f} {unit}"
    return f"{num_bytes} B"


async def is_user_server_admin(
    interaction: discord.Interaction,
    bot: Optional[commands.Bot] = None,
) -> bool:
    """Return True if the interaction caller has Discord Administrator permissions."""
    perms = getattr(interaction, "permissions", None)
    if perms is not None and getattr(perms, "administrator", False):
        return True

    user_guild_perms = getattr(getattr(interaction, "user", None), "guild_permissions", None)
    if user_guild_perms is not None and getattr(user_guild_perms, "administrator", False):
        return True

    # If invoked in a DM, check configured guild or shared guilds
    if getattr(interaction, "guild", None) is None and bot is not None:
        candidate_guilds = []
        if GUILD_ID:
            try:
                g = bot.get_guild(int(GUILD_ID))
                if g is not None:
                    candidate_guilds.append(g)
            except (TypeError, ValueError):
                pass
        for g in getattr(bot, "guilds", None) or []:
            if g not in candidate_guilds:
                candidate_guilds.append(g)

        user_id = getattr(getattr(interaction, "user", None), "id", None)
        if user_id is not None:
            for g in candidate_guilds:
                member = g.get_member(user_id)
                if member is None and hasattr(g, "fetch_member"):
                    try:
                        member = await g.fetch_member(user_id)
                    except Exception:
                        member = None
                if member is not None and getattr(getattr(member, "guild_permissions", None), "administrator", False):
                    return True

    return False


def check_backup_authorization(
    instance: Dict[str, Any],
    user_id: str,
    is_admin: bool,
) -> Tuple[bool, Optional[str]]:
    """Verify whether `user_id` is authorized to request a backup of `instance`."""
    if is_admin:
        return True, None

    owner_id = k8s.get_instance_owner_id(instance)
    if not owner_id:
        return (
            False,
            "This instance has no recorded Discord owner and can only be backed up by a server administrator.",
        )

    if str(owner_id) != str(user_id):
        return (
            False,
            "You can only request backups for instances you created (or as a server administrator).",
        )

    return True, None


def describe_active_session_notice(instance: Dict[str, Any]) -> Optional[str]:
    """Build an informational notice if a world or user session was active during the snapshot."""
    status = instance.get("status", {}) or {}
    annotations = (instance.get("metadata", {}) or {}).get("annotations", {}) or {}

    players = status.get("connectedPlayers", 0)
    active_world = status.get("activeWorld")
    has_world_name = isinstance(active_world, str) and bool(active_world.strip())
    world_active = bool(status.get("worldActive") or has_world_name)
    activity = (annotations.get("foundry.platform/activity") or "idle").strip()

    details: List[str] = []
    if has_world_name:
        details.append(f"world **{active_world}** was open")
    elif world_active:
        details.append("a world was open")

    if isinstance(players, int) and players > 0:
        details.append(f"**{players}** player(s) connected")

    if activity and activity != "idle" and not details:
        details.append(f"instance activity detected (`{activity}`)")

    if not details:
        return None

    joined = " with ".join(details[:2]) if len(details) == 2 else ", ".join(details)
    return (
        f"⚠️ **Live Snapshot Notice:** Taken while {joined}. "
        "Foundry VTT uses LevelDB for world data — if active writes were in flight, "
        "returning to Setup or standby before backing up guarantees a fully quiescent snapshot."
    )


def build_backup_dm_embed(
    *,
    instance_name: str,
    scope: str,
    backup_info: Dict[str, Any],
    delivery: str,
    download_url: Optional[str] = None,
    active_notice: Optional[str] = None,
) -> discord.Embed:
    """Construct the Discord DM Embed accompanying either a file attachment or a signed link."""
    filename = backup_info.get("filename", f"{instance_name}-backup.zip")
    file_count = backup_info.get("fileCount", 0)
    uncompressed_bytes = backup_info.get("uncompressedBytes", 0)
    compressed_bytes = backup_info.get("compressedBytes")
    worlds = backup_info.get("worlds") or []
    systems_count = backup_info.get("systemsCount", 0)
    modules_count = backup_info.get("modulesCount", 0)
    expires_at = backup_info.get("expiresAt")

    if delivery == "inline":
        desc_parts = [f"Your backup archive **`{filename}`** is attached to this message."]
    else:
        desc_parts = [
            f"Your backup archive **`{filename}`** exceeds Discord's DM attachment limit and is ready for download:"
        ]
        if download_url:
            desc_parts.append(f"\n### [⬇️ Download Backup Archive]({download_url})")
        if expires_at:
            desc_parts.append(f"⏳ Link expires <t:{int(expires_at)}:R> (<t:{int(expires_at)}:t>).")

    if active_notice:
        desc_parts.append(f"\n{active_notice}")

    embed = discord.Embed(
        title=f"📦 Foundry Backup: {instance_name}",
        description="\n".join(desc_parts),
        color=discord.Color.green() if not active_notice else discord.Color.gold(),
    )

    embed.add_field(
        name="Scope",
        value=SCOPE_LABELS.get(scope, scope),
        inline=False,
    )

    size_summary = f"{file_count:,} file(s) • {format_bytes(uncompressed_bytes)} uncompressed"
    if compressed_bytes is not None:
        size_summary += f" ({format_bytes(compressed_bytes)} zip)"
    embed.add_field(name="Archive Size", value=size_summary, inline=True)

    if worlds:
        shown_worlds = ", ".join(worlds[:5])
        if len(worlds) > 5:
            shown_worlds += f" (+{len(worlds) - 5} more)"
        embed.add_field(name=f"Worlds ({len(worlds)})", value=shown_worlds, inline=True)
    else:
        embed.add_field(name="Worlds", value="None found", inline=True)

    embed.add_field(
        name="Package Manifest",
        value=f"`backup-manifest.json` ({systems_count} system(s), {modules_count} module(s))",
        inline=False,
    )

    embed.set_footer(text="Keep this backup safe • Excludes license keys & container cache")
    return embed


class BackupCog(commands.Cog):
    """Cog containing the /vtt-backup command."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _sidecar_base_url(self, instance_name: str, namespace: str) -> str:
        return BACKUP_INTERNAL_URL_TEMPLATE.format(
            instance=instance_name,
            namespace=namespace,
            port=BACKUP_SERVICE_PORT,
        ).rstrip("/")

    async def _prepare_and_fetch_backup(
        self,
        instance: Dict[str, Any],
        instance_name: str,
        namespace: str,
        scope: str,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[bytes], Optional[str]]:
        """
        Ask the instance's monitor sidecar to prepare a backup and, if inline, fetch the zip bytes.

        Returns `(backup_info, inline_zip_bytes_or_none, error_message_or_none)`.
        """
        secret_name = k8s.password_secret_name(instance)
        primary_pw = await asyncio.to_thread(k8s.get_admin_password_plaintext, secret_name, namespace)

        candidate_keys: List[str] = []
        if primary_pw:
            candidate_keys.append(primary_pw)

        base_url = self._sidecar_base_url(instance_name, namespace)
        timeout = aiohttp.ClientTimeout(total=300, connect=10)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Lazy fallback to running pod env password if secret missing or rotated moments ago
            async def _get_all_keys() -> List[str]:
                if not candidate_keys:
                    running_pw = await asyncio.to_thread(k8s.get_running_admin_password, instance_name, namespace)
                    if running_pw:
                        candidate_keys.append(running_pw)
                return candidate_keys

            keys = await _get_all_keys()
            if not keys:
                return (
                    None,
                    None,
                    f"Could not retrieve credentials for **{instance_name}**. The instance may still be initializing.",
                )

            backup_info: Optional[Dict[str, Any]] = None
            last_error: Optional[str] = None

            for attempt_idx in range(2):
                auth_key = keys[-1]
                try:
                    async with session.post(
                        f"{base_url}/__platform/backups",
                        headers={"Authorization": f"Bearer {auth_key}"},
                        json={
                            "scope": scope,
                            "maxInlineBytes": DISCORD_DM_MAX_ATTACHMENT_BYTES,
                        },
                    ) as resp:
                        if resp.status == 401 and attempt_idx == 0:
                            running_pw = await asyncio.to_thread(
                                k8s.get_running_admin_password, instance_name, namespace
                            )
                            if running_pw and running_pw not in keys:
                                keys.append(running_pw)
                                continue
                            return None, None, "Sidecar rejected backup authentication."

                        if resp.status != 200:
                            try:
                                err_payload = await resp.json()
                                err_detail = err_payload.get("error") or f"HTTP {resp.status}"
                            except Exception:
                                err_detail = f"HTTP {resp.status}"
                            return None, None, f"Backup service returned an error: {err_detail}"

                        backup_info = await resp.json()
                        break
                except aiohttp.ClientConnectorError:
                    return (
                        None,
                        None,
                        f"Could not reach the backup service for **{instance_name}**. Ensure the instance pod is running (`/vtt-status`).",
                    )
                except asyncio.TimeoutError:
                    return None, None, f"Timed out while preparing backup for **{instance_name}**."
                except Exception as exc:
                    last_error = str(exc)
                    break

            if not backup_info:
                return None, None, f"Failed to prepare backup: {last_error or 'unknown error'}"

            inline_bytes: Optional[bytes] = None
            if backup_info.get("delivery") == "inline":
                token = quote(str(backup_info["token"]), safe=".")
                filename = quote(str(backup_info["filename"]), safe="._-")
                dl_url = f"{base_url}/__platform/backups/download/{token}/{filename}"
                try:
                    async with session.get(dl_url) as dl_resp:
                        if dl_resp.status == 200:
                            inline_bytes = await dl_resp.read()
                        else:
                            # Fall back to external download link if inline pickup failed
                            backup_info["delivery"] = "link"
                except Exception:
                    backup_info["delivery"] = "link"

            return backup_info, inline_bytes, None

    @app_commands.command(
        name="vtt-backup",
        description="Request a backup of a Foundry instance sent to your Discord DMs",
    )
    @app_commands.describe(
        instance="Name of the Foundry instance to back up",
        scope="Which files from /data/Data to include in the backup archive",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="Worlds + user assets (Recommended)", value="default"),
            app_commands.Choice(name="Worlds only (Data/worlds)", value="worlds"),
            app_commands.Choice(name="Full Data directory (includes systems & modules)", value="full"),
        ]
    )
    async def vtt_backup(
        self,
        interaction: discord.Interaction,
        instance: str,
        scope: Optional[app_commands.Choice[str]] = None,
    ):
        """Create a backup of a FoundryInstance and send it to the caller via Discord DM."""
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not k8s.is_connected():
            await interaction.followup.send("❌ Kubernetes not connected.", ephemeral=True)
            return

        inst = await asyncio.to_thread(k8s.get_foundry_instance, instance)
        if not inst:
            await interaction.followup.send(f"❌ Instance **{instance}** not found.", ephemeral=True)
            return

        is_admin = await is_user_server_admin(interaction, self.bot)
        allowed, deny_reason = check_backup_authorization(inst, str(interaction.user.id), is_admin)
        if not allowed:
            await interaction.followup.send(f"🚫 **Permission Denied:** {deny_reason}", ephemeral=True)
            return

        selected_scope = scope.value if scope is not None else "default"
        namespace = (inst.get("metadata", {}) or {}).get("namespace", FOUNDRY_NAMESPACE)

        progress_msg = await interaction.followup.send(
            f"📦 Preparing **{SCOPE_LABELS.get(selected_scope, selected_scope)}** backup for **{instance}**...",
            ephemeral=True,
            wait=True,
        )

        backup_info, inline_bytes, err_msg = await self._prepare_and_fetch_backup(
            inst,
            instance,
            namespace,
            selected_scope,
        )
        if err_msg or not backup_info:
            await progress_msg.edit(content=f"❌ {err_msg or 'Failed to create backup.'}")
            return

        # Resolve external baseDomain from parent FoundryLicense for download links
        license_name = (inst.get("spec", {}) or {}).get("licenseRef", {}).get("name")
        base_domain = "lab.jake-watson.co.uk"
        if license_name:
            lic = await asyncio.to_thread(k8s.get_foundry_license, license_name, namespace)
            if lic:
                base_domain = (
                    (lic.get("spec", {}) or {}).get("gateway", {}).get("baseDomain") or base_domain
                )

        token_enc = quote(str(backup_info["token"]), safe=".")
        filename_raw = str(backup_info["filename"])
        filename_enc = quote(filename_raw, safe="._-")
        external_download_url = (
            f"https://{instance}.{base_domain}/__platform/backups/download/{token_enc}/{filename_enc}"
        )

        active_notice = describe_active_session_notice(inst)
        delivery = str(backup_info.get("delivery") or "link")

        try:
            if delivery == "inline" and inline_bytes is not None:
                embed = build_backup_dm_embed(
                    instance_name=instance,
                    scope=selected_scope,
                    backup_info=backup_info,
                    delivery="inline",
                    active_notice=active_notice,
                )
                try:
                    discord_file = discord.File(io.BytesIO(inline_bytes), filename=filename_raw)
                    await interaction.user.send(embed=embed, file=discord_file)
                    await progress_msg.edit(
                        content=f"📬 Backup for **{instance}** (`{filename_raw}`) has been attached in your DMs!"
                    )
                    return
                except discord.HTTPException as http_exc:
                    if getattr(http_exc, "status", None) != 413:
                        raise
                    # Payload exceeded Discord's runtime attachment cap -> fall back seamlessly to signed link
                    delivery = "link"

            link_embed = build_backup_dm_embed(
                instance_name=instance,
                scope=selected_scope,
                backup_info=backup_info,
                delivery="link",
                download_url=external_download_url,
                active_notice=active_notice,
            )
            await interaction.user.send(embed=link_embed)
            await progress_msg.edit(
                content=(
                    f"📬 Backup download link for **{instance}** has been sent to your DMs! "
                    f"(Archive exceeded Discord's attachment size limit.)"
                )
            )
        except discord.Forbidden:
            await progress_msg.edit(
                content=(
                    "❌ Unable to send you a direct message. "
                    "Please enable DMs from server members in your Discord Privacy Settings and try again."
                )
            )
        except Exception as send_exc:
            await progress_msg.edit(content=f"❌ Failed to deliver backup via Discord DM: {send_exc}")

    @vtt_backup.autocomplete("instance")
    async def instance_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """Autocomplete instance names (owned instances for regular users, all instances for admins)."""
        try:
            instances = k8s.get_foundry_instances()
            if not instances:
                return []

            is_admin = bool(getattr(getattr(interaction, "permissions", None), "administrator", False))
            caller_id = str(interaction.user.id)
            choices: List[app_commands.Choice[str]] = []

            for inst in instances:
                name = inst["metadata"]["name"]
                if not is_admin:
                    owner_id = k8s.get_instance_owner_id(inst)
                    if not owner_id or str(owner_id) != caller_id:
                        continue
                if current.lower() in name.lower():
                    choices.append(app_commands.Choice(name=name, value=name))

            return choices[:25]
        except Exception as exc:
            print(f"Backup autocomplete error: {exc}")
            return []


async def setup(bot: commands.Bot):
    """Load the BackupCog."""
    await bot.add_cog(BackupCog(bot))
