import asyncio
import base64
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
bot_path = os.path.join(project_root, "discord-bot")
if bot_path not in sys.path:
    sys.path.insert(0, bot_path)

# Mock kubernetes & discord before importing k8s_client
k8s_mock = MagicMock()
sys.modules.setdefault("kubernetes", k8s_mock)
sys.modules.setdefault("kubernetes.client", k8s_mock.client)
sys.modules.setdefault("kubernetes.client.rest", k8s_mock.client.rest)
sys.modules.setdefault("kubernetes.config", k8s_mock.config)
sys.modules.setdefault("kubernetes.stream", k8s_mock.stream)

discord_mock = MagicMock()


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


discord_mock.Embed = _FakeEmbed
discord_mock.Forbidden = type("Forbidden", (Exception,), {})
discord_mock.NotFound = type("NotFound", (Exception,), {})
discord_mock.app_commands.Group = _FakeGroup
discord_mock.app_commands.describe = lambda **kwargs: (lambda fn: fn)
discord_mock.ext.commands.Cog = object

sys.modules.setdefault("discord", discord_mock)
sys.modules.setdefault("discord.ext", discord_mock.ext)
sys.modules.setdefault("discord.ext.commands", discord_mock.ext.commands)
sys.modules.setdefault("discord.app_commands", discord_mock.app_commands)
sys.modules.setdefault("dotenv", MagicMock())

import k8s_client as k8s
from cogs.password import PasswordCog


class TestPasswordFlow(unittest.TestCase):

    def setUp(self):
        k8s.k8s_api = MagicMock()

    def test_get_instance_owner_id_from_annotations(self):
        inst = {
            "metadata": {
                "name": "my-game",
                "annotations": {
                    "foundry.platform/created-by-id": "123456789"
                }
            }
        }
        self.assertEqual(k8s.get_instance_owner_id(inst), "123456789")

    def test_get_instance_owner_id_from_labels(self):
        inst = {
            "metadata": {
                "name": "my-game",
                "labels": {
                    "foundry.platform/owner-id": "987654321"
                }
            }
        }
        self.assertEqual(k8s.get_instance_owner_id(inst), "987654321")

    def test_get_instance_owner_id_none(self):
        inst = {"metadata": {"name": "legacy-instance"}}
        self.assertIsNone(k8s.get_instance_owner_id(inst))

    def test_password_secret_name_explicit(self):
        inst = {
            "metadata": {"name": "test-inst"},
            "spec": {
                "adminPasswordSecretRef": {
                    "name": "custom-secret-name"
                }
            }
        }
        self.assertEqual(k8s.password_secret_name(inst), "custom-secret-name")

    def test_password_secret_name_default(self):
        inst = {
            "metadata": {"name": "test-inst"},
            "spec": {}
        }
        self.assertEqual(k8s.password_secret_name(inst), "foundry-password-test-inst")

    def test_request_password_rotation(self):
        result = k8s.request_password_rotation("foundry-password-user-123", namespace="foundry-vtt")
        self.assertTrue(result["success"])
        k8s.k8s_api.patch_namespaced_custom_object.assert_called_once()
        call_kwargs = k8s.k8s_api.patch_namespaced_custom_object.call_args[1]
        self.assertEqual(call_kwargs["name"], "foundry-password-user-123")
        self.assertEqual(call_kwargs["plural"], "foundrypasswords")
        self.assertIn("rotateRequestedAt", call_kwargs["body"]["spec"])

    def test_annotate_foundry_password(self):
        result = k8s.annotate_foundry_password(
            "foundry-password-test",
            {"foundry.platform/password-notified-at": "2026-10-29T19:00:00Z"},
            namespace="foundry-vtt"
        )
        self.assertTrue(result["success"])
        k8s.k8s_api.patch_namespaced_custom_object.assert_called_once()
        call_kwargs = k8s.k8s_api.patch_namespaced_custom_object.call_args[1]
        self.assertEqual(call_kwargs["body"]["metadata"]["annotations"]["foundry.platform/password-notified-at"], "2026-10-29T19:00:00Z")

    @patch.object(k8s, "get_secret")
    def test_get_admin_password_plaintext(self, mock_get_secret):
        encoded = base64.b64encode(b"SuperSecretKey123").decode("utf-8")
        secret_mock = MagicMock()
        secret_mock.data = {"adminPassword": encoded}
        mock_get_secret.return_value = secret_mock

        pwd = k8s.get_admin_password_plaintext("foundry-password-test", namespace="foundry-vtt")
        self.assertEqual(pwd, "SuperSecretKey123")

    @patch.object(k8s, "get_secret")
    def test_get_admin_password_plaintext_missing(self, mock_get_secret):
        mock_get_secret.return_value = None
        self.assertIsNone(k8s.get_admin_password_plaintext("nonexistent-secret"))

    def _make_pod(self, name="foundry-camp-abc", phase="Running", deletion_ts=None, foundry_running=True):
        cs = SimpleNamespace(
            name="foundry-vtt",
            state=SimpleNamespace(running=SimpleNamespace() if foundry_running else None),
        )
        monitor_cs = SimpleNamespace(
            name="monitor",
            state=SimpleNamespace(running=SimpleNamespace()),
        )
        return SimpleNamespace(
            metadata=SimpleNamespace(name=name, deletion_timestamp=deletion_ts),
            status=SimpleNamespace(phase=phase, container_statuses=[cs, monitor_cs]),
        )

    @patch.object(k8s, "stream")
    def test_get_running_admin_password_returns_pod_env(self, mock_stream):
        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = SimpleNamespace(items=[self._make_pod()])
        k8s.client.CoreV1Api.return_value = core_api
        mock_stream.return_value = "OldRunningKey\r\n"

        pwd = k8s.get_running_admin_password("camp", namespace="custom-ns")
        self.assertEqual(pwd, "OldRunningKey")
        core_api.list_namespaced_pod.assert_called_once_with(
            "custom-ns",
            label_selector="app=foundry-vtt,instance=camp",
            _request_timeout=k8s.POD_EXEC_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            mock_stream.call_args.kwargs["_request_timeout"],
            k8s.POD_EXEC_TIMEOUT_SECONDS,
        )

    @patch.object(k8s, "stream")
    def test_get_running_admin_password_skips_waiting_or_terminating_containers(self, mock_stream):
        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = SimpleNamespace(
            items=[
                self._make_pod(name="terminating-pod", deletion_ts="2026-11-03T00:00:00Z", foundry_running=True),
                self._make_pod(name="crashloop-pod", foundry_running=False),
            ]
        )
        k8s.client.CoreV1Api.return_value = core_api

        self.assertIsNone(k8s.get_running_admin_password("camp", namespace="foundry-vtt"))
        mock_stream.assert_not_called()

    def _make_interaction(self, user_id=42):
        interaction = SimpleNamespace()
        interaction.response = SimpleNamespace(defer=AsyncMock())
        interaction.followup = SimpleNamespace(send=AsyncMock())
        interaction.user = SimpleNamespace(id=user_id, send=AsyncMock())
        return interaction

    def test_show_password_instance_pending_reset_uses_running_and_instance_namespace(self):
        cog = PasswordCog(bot=MagicMock())
        interaction = self._make_interaction(user_id=42)
        inst = {
            "metadata": {
                "name": "camp",
                "namespace": "custom-ns",
                "annotations": {"foundry.platform/created-by-id": "42"},
            },
            "spec": {"adminPasswordSecretRef": {"name": "foundry-password-camp"}},
        }

        with patch.object(k8s, "is_connected", return_value=True), \
             patch.object(k8s, "get_foundry_instance", return_value=inst), \
             patch.object(k8s, "get_admin_password_plaintext", return_value="NewSecretPass") as mock_secret, \
             patch.object(k8s, "get_running_admin_password", return_value="OldPodPass") as mock_running:
            asyncio.run(cog.show_password(interaction, name="camp"))

        mock_secret.assert_called_once_with("foundry-password-camp", "custom-ns")
        mock_running.assert_called_once_with("camp", "custom-ns")
        embed = interaction.user.send.call_args.kwargs["embed"]
        self.assertIn("password reset is pending", embed.description)
        self.assertEqual(embed.fields[0]["value"], "||`OldPodPass`||")

    def test_show_password_default_warns_when_owned_instance_pending_restart(self):
        cog = PasswordCog(bot=MagicMock())
        interaction = self._make_interaction(user_id=42)
        inst = {
            "metadata": {
                "name": "camp-1",
                "namespace": "foundry-vtt",
                "annotations": {"foundry.platform/created-by-id": "42"},
            },
            "spec": {"adminPasswordSecretRef": {"name": "foundry-password-user-42"}},
        }

        with patch.object(k8s, "is_connected", return_value=True), \
             patch.object(k8s, "get_admin_password_plaintext", return_value="RotatedDefault"), \
             patch.object(k8s, "get_foundry_instances", return_value=[inst]), \
             patch.object(k8s, "get_running_admin_password", return_value="PreviousDefault"):
            asyncio.run(cog.show_password(interaction, name=None))

        embed = interaction.user.send.call_args.kwargs["embed"]
        self.assertIn("Pending restart on: **camp-1**", embed.description)
        self.assertEqual(embed.fields[0]["value"], "||`RotatedDefault`||")


if __name__ == "__main__":
    unittest.main()
