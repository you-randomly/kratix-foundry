import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Add scripts and lib to path
sys.path.append("/Users/hd25646/Documents/kratix-foundry/promises/foundry-license/configure-pipeline/scripts")
sys.path.append("/Users/hd25646/Documents/kratix-foundry/lib")

# Mock kubernetes before importing generate_route
sys.modules['kubernetes'] = MagicMock()
sys.modules['kubernetes.client'] = MagicMock()
sys.modules['kubernetes.config'] = MagicMock()

import generate_route

class TestGenerateRoute(unittest.TestCase):
    def setUp(self):
        self.pipeline = MagicMock()
        self.resource = {
            "metadata": {"name": "test-license", "namespace": "default"},
            "spec": {
                "activeInstanceName": "instance-2",
                "switchMode": "block"
            },
            "status": {
                "activeInstance": "instance-1"
            }
        }
        self.admin_key = "test-key"

        # Mock CustomObjectsApi
        self.mock_custom_api = MagicMock()
        generate_route.client.CustomObjectsApi.return_value = self.mock_custom_api
        
        # Mock instances list
        self.mock_custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "instance-1"},
                    "spec": {"licenseRef": {"name": "test-license"}}
                },
                {
                    "metadata": {"name": "instance-2"},
                    "spec": {"licenseRef": {"name": "test-license"}}
                }
            ]
        }

    @patch('generate_route.check_players')
    def test_switch_blocked_by_players(self, mock_check):
        # Setup: 5 players connected to instance-1
        mock_check.return_value = {"connectedPlayers": 5}
        
        status = generate_route.generate_routes(self.pipeline, self.resource, self.admin_key)
        
        # Verify instance-1 remains active
        self.assertEqual(status["activeInstance"], "instance-1")
        self.assertIn("blocked", status["warning"])
        self.assertEqual(status["registeredInstances"][0]["state"], "active") # instance-1
        self.assertEqual(status["registeredInstances"][1]["state"], "standby") # instance-2

    @patch('generate_route.check_players')
    def test_switch_allowed_no_players(self, mock_check):
        # Setup: 0 players connected to instance-1
        mock_check.return_value = {"connectedPlayers": 0}
        
        status = generate_route.generate_routes(self.pipeline, self.resource, self.admin_key)
        
        # Verify instance-2 becomes active
        self.assertEqual(status["activeInstance"], "instance-2")
        self.assertNotIn("warning", status)
        self.assertEqual(status["registeredInstances"][0]["state"], "standby") # instance-1
        self.assertEqual(status["registeredInstances"][1]["state"], "active") # instance-2

    @patch('generate_route.check_players')
    def test_switch_force_mode_with_players(self, mock_check):
        # Setup: 5 players connected, but mode is FORCE
        self.resource["spec"]["switchMode"] = "force"
        mock_check.return_value = {"connectedPlayers": 5}
        
        status = generate_route.generate_routes(self.pipeline, self.resource, self.admin_key)
        
        # Verify instance-2 becomes active despite players
        self.assertEqual(status["activeInstance"], "instance-2")
        self.assertNotIn("warning", status)
        mock_check.assert_not_called()

if __name__ == '__main__':
    unittest.main()
