import datetime
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientSession, web

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
lib_path = os.path.join(project_root, "lib")
bot_path = os.path.join(project_root, "discord-bot")
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)
if bot_path not in sys.path:
    sys.path.insert(0, bot_path)

# Mock kubernetes & dotenv if not installed in unit test environment
k8s_mock = MagicMock()
sys.modules.setdefault("kubernetes", k8s_mock)
sys.modules.setdefault("kubernetes.client", k8s_mock.client)
sys.modules.setdefault("kubernetes.client.rest", k8s_mock.client.rest)
sys.modules.setdefault("kubernetes.config", k8s_mock.config)
sys.modules.setdefault("kubernetes.stream", k8s_mock.stream)
sys.modules.setdefault("dotenv", MagicMock())


class _FakeEmbed:
    def __init__(self, title=None, description=None, color=None):
        self.title = title
        self.description = description
        self.color = color
        self.fields = []
        self.footer = None

    def add_field(self, name, value, inline=False):
        self.fields.append({"name": name, "value": value, "inline": inline})

    def set_footer(self, text):
        self.footer = text


class _FakeFile:
    def __init__(self, fp, filename=None):
        self.fp = fp
        self.filename = filename
        self.data = fp.read() if hasattr(fp, "read") else b""


class _FakeChoice:
    def __class_getitem__(cls, _item):
        return cls

    def __init__(self, name, value):
        self.name = name
        self.value = value


class _FakeHTTPException(Exception):
    def __init__(self, message="", status=400):
        super().__init__(message)
        self.status = status


def _passthrough_command(*_args, **_kwargs):
    def decorator(fn):
        fn.autocomplete = lambda _param: (lambda ac_fn: ac_fn)
        return fn
    return decorator


class _FakeGroup:
    def __init__(self, *_args, **_kwargs):
        pass

    def command(self, *_args, **_kwargs):
        return _passthrough_command(*_args, **_kwargs)


if "discord" not in sys.modules:
    discord_mock = MagicMock()
    discord_mock.Embed = _FakeEmbed
    discord_mock.File = _FakeFile
    discord_mock.Forbidden = type("Forbidden", (Exception,), {})
    discord_mock.NotFound = type("NotFound", (Exception,), {})
    discord_mock.HTTPException = _FakeHTTPException
    discord_mock.app_commands.Group = _FakeGroup
    discord_mock.ext.commands.Cog = object
    sys.modules["discord"] = discord_mock
    sys.modules["discord.ext"] = discord_mock.ext
    sys.modules["discord.ext.commands"] = discord_mock.ext.commands
    sys.modules["discord.app_commands"] = discord_mock.app_commands

import discord

# Ensure required attributes exist even if another test module initialized discord_mock first
discord.Embed = _FakeEmbed
discord.File = _FakeFile
if not isinstance(getattr(discord, "Forbidden", None), type) or not issubclass(discord.Forbidden, BaseException):
    discord.Forbidden = type("Forbidden", (Exception,), {})
if not isinstance(getattr(discord, "HTTPException", None), type) or not issubclass(discord.HTTPException, BaseException):
    discord.HTTPException = _FakeHTTPException
discord.app_commands.command = _passthrough_command
discord.app_commands.describe = lambda **_kw: (lambda fn: fn)
discord.app_commands.choices = lambda **_kw: (lambda fn: fn)
discord.app_commands.Choice = _FakeChoice

import k8s_client as k8s
from cogs.backup import BackupCog, check_backup_authorization, describe_active_session_notice
from foundry_lib.backup import (
    BackupManager,
    collect_backup_plan,
    sign_backup_token,
    verify_backup_token,
    write_backup_zip,
)
from foundry_lib.presence import PresenceCoordinator
from foundry_lib.proxy import create_proxy_app


def _populate_sample_foundry_data(root_dir: str) -> None:
    """Create a realistic Foundry `/data` layout including sensitive files and NAS junk."""
    # Sensitive Config & cache directories that must NEVER be archived
    os.makedirs(os.path.join(root_dir, "Config"), exist_ok=True)
    with open(os.path.join(root_dir, "Config", "license.json"), "w", encoding="utf-8") as f:
        json.dump({"license": "SUPER-SECRET-FOUNDRY-LICENSE"}, f)
    with open(os.path.join(root_dir, "Config", "admin.txt"), "w", encoding="utf-8") as f:
        f.write("secret-admin-hash")

    os.makedirs(os.path.join(root_dir, "container_cache"), exist_ok=True)
    with open(os.path.join(root_dir, "container_cache", "release.zip"), "wb") as f:
        f.write(b"cached-release-blob" * 100)

    os.makedirs(os.path.join(root_dir, "Logs"), exist_ok=True)
    with open(os.path.join(root_dir, "Logs", "debug.log"), "w", encoding="utf-8") as f:
        f.write("some log line\n")

    # World data (including LevelDB files and an epoch-0 timestamp file)
    world_dir = os.path.join(root_dir, "Data", "worlds", "curse-of-strahd")
    os.makedirs(os.path.join(world_dir, "data"), exist_ok=True)
    with open(os.path.join(world_dir, "world.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "id": "curse-of-strahd",
                "title": "Curse of Strahd",
                "system": "dnd5e",
                "coreVersion": "13.351",
                "systemVersion": "4.1.0",
            },
            f,
        )
    ldb_path = os.path.join(world_dir, "data", "000005.ldb")
    with open(ldb_path, "wb") as f:
        f.write(os.urandom(4096))
    os.utime(ldb_path, (0, 0))  # 1970-01-01 mtime edge case

    # Custom user assets in Data/
    assets_dir = os.path.join(root_dir, "Data", "assets", "maps")
    os.makedirs(assets_dir, exist_ok=True)
    with open(os.path.join(assets_dir, "barovia.webp"), "wb") as f:
        f.write(os.urandom(8192))

    # Synology NAS @eaDir & NFS silly-rename junk that must be excluded
    ea_dir = os.path.join(world_dir, "@eaDir")
    os.makedirs(ea_dir, exist_ok=True)
    with open(os.path.join(ea_dir, "world.json@SynoEAStream"), "wb") as f:
        f.write(b"syno-metadata")
    with open(os.path.join(world_dir, ".nfs00000001234"), "wb") as f:
        f.write(b"nfs-temp")

    # Installed system & module
    sys_dir = os.path.join(root_dir, "Data", "systems", "dnd5e")
    os.makedirs(sys_dir, exist_ok=True)
    with open(os.path.join(sys_dir, "system.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "id": "dnd5e",
                "title": "Dungeons & Dragons Fifth Edition",
                "version": "4.1.0",
                "manifest": "https://example.com/dnd5e/system.json",
            },
            f,
        )
    with open(os.path.join(sys_dir, "dnd5e.mjs"), "wb") as f:
        f.write(os.urandom(16384))

    mod_dir = os.path.join(root_dir, "Data", "modules", "dice-so-nice")
    os.makedirs(mod_dir, exist_ok=True)
    with open(os.path.join(mod_dir, "module.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "id": "dice-so-nice",
                "title": "Dice So Nice!",
                "version": "5.1.2",
                "manifest": "https://example.com/dsn/module.json",
            },
            f,
        )
    with open(os.path.join(mod_dir, "main.js"), "wb") as f:
        f.write(os.urandom(16384))

    # Malicious symlink inside Data/ pointing to Config/license.json
    symlink_path = os.path.join(world_dir, "leaked-license.json")
    os.symlink(os.path.join(root_dir, "Config", "license.json"), symlink_path)


class TestBackupEngineUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_root = self.tmp.name
        _populate_sample_foundry_data(self.data_root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scope_filtering_and_security_exclusions(self):
        # 1. Default scope: worlds + user assets, no systems/modules files, but manifest records them
        plan_default = collect_backup_plan(
            self.data_root,
            instance_name="dstrahd",
            scope="default",
            foundry_version="13.351.0",
        )
        arcnames_default = {e.arcname for e in plan_default.files}
        self.assertIn("Data/worlds/curse-of-strahd/world.json", arcnames_default)
        self.assertIn("Data/worlds/curse-of-strahd/data/000005.ldb", arcnames_default)
        self.assertIn("Data/assets/maps/barovia.webp", arcnames_default)
        self.assertFalse(any(a.startswith("Data/systems/") for a in arcnames_default))
        self.assertFalse(any(a.startswith("Data/modules/") for a in arcnames_default))
        self.assertFalse(any("Config" in a or "license" in a for a in arcnames_default))
        self.assertFalse(any("@eaDir" in a or ".nfs" in a for a in arcnames_default))

        self.assertEqual(len(plan_default.manifest["worlds"]), 1)
        self.assertEqual(plan_default.manifest["worlds"][0]["title"], "Curse of Strahd")
        self.assertEqual(len(plan_default.manifest["systems"]), 1)
        self.assertEqual(plan_default.manifest["systems"][0]["id"], "dnd5e")
        self.assertEqual(len(plan_default.manifest["modules"]), 1)
        self.assertEqual(plan_default.manifest["modules"][0]["id"], "dice-so-nice")

        # 2. Worlds-only scope: excludes Data/assets as well
        plan_worlds = collect_backup_plan(self.data_root, instance_name="dstrahd", scope="worlds")
        arcnames_worlds = {e.arcname for e in plan_worlds.files}
        self.assertIn("Data/worlds/curse-of-strahd/world.json", arcnames_worlds)
        self.assertNotIn("Data/assets/maps/barovia.webp", arcnames_worlds)

        # 3. Full scope: includes Data/systems and Data/modules
        plan_full = collect_backup_plan(self.data_root, instance_name="dstrahd", scope="full")
        arcnames_full = {e.arcname for e in plan_full.files}
        self.assertIn("Data/systems/dnd5e/dnd5e.mjs", arcnames_full)
        self.assertIn("Data/modules/dice-so-nice/main.js", arcnames_full)
        self.assertFalse(any("leaked-license" in a for a in arcnames_full))

    def test_resilient_zip_when_leveldb_file_vanishes_mid_snapshot(self):
        plan = collect_backup_plan(self.data_root, instance_name="dstrahd", scope="default")
        # Simulate LevelDB background compaction deleting 000005.ldb after plan scan
        os.remove(os.path.join(self.data_root, "Data", "worlds", "curse-of-strahd", "data", "000005.ldb"))

        buf = io.BytesIO()
        write_backup_zip(plan, buf)

        with zipfile.ZipFile(io.BytesIO(buf.getvalue()), "r") as zf:
            self.assertIsNone(zf.testzip())
            names = set(zf.namelist())
            self.assertIn("backup-manifest.json", names)
            self.assertIn("Data/worlds/curse-of-strahd/world.json", names)
            self.assertNotIn("Data/worlds/curse-of-strahd/data/000005.ldb", names)

    def test_hmac_token_validation_and_cross_instance_isolation(self):
        tok = sign_backup_token(
            signing_key="shared-default-pw",
            instance_name="dstrahd",
            scope="default",
            filename="dstrahd-backup.zip",
            expires_at=2000,
        )

        # Valid verification (including rotated key fallback)
        payload, reason = verify_backup_token(
            tok,
            candidate_keys=["new-rotated-pw", "shared-default-pw"],
            expected_instance="dstrahd",
            expected_filename="dstrahd-backup.zip",
            now_ts=1500,
        )
        self.assertEqual(reason, "ok")
        self.assertEqual(payload["scope"], "default")

        # Expired token rejected
        payload_exp, reason_exp = verify_backup_token(
            tok,
            candidate_keys=["shared-default-pw"],
            expected_instance="dstrahd",
            expected_filename="dstrahd-backup.zip",
            now_ts=2001,
        )
        self.assertIsNone(payload_exp)
        self.assertEqual(reason_exp, "expired")

        # Cross-instance replay with the same user password rejected
        payload_cross, reason_cross = verify_backup_token(
            tok,
            candidate_keys=["shared-default-pw"],
            expected_instance="pf2e",
            expected_filename="dstrahd-backup.zip",
            now_ts=1500,
        )
        self.assertIsNone(payload_cross)
        self.assertEqual(reason_cross, "invalid")


class TestSidecarAndDiscordBackupIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_root = self.tmp.name
        _populate_sample_foundry_data(self.data_root)

        self.active_keys = ["admin-secret-123"]
        self.backup_manager = BackupManager(
            instance_name="dstrahd",
            data_root=self.data_root,
            key_provider=lambda: list(self.active_keys),
            foundry_version="14.360",
            token_ttl_seconds=3600,
        )
        self.coordinator = PresenceCoordinator(instance_name="dstrahd")
        app = create_proxy_app(
            coordinator=self.coordinator,
            upstream_url="http://127.0.0.1:59999",
            backup_manager=self.backup_manager,
        )
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.http = ClientSession()

        k8s.k8s_api = MagicMock()

    async def asyncTearDown(self):
        await self.http.close()
        await self.runner.cleanup()
        self.tmp.cleanup()

    async def test_sidecar_auth_inline_and_streaming_downloads(self):
        # 1. Missing/wrong bearer token rejected with 401
        async with self.http.post(f"{self.base_url}/__platform/backups", json={"scope": "default"}) as r:
            self.assertEqual(r.status, 401)

        # 2. Small backup fits within maxInlineBytes -> delivery == "inline"
        async with self.http.post(
            f"{self.base_url}/__platform/backups",
            headers={"Authorization": "Bearer admin-secret-123"},
            json={"scope": "default", "maxInlineBytes": 5_000_000},
        ) as r:
            self.assertEqual(r.status, 200)
            info = await r.json()
            self.assertEqual(info["delivery"], "inline")
            token = info["token"]
            filename = info["filename"]

        # Discordbot link preview does NOT consume inline cache or stream bytes
        dl_url = f"{self.base_url}/__platform/backups/download/{token}/{filename}"
        async with self.http.get(dl_url, headers={"User-Agent": "Mozilla/5.0 (compatible; Discordbot/2.0)"}) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(await r.read(), b"")

        # First real GET consumes cached inline zip
        async with self.http.get(dl_url) as r:
            self.assertEqual(r.status, 200)
            first_zip = await r.read()

        # Second real GET streams fresh zip using stateless HMAC token
        async with self.http.get(dl_url) as r:
            self.assertEqual(r.status, 200)
            second_zip = await r.read()

        for raw_zip in (first_zip, second_zip):
            with zipfile.ZipFile(io.BytesIO(raw_zip), "r") as zf:
                self.assertIsNone(zf.testzip())
                manifest = json.loads(zf.read("backup-manifest.json").decode("utf-8"))
                self.assertEqual(manifest["instance"], "dstrahd")
                self.assertIn("Data/worlds/curse-of-strahd/world.json", zf.namelist())

        # 3. Large backup (> maxInlineBytes) returns delivery == "link" and streams cleanly
        async with self.http.post(
            f"{self.base_url}/__platform/backups",
            headers={"Authorization": "Bearer admin-secret-123"},
            json={"scope": "full", "maxInlineBytes": 1024},
        ) as r:
            self.assertEqual(r.status, 200)
            large_info = await r.json()
            self.assertEqual(large_info["delivery"], "link")
            self.assertIsNone(large_info["compressedBytes"])

        large_url = (
            f"{self.base_url}/__platform/backups/download/{large_info['token']}/{large_info['filename']}"
        )
        async with self.http.get(large_url) as r:
            self.assertEqual(r.status, 200)
            streamed = await r.read()
        with zipfile.ZipFile(io.BytesIO(streamed), "r") as zf:
            self.assertIsNone(zf.testzip())
            self.assertIn("Data/systems/dnd5e/dnd5e.mjs", zf.namelist())
            self.assertIn("Data/modules/dice-so-nice/main.js", zf.namelist())

    def _make_interaction(self, user_id: str = "111", is_admin: bool = False):
        progress_msg = SimpleNamespace(edit=AsyncMock())
        user = SimpleNamespace(
            id=int(user_id),
            send=AsyncMock(),
            guild_permissions=SimpleNamespace(administrator=is_admin),
        )
        interaction = SimpleNamespace(
            user=user,
            guild=SimpleNamespace(id=999),
            permissions=SimpleNamespace(administrator=is_admin),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock(return_value=progress_msg)),
        )
        return interaction, progress_msg

    async def test_discord_cog_inline_dm_attachment_and_active_warning(self):
        inst = {
            "metadata": {
                "name": "dstrahd",
                "namespace": "foundry-vtt",
                "annotations": {"foundry.platform/created-by-id": "111"},
            },
            "spec": {
                "licenseRef": {"name": "primary-license"},
                "adminPasswordSecretRef": {"name": "foundry-password-user-111"},
            },
            "status": {
                "connectedPlayers": 2,
                "activeWorld": "Curse of Strahd",
                "worldActive": True,
            },
        }
        lic = {"spec": {"gateway": {"baseDomain": "lab.jake-watson.co.uk"}}}

        bot = SimpleNamespace(guilds=[])
        cog = BackupCog(bot)
        interaction, progress_msg = self._make_interaction(user_id="111", is_admin=False)

        with (
            patch.object(k8s, "get_foundry_instance", return_value=inst),
            patch.object(k8s, "get_foundry_license", return_value=lic),
            patch.object(k8s, "get_admin_password_plaintext", return_value="admin-secret-123"),
            patch("cogs.backup.BACKUP_INTERNAL_URL_TEMPLATE", self.base_url),
            patch("cogs.backup.DISCORD_DM_MAX_ATTACHMENT_BYTES", 5_000_000),
        ):
            await cog.vtt_backup(interaction, instance="dstrahd", scope=None)

        interaction.user.send.assert_awaited_once()
        sent_kwargs = interaction.user.send.await_args.kwargs
        self.assertIn("file", sent_kwargs)
        sent_file = sent_kwargs["file"]
        self.assertTrue(sent_file.filename.startswith("dstrahd-backup-"))
        with zipfile.ZipFile(io.BytesIO(sent_file.data), "r") as zf:
            self.assertIsNone(zf.testzip())

        sent_embed = sent_kwargs["embed"]
        self.assertIn("Live Snapshot Notice", sent_embed.description)
        self.assertIn("Curse of Strahd", sent_embed.description)
        self.assertIn("attached", progress_msg.edit.await_args.kwargs["content"])

    async def test_discord_cog_large_backup_falls_back_to_signed_link_and_admin_override(self):
        # Legacy/other-owner instance accessed by a Discord server administrator
        inst = {
            "metadata": {
                "name": "dstrahd",
                "namespace": "foundry-vtt",
                "annotations": {"foundry.platform/created-by-id": "222"},
            },
            "spec": {
                "licenseRef": {"name": "primary-license"},
            },
            "status": {
                "connectedPlayers": 0,
                "worldActive": False,
            },
        }
        lic = {"spec": {"gateway": {"baseDomain": "lab.jake-watson.co.uk"}}}
        bot = SimpleNamespace(guilds=[])
        cog = BackupCog(bot)

        # Non-owner regular user is rejected
        denied_interaction, _ = self._make_interaction(user_id="999", is_admin=False)
        with patch.object(k8s, "get_foundry_instance", return_value=inst):
            await cog.vtt_backup(denied_interaction, instance="dstrahd", scope=None)
        denied_interaction.user.send.assert_not_awaited()
        self.assertIn("Permission Denied", denied_interaction.followup.send.await_args.args[0])

        # Server administrator succeeds and receives a signed HTTPS link when > maxInlineBytes
        admin_interaction, progress_msg = self._make_interaction(user_id="999", is_admin=True)
        full_choice = _FakeChoice("Full", "full")
        with (
            patch.object(k8s, "get_foundry_instance", return_value=inst),
            patch.object(k8s, "get_foundry_license", return_value=lic),
            # Simulate password rotation race: Secret has new key, pod still uses admin-secret-123
            patch.object(k8s, "get_admin_password_plaintext", return_value="brand-new-unmounted-key"),
            patch.object(k8s, "get_running_admin_password", return_value="admin-secret-123"),
            patch("cogs.backup.BACKUP_INTERNAL_URL_TEMPLATE", self.base_url),
            patch("cogs.backup.DISCORD_DM_MAX_ATTACHMENT_BYTES", 512),
        ):
            await cog.vtt_backup(admin_interaction, instance="dstrahd", scope=full_choice)

        admin_interaction.user.send.assert_awaited_once()
        sent_kwargs = admin_interaction.user.send.await_args.kwargs
        self.assertNotIn("file", sent_kwargs)
        embed = sent_kwargs["embed"]
        self.assertIn("https://dstrahd.lab.jake-watson.co.uk/__platform/backups/download/", embed.description)
        self.assertNotIn("Live Snapshot Notice", embed.description)
        self.assertIn("download link", progress_msg.edit.await_args.kwargs["content"])

    async def test_discord_413_runtime_attachment_fallback_and_dm_admin_check(self):
        inst = {
            "metadata": {
                "name": "dstrahd",
                "namespace": "foundry-vtt",
                # Legacy unowned instance, requested from a DM by a guild admin
            },
            "spec": {"licenseRef": {"name": "primary-license"}},
            "status": {"connectedPlayers": 0, "worldActive": False},
        }
        lic = {"spec": {"gateway": {"baseDomain": "lab.jake-watson.co.uk"}}}

        admin_member = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True))
        guild = SimpleNamespace(get_member=MagicMock(return_value=admin_member))
        bot = SimpleNamespace(guilds=[guild], get_guild=MagicMock(return_value=guild))
        cog = BackupCog(bot)

        progress_msg = SimpleNamespace(edit=AsyncMock())
        # First user.send(file=...) raises 413 Payload Too Large; second user.send(embed=...) succeeds
        send_mock = AsyncMock(side_effect=[_FakeHTTPException("Payload Too Large", status=413), None])
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=777, send=send_mock, guild_permissions=None),
            guild=None,
            permissions=None,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock(return_value=progress_msg)),
        )

        with (
            patch.object(k8s, "get_foundry_instance", return_value=inst),
            patch.object(k8s, "get_foundry_license", return_value=lic),
            patch.object(k8s, "get_admin_password_plaintext", return_value="admin-secret-123"),
            patch("cogs.backup.BACKUP_INTERNAL_URL_TEMPLATE", self.base_url),
            patch("cogs.backup.DISCORD_DM_MAX_ATTACHMENT_BYTES", 5_000_000),
        ):
            await cog.vtt_backup(interaction, instance="dstrahd", scope=None)

        self.assertEqual(send_mock.await_count, 2)
        first_call_kwargs = send_mock.await_args_list[0].kwargs
        second_call_kwargs = send_mock.await_args_list[1].kwargs
        self.assertIn("file", first_call_kwargs)
        self.assertNotIn("file", second_call_kwargs)
        self.assertIn(
            "https://dstrahd.lab.jake-watson.co.uk/__platform/backups/download/",
            second_call_kwargs["embed"].description,
        )


if __name__ == "__main__":
    unittest.main()
