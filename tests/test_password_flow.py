import base64
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
sys.modules.setdefault("discord", MagicMock())
sys.modules.setdefault("discord.ext", MagicMock())
sys.modules.setdefault("discord.app_commands", MagicMock())
sys.modules.setdefault("dotenv", MagicMock())

import k8s_client as k8s


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


if __name__ == "__main__":
    unittest.main()
