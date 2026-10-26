import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
lib_path = os.path.join(project_root, "lib")
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)

sys.modules.setdefault("kubernetes", MagicMock())
sys.modules.setdefault("kubernetes.client", MagicMock())
sys.modules.setdefault("kubernetes.config", MagicMock())
sys.modules.setdefault("requests", MagicMock())

from foundry_lib.sidecar_monitor import (
    ANNOTATION_REQUESTED_INSTANCE,
    ANNOTATION_SWITCH_BLOCKED_REASON,
    ANNOTATION_SWITCH_MODE,
    is_lease_expired,
    reconcile_lease,
)


def make_lease(
    holder_identity: str = "",
    requested_instance: str = "",
    switch_mode: str = "block",
    renew_time: datetime = None,
    lease_duration_seconds: int = 60,
    lease_transitions: int = 0,
):
    annotations = {
        ANNOTATION_REQUESTED_INSTANCE: requested_instance,
        ANNOTATION_SWITCH_MODE: switch_mode,
    }
    return SimpleNamespace(
        metadata=SimpleNamespace(name="main-license", namespace="foundry-vtt", annotations=annotations),
        spec=SimpleNamespace(
            holder_identity=holder_identity,
            acquire_time=renew_time,
            renew_time=renew_time,
            lease_duration_seconds=lease_duration_seconds,
            lease_transitions=lease_transitions,
        ),
    )


class TestSidecarLeaseMonitor(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 26, 20, 0, 0, tzinfo=timezone.utc)
        self.coord_api = MagicMock()

    def test_acquire_empty_lease_when_requested(self):
        lease = make_lease(holder_identity="", requested_instance="instance-a")
        self.coord_api.read_namespaced_lease.return_value = lease

        result = reconcile_lease(
            coordination_api=self.coord_api,
            instance_name="instance-a",
            license_name="main-license",
            namespace="foundry-vtt",
            stats={"connectedPlayers": 0},
            now=self.now,
        )

        self.assertEqual(result["action"], "acquired")
        self.assertTrue(result["isActive"])
        self.assertEqual(lease.spec.holder_identity, "instance-a")
        self.assertEqual(lease.spec.renew_time, self.now)
        self.assertEqual(lease.spec.lease_transitions, 1)
        self.coord_api.replace_namespaced_lease.assert_called_once()

    def test_renew_held_lease_when_still_requested(self):
        earlier = self.now - timedelta(seconds=15)
        lease = make_lease(
            holder_identity="instance-a",
            requested_instance="instance-a",
            renew_time=earlier,
            lease_transitions=2,
        )
        self.coord_api.read_namespaced_lease.return_value = lease

        result = reconcile_lease(
            coordination_api=self.coord_api,
            instance_name="instance-a",
            license_name="main-license",
            namespace="foundry-vtt",
            stats={"connectedPlayers": 3},
            now=self.now,
        )

        self.assertEqual(result["action"], "renewed")
        self.assertTrue(result["isActive"])
        self.assertEqual(lease.spec.renew_time, self.now)
        self.assertEqual(lease.spec.lease_transitions, 2)

    def test_block_switch_when_players_connected(self):
        earlier = self.now - timedelta(seconds=15)
        lease = make_lease(
            holder_identity="instance-a",
            requested_instance="instance-b",
            switch_mode="block",
            renew_time=earlier,
        )
        self.coord_api.read_namespaced_lease.return_value = lease

        result = reconcile_lease(
            coordination_api=self.coord_api,
            instance_name="instance-a",
            license_name="main-license",
            namespace="foundry-vtt",
            stats={"connectedPlayers": 4},
            now=self.now,
        )

        self.assertEqual(result["action"], "blocked_release")
        self.assertTrue(result["isActive"])
        self.assertEqual(lease.spec.holder_identity, "instance-a")
        self.assertEqual(lease.spec.renew_time, self.now)
        self.assertIn("4 player(s) connected", lease.metadata.annotations[ANNOTATION_SWITCH_BLOCKED_REASON])

    def test_release_lease_when_zero_players_in_block_mode(self):
        earlier = self.now - timedelta(seconds=15)
        lease = make_lease(
            holder_identity="instance-a",
            requested_instance="instance-b",
            switch_mode="block",
            renew_time=earlier,
        )
        lease.metadata.annotations[ANNOTATION_SWITCH_BLOCKED_REASON] = "previous block"
        self.coord_api.read_namespaced_lease.return_value = lease

        result = reconcile_lease(
            coordination_api=self.coord_api,
            instance_name="instance-a",
            license_name="main-license",
            namespace="foundry-vtt",
            stats={"connectedPlayers": 0},
            now=self.now,
        )

        self.assertEqual(result["action"], "released")
        self.assertFalse(result["isActive"])
        self.assertEqual(lease.spec.holder_identity, "")
        self.assertIsNone(lease.spec.renew_time)
        self.assertNotIn(ANNOTATION_SWITCH_BLOCKED_REASON, lease.metadata.annotations)

    def test_force_mode_releases_immediately_despite_players(self):
        earlier = self.now - timedelta(seconds=15)
        lease = make_lease(
            holder_identity="instance-a",
            requested_instance="instance-b",
            switch_mode="force",
            renew_time=earlier,
        )
        self.coord_api.read_namespaced_lease.return_value = lease

        result = reconcile_lease(
            coordination_api=self.coord_api,
            instance_name="instance-a",
            license_name="main-license",
            namespace="foundry-vtt",
            stats={"connectedPlayers": 5},
            now=self.now,
        )

        self.assertEqual(result["action"], "released")
        self.assertFalse(result["isActive"])
        self.assertEqual(lease.spec.holder_identity, "")

    def test_waiting_instance_does_not_steal_valid_lease(self):
        earlier = self.now - timedelta(seconds=15)
        lease = make_lease(
            holder_identity="instance-a",
            requested_instance="instance-b",
            switch_mode="block",
            renew_time=earlier,
            lease_duration_seconds=60,
        )
        self.coord_api.read_namespaced_lease.return_value = lease

        result = reconcile_lease(
            coordination_api=self.coord_api,
            instance_name="instance-b",
            license_name="main-license",
            namespace="foundry-vtt",
            stats={"connectedPlayers": 0},
            now=self.now,
        )

        self.assertEqual(result["action"], "waiting")
        self.assertFalse(result["isActive"])
        self.assertEqual(result["holder"], "instance-a")
        self.coord_api.replace_namespaced_lease.assert_not_called()

    def test_takeover_expired_lease(self):
        expired_time = self.now - timedelta(seconds=120)
        lease = make_lease(
            holder_identity="instance-a",
            requested_instance="instance-b",
            renew_time=expired_time,
            lease_duration_seconds=60,
            lease_transitions=1,
        )
        self.coord_api.read_namespaced_lease.return_value = lease

        self.assertTrue(is_lease_expired(lease, now=self.now))

        result = reconcile_lease(
            coordination_api=self.coord_api,
            instance_name="instance-b",
            license_name="main-license",
            namespace="foundry-vtt",
            stats={"connectedPlayers": 0},
            now=self.now,
        )

        self.assertEqual(result["action"], "acquired")
        self.assertTrue(result["isActive"])
        self.assertEqual(lease.spec.holder_identity, "instance-b")
        self.assertEqual(lease.spec.lease_transitions, 2)


if __name__ == "__main__":
    unittest.main()
