import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Add scripts and lib to path using relative paths
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
scripts_path = os.path.join(project_root, "promises/foundry-license/configure-pipeline/scripts")
lib_path = os.path.join(project_root, "lib")

sys.path.append(scripts_path)
sys.path.append(lib_path)

# Mock kubernetes before importing generate_route
sys.modules['kubernetes'] = MagicMock()
sys.modules['kubernetes.client'] = MagicMock()
sys.modules['kubernetes.config'] = MagicMock()
sys.modules['requests'] = MagicMock()

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

    @patch('generate_route.check_players')
    def test_public_ip_creates_dns_endpoint(self, mock_check):
        # Setup: Public IP configured
        self.resource["spec"]["gateway"] = {
            "publicIP": "1.2.3.4",
            "parentRef": {"name": "gw", "namespace": "ns"}
        }
        mock_check.return_value = {"connectedPlayers": 0}
        
        generate_route.generate_routes(self.pipeline, self.resource, self.admin_key)
        
        # Check that write_output was called for DNSEndpoints
        # We expect calls for route-instance-1, route-instance-2, dns-instance-1, dns-instance-2
        
        # Extract all filenames passed to write_output
        calls = self.pipeline.write_output.call_args_list
        filenames = [call.args[0] for call in calls]
        
        self.assertIn("dns-instance-1.yaml", filenames)
        self.assertIn("dns-instance-2.yaml", filenames)
        
        # Verify content of one DNS endpoint
        dns_call = next(call for call in calls if call.args[0] == "dns-instance-2.yaml")
        dns_content = dns_call.args[1]
        
        self.assertEqual(dns_content["kind"], "DNSEndpoint")
        self.assertEqual(dns_content["spec"]["endpoints"][0]["targets"], ["1.2.3.4"])

if __name__ == '__main__':
    unittest.main()
