import json
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
bot_path = os.path.join(project_root, "discord-bot")
if bot_path not in sys.path:
    sys.path.insert(0, bot_path)

k8s_mock = MagicMock()
sys.modules.setdefault("kubernetes", k8s_mock)
sys.modules.setdefault("kubernetes.client", k8s_mock.client)
sys.modules.setdefault("kubernetes.client.rest", k8s_mock.client.rest)
sys.modules.setdefault("kubernetes.config", k8s_mock.config)
sys.modules.setdefault("kubernetes.stream", k8s_mock.stream)


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


class _FakeGroup:
    def __init__(self, *args, **kwargs):
        pass

    def command(self, *args, **kwargs):
        def decorator(fn):
            fn.autocomplete = lambda _param: (lambda ac_fn: ac_fn)
            return fn
        return decorator


if "discord" not in sys.modules:
    discord_mock = MagicMock()
    discord_mock.Embed = _FakeEmbed
    discord_mock.Forbidden = type("Forbidden", (Exception,), {})
    discord_mock.NotFound = type("NotFound", (Exception,), {})
    discord_mock.app_commands.Group = _FakeGroup
    discord_mock.app_commands.describe = lambda **kwargs: (lambda fn: fn)
    discord_mock.ext.commands.Cog = object
    sys.modules["discord"] = discord_mock
    sys.modules["discord.ext"] = discord_mock.ext
    sys.modules["discord.ext.commands"] = discord_mock.ext.commands
    sys.modules["discord.app_commands"] = discord_mock.app_commands

sys.modules.setdefault("dotenv", MagicMock())

import discord
import k8s_client as k8s
from switch_tracker import ANNOTATION_DISCORD_TRACKED_MESSAGES, SwitchTracker


def _make_inst(name: str, ns: str = "foundry-vtt"):
    return {
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "foundryVersion": "14.369.1",
            "licenseRef": {"name": "primary-license"},
            "storageBackend": "nfs",
        },
        "status": {
            "connectedPlayers": 0,
            "activeWorld": None,
            "lastSidecarUpdate": "2026-11-03T03:00:00Z",
        },
    }


def _make_lic(
    active: str,
    requested: str,
    blocked_reason: str = "",
    annotations: dict = None,
):
    return {
        "metadata": {
            "name": "primary-license",
            "namespace": "foundry-vtt",
            "annotations": dict(annotations or {}),
        },
        "spec": {
            "activeInstanceName": requested,
            "gateway": {"baseDomain": "lab.jake-watson.co.uk"},
        },
        "status": {
            "activeInstance": active,
            "requestedInstance": requested,
            "blockedReason": blocked_reason,
        },
    }


class TestDiscordSwitchTracking(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved_annotations = {}
        self.partials = {}
        self.channel = SimpleNamespace(
            send=AsyncMock(),
            get_partial_message=self._get_partial,
        )
        self.bot = SimpleNamespace(
            get_channel=MagicMock(return_value=self.channel),
            fetch_channel=AsyncMock(return_value=self.channel),
            fetch_user=AsyncMock(return_value=SimpleNamespace(send=AsyncMock())),
        )

    def _get_partial(self, mid: int):
        mid = int(mid)
        if mid not in self.partials:
            self.partials[mid] = SimpleNamespace(id=mid, edit=AsyncMock())
        return self.partials[mid]

    def _mock_annotate(self, _name, annotations, _ns=None):
        for k, v in annotations.items():
            if v is None:
                self.saved_annotations.pop(k, None)
            else:
                self.saved_annotations[k] = v
        return {"success": True}

    async def test_immediate_switch_completes_and_demotes_previous_active_embed(self):
        tracker = SwitchTracker()
        wh_tag = SimpleNamespace(id=101, edit=AsyncMock())
        inter_tag = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))

        # 1. Activate 'tag' immediately
        lic_tag = _make_lic(active="tag", requested="tag")
        with patch.object(k8s, "get_foundry_license", side_effect=lambda *a, **kw: lic_tag), \
             patch.object(k8s, "get_foundry_instance", side_effect=lambda n, *a, **kw: _make_inst(n)), \
             patch.object(k8s, "annotate_foundry_license", side_effect=self._mock_annotate), \
             patch.object(discord, "Embed", _FakeEmbed):
            await tracker.register_pending(
                bot=self.bot,
                license_name="primary-license",
                action="activate",
                instance_name="tag",
                user_id="111",
                channel_id=500,
                message_id=101,
                webhook_msg=wh_tag,
                interaction=inter_tag,
                now=1000.0,
            )
            state1 = await tracker.reconcile_license(self.bot, "primary-license", now=1005.0)
            self.assertEqual(state1, "completed")
            live_tag_embed = wh_tag.edit.call_args.kwargs["embed"]
            self.assertEqual(live_tag_embed.title, "✅ Instance Live: tag")
            self.channel.send.assert_not_called()

            # 2. Now activate 'pf2e' cleanly -> 'pf2e' becomes Live AND 'tag' message (101) demotes to Standby!
            wh_pf2e = SimpleNamespace(id=102, edit=AsyncMock())
            inter_pf2e = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))
            lic_pf2e = _make_lic(
                active="pf2e",
                requested="pf2e",
                annotations=self.saved_annotations,
            )
            with patch.object(k8s, "get_foundry_license", side_effect=lambda *a, **kw: lic_pf2e):
                await tracker.register_pending(
                    bot=self.bot,
                    license_name="primary-license",
                    action="activate",
                    instance_name="pf2e",
                    user_id="222",
                    channel_id=500,
                    message_id=102,
                    webhook_msg=wh_pf2e,
                    interaction=inter_pf2e,
                    now=1020.0,
                )
                state2 = await tracker.reconcile_license(self.bot, "primary-license", now=1023.0)
                self.assertEqual(state2, "completed")

            # Message 102 is green 'Instance Live: pf2e'
            live_pf2e_embed = wh_pf2e.edit.call_args.kwargs["embed"]
            self.assertEqual(live_pf2e_embed.title, "✅ Instance Live: pf2e")
            # Message 101 ('tag') was edited to Standby!
            tag_standby_embed = self._get_partial(101).edit.call_args.kwargs["embed"]
            self.assertEqual(tag_standby_embed.title, "🟠 Instance Now on Standby: tag")
            self.assertIn("pf2e", tag_standby_embed.description)

    async def test_queued_challenge_then_15m_snooze_then_completion_pings_via_partial_message(self):
        tracker = SwitchTracker()
        wh_msg = SimpleNamespace(id=201, edit=AsyncMock())
        inter = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))

        current_lic = {"val": _make_lic(
            active="tag",
            requested="pf2e",
            blocked_reason="Switch to 'pf2e' queued: awaiting user confirmation (85s remaining)",
        )}

        with patch.object(k8s, "get_foundry_license", side_effect=lambda *a, **kw: current_lic["val"]), \
             patch.object(k8s, "get_foundry_instance", side_effect=lambda n, *a, **kw: _make_inst(n)), \
             patch.object(k8s, "annotate_foundry_license", side_effect=self._mock_annotate), \
             patch.object(discord, "Embed", _FakeEmbed):
            await tracker.register_pending(
                bot=self.bot,
                license_name="primary-license",
                action="activate",
                instance_name="pf2e",
                user_id="333",
                channel_id=500,
                message_id=201,
                webhook_msg=wh_msg,
                interaction=inter,
                now=100.0,
            )

            # 1. Challenge detected immediately at t=103s
            s1 = await tracker.reconcile_license(self.bot, "primary-license", now=103.0)
            self.assertEqual(s1, "queued")
            emb1 = wh_msg.edit.call_args.kwargs["embed"]
            self.assertEqual(emb1.title, "⏳ Waiting for Active User Confirmation")
            self.assertIn("85s remaining", emb1.description)

            # 2. User on 'tag' clicks "I'm still here" -> 15m snooze
            current_lic["val"] = _make_lic(
                active="tag",
                requested="pf2e",
                blocked_reason="Switch to 'pf2e' queued: active session confirmed (14m 55s remaining)",
                annotations=self.saved_annotations,
            )
            s2 = await tracker.reconcile_license(self.bot, "primary-license", now=115.0)
            self.assertEqual(s2, "queued")
            emb2 = wh_msg.edit.call_args.kwargs["embed"]
            self.assertEqual(emb2.title, "⏳ Switch Queued — Active Session Confirmed")

            # 3. 16.5 minutes later (t=1100s > 840s webhook TTL!) -> 'pf2e' finally acquires license!
            current_lic["val"] = _make_lic(
                active="pf2e",
                requested="pf2e",
                blocked_reason="",
                annotations=self.saved_annotations,
            )
            s3 = await tracker.reconcile_license(self.bot, "primary-license", now=1100.0)
            self.assertEqual(s3, "completed")

            # Edited via channel PartialMessage (since >14m elapsed) to Instance Live!
            final_emb = self._get_partial(201).edit.call_args.kwargs["embed"]
            self.assertEqual(final_emb.title, "✅ Instance Live: pf2e")
            self.assertIn("https://pf2e.lab.jake-watson.co.uk", final_emb.description)

            # And sent a completion ping mentioning <@333>!
            self.channel.send.assert_called_once()
            ping_content = self.channel.send.call_args.kwargs["content"]
            self.assertIn("<@333>", ping_content)
            self.assertIn("pf2e", ping_content)

    async def test_rehydrates_from_license_annotation_after_bot_restart(self):
        saved = {
            ANNOTATION_DISCORD_TRACKED_MESSAGES: json.dumps({
                "active_msg": {
                    "channel_id": 500,
                    "message_id": 301,
                    "instance": "tag",
                    "user_id": "111",
                    "namespace": "foundry-vtt",
                },
                "pending_msg": {
                    "action": "activate",
                    "instance": "pf2e",
                    "channel_id": 500,
                    "message_id": 302,
                    "user_id": "222",
                    "namespace": "foundry-vtt",
                    "created_at": 100.0,
                    "was_queued": True,
                    "stage": "challenging",
                },
            })
        }
        fresh_tracker = SwitchTracker()
        lic_done = _make_lic(active="pf2e", requested="pf2e", annotations=saved)

        with patch.object(k8s, "is_connected", return_value=True), \
             patch.object(k8s, "get_foundry_licenses", return_value=[lic_done]), \
             patch.object(k8s, "get_foundry_instance", side_effect=lambda n, *a, **kw: _make_inst(n)), \
             patch.object(k8s, "annotate_foundry_license", side_effect=self._mock_annotate), \
             patch.object(discord, "Embed", _FakeEmbed):
            await fresh_tracker.reconcile_all(self.bot)

        # Old 'tag' message (301) demoted to Standby
        self.assertEqual(
            self._get_partial(301).edit.call_args.kwargs["embed"].title,
            "🟠 Instance Now on Standby: tag",
        )
        # Queued 'pf2e' message (302) promoted to Instance Live AND requester pinged
        self.assertEqual(
            self._get_partial(302).edit.call_args.kwargs["embed"].title,
            "✅ Instance Live: pf2e",
        )
        self.channel.send.assert_called_once()
        self.assertIn("<@222>", self.channel.send.call_args.kwargs["content"])

    async def test_superseded_queued_switch_updates_old_pending_message(self):
        tracker = SwitchTracker()
        wh_pf2e = SimpleNamespace(id=401, edit=AsyncMock())
        wh_unlit = SimpleNamespace(id=402, edit=AsyncMock())
        lic = _make_lic(active="tag", requested="pf2e", blocked_reason="awaiting user confirmation")

        with patch.object(k8s, "get_foundry_license", side_effect=lambda *a, **kw: lic), \
             patch.object(k8s, "annotate_foundry_license", side_effect=self._mock_annotate), \
             patch.object(discord, "Embed", _FakeEmbed):
            await tracker.register_pending(
                bot=self.bot,
                license_name="primary-license",
                action="activate",
                instance_name="pf2e",
                user_id="222",
                channel_id=500,
                message_id=401,
                webhook_msg=wh_pf2e,
                now=100.0,
            )
            await tracker.register_pending(
                bot=self.bot,
                license_name="primary-license",
                action="activate",
                instance_name="unlit",
                user_id="333",
                channel_id=500,
                message_id=402,
                webhook_msg=wh_unlit,
                now=110.0,
            )

        sup_embed = wh_pf2e.edit.call_args.kwargs["embed"]
        self.assertEqual(sup_embed.title, "🚫 Request Superseded")
        self.assertIn("unlit", sup_embed.description)


if __name__ == "__main__":
    unittest.main()
