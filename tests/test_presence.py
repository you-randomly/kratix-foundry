import os
import sys
import unittest

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
lib_path = os.path.join(project_root, "lib")
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)

from foundry_lib.activity import ActivityEvaluation
from foundry_lib.presence import PresenceCoordinator


class TestPresenceCoordinator(unittest.TestCase):
    def test_fast_path_interrupt_when_no_clients_or_activity(self):
        coord = PresenceCoordinator(instance_name="tag")
        ev = ActivityEvaluation(
            is_safe=True,
            reasons=[],
            connected_players=0,
            world_active=False,
            active_sessions=0,
            idle_sessions=0,
        )

        decision = coord.evaluate_contention(
            has_contention=True,
            contention_kind="switch",
            target_instance="pf2e",
            activity_eval=ev,
            now=1000.0,
        )
        self.assertTrue(decision.should_interrupt)
        self.assertEqual(decision.state, "empty")
        self.assertFalse(coord.get_status(1000.0)["challenging"])

    def test_challenge_countdown_and_expiry_even_with_connected_players(self):
        coord = PresenceCoordinator(
            instance_name="tag",
            challenge_timeout_seconds=90.0,
            ack_snooze_seconds=900.0,
        )
        coord.record_tab_heartbeat("tab-1", now=1000.0)
        ev = ActivityEvaluation(
            is_safe=False,
            reasons=["2 player(s) in world"],
            connected_players=2,
            world_active=True,
            active_sessions=1,
        )

        # t=1000: contention begins -> 90s challenge starts
        d0 = coord.evaluate_contention(
            has_contention=True,
            contention_kind="switch",
            target_instance="pf2e",
            activity_eval=ev,
            now=1000.0,
        )
        self.assertFalse(d0.should_interrupt)
        self.assertEqual(d0.state, "challenging")
        self.assertIn("90s remaining", d0.reasons[0])

        status = coord.get_status(1030.0)
        self.assertTrue(status["challenging"])
        self.assertEqual(status["remainingSeconds"], 60)
        self.assertEqual(status["targetInstance"], "pf2e")
        self.assertFalse(status["canReleaseNow"])

        # t=1089: still within 90s window
        d1 = coord.evaluate_contention(
            has_contention=True,
            contention_kind="switch",
            target_instance="pf2e",
            activity_eval=ev,
            now=1089.0,
        )
        self.assertFalse(d1.should_interrupt)

        # t=1090: 90s elapsed without user acknowledgement -> interrupt allowed!
        d2 = coord.evaluate_contention(
            has_contention=True,
            contention_kind="switch",
            target_instance="pf2e",
            activity_eval=ev,
            now=1090.0,
        )
        self.assertTrue(d2.should_interrupt)
        self.assertEqual(d2.state, "challenge_expired")
        self.assertTrue(coord.get_status(1090.0)["expired"])

    def test_idle_open_setup_tab_triggers_90s_challenge(self):
        """An open /setup tab with 0 players and idle TCP still gets a 90s prompt before switch."""
        coord = PresenceCoordinator(instance_name="tag", challenge_timeout_seconds=90.0)
        coord.record_tab_heartbeat("setup-tab", now=500.0)
        ev = ActivityEvaluation(
            is_safe=True,
            reasons=[],
            connected_players=0,
            world_active=False,
            active_sessions=0,
            idle_sessions=1,
        )

        d = coord.evaluate_contention(
            has_contention=True,
            contention_kind="standby",
            target_instance="",
            activity_eval=ev,
            now=501.0,
        )
        self.assertFalse(d.should_interrupt)
        self.assertEqual(d.state, "challenging")
        status = coord.get_status(501.0)
        self.assertTrue(status["challenging"])
        self.assertTrue(status["canReleaseNow"])
        self.assertEqual(status["contentionKind"], "standby")

    def test_acknowledge_snoozes_for_15_minutes_then_rechallenges(self):
        coord = PresenceCoordinator(
            instance_name="tag",
            challenge_timeout_seconds=90.0,
            ack_snooze_seconds=900.0,
        )
        coord.record_tab_heartbeat("tab-1", now=100.0)
        ev = ActivityEvaluation(
            is_safe=False,
            reasons=["1 player(s) in world"],
            connected_players=1,
            world_active=True,
        )

        coord.evaluate_contention(True, "switch", "unlit", ev, now=100.0)
        ack_status = coord.acknowledge(now=120.0)
        self.assertFalse(ack_status["challenging"])
        self.assertTrue(ack_status["snoozed"])
        self.assertEqual(ack_status["snoozeRemainingSeconds"], 900)

        # During 15m snooze (t=500)
        d_mid = coord.evaluate_contention(True, "switch", "unlit", ev, now=500.0)
        self.assertFalse(d_mid.should_interrupt)
        self.assertEqual(d_mid.state, "snoozed")
        self.assertIn("active session confirmed", d_mid.reasons[0])

        # Once 900s elapses at t=1020 -> new 90s challenge begins
        d_re = coord.evaluate_contention(True, "switch", "unlit", ev, now=1020.0)
        self.assertFalse(d_re.should_interrupt)
        self.assertEqual(d_re.state, "challenging")
        self.assertTrue(coord.get_status(1020.0)["challenging"])
        self.assertEqual(coord.get_status(1020.0)["remainingSeconds"], 90)

    def test_snooze_clears_early_after_all_clients_leave_past_grace(self):
        coord = PresenceCoordinator(
            instance_name="tag",
            challenge_timeout_seconds=90.0,
            ack_snooze_seconds=900.0,
            tab_ttl_seconds=15.0,
            empty_grace_seconds=30.0,
        )
        coord.record_tab_heartbeat("tab-1", now=100.0)
        busy_ev = ActivityEvaluation(is_safe=False, reasons=["1 active"], connected_players=0, active_sessions=1)
        coord.evaluate_contention(True, "switch", "pf2e", busy_ev, now=100.0)
        coord.acknowledge(now=110.0)

        # User closes tab; brief navigation gap (t=130, 20s since last seen < 30s grace) preserves snooze
        coord.remove_tab("tab-1")
        idle_ev = ActivityEvaluation(is_safe=True, reasons=[], connected_players=0, active_sessions=0)
        d_grace = coord.evaluate_contention(True, "switch", "pf2e", idle_ev, now=130.0)
        self.assertFalse(d_grace.should_interrupt)
        self.assertEqual(d_grace.state, "snoozed")

        # Past 30s grace (t=145, 35s since last seen) -> snooze clears and switch proceeds immediately
        d_gone = coord.evaluate_contention(True, "switch", "pf2e", idle_ev, now=145.0)
        self.assertTrue(d_gone.should_interrupt)
        self.assertEqual(d_gone.state, "empty")

    def test_request_release_requires_zero_connected_players(self):
        coord = PresenceCoordinator(instance_name="tag")
        coord.record_tab_heartbeat("gm-tab", now=10.0)

        # Fails when players > 0
        coord.connected_players = 2
        ok, _ = coord.request_release(now=12.0)
        self.assertFalse(ok)
        self.assertFalse(coord.release_now_requested)

        # Succeeds when players == 0
        coord.connected_players = 0
        ok, _ = coord.request_release(now=15.0)
        self.assertTrue(ok)
        self.assertTrue(coord.release_now_requested)

        setup_ev = ActivityEvaluation(
            is_safe=False,
            reasons=["1 active browser session(s) (Setup)"],
            connected_players=0,
            active_sessions=1,
        )
        decision = coord.evaluate_contention(True, "switch", "pf2e", setup_ev, now=15.0)
        self.assertTrue(decision.should_interrupt)
        self.assertEqual(decision.state, "released_by_user")

    def test_downloads_always_block_even_after_challenge_expiry_or_release(self):
        coord = PresenceCoordinator(instance_name="tag", challenge_timeout_seconds=90.0)
        coord.record_tab_heartbeat("setup-tab", now=0.0)
        coord.connected_players = 0
        coord.request_release(now=5.0)

        dl_ev = ActivityEvaluation(
            is_safe=False,
            reasons=["package download in progress"],
            connected_players=0,
            downloads_active=True,
        )
        decision = coord.evaluate_contention(True, "switch", "pf2e", dl_ev, now=200.0)
        self.assertFalse(decision.should_interrupt)
        self.assertEqual(decision.state, "download_blocked")
        self.assertIn("package download in progress", decision.reasons)

    def test_mark_released_sets_expired_briefly_for_browser_reload(self):
        coord = PresenceCoordinator(instance_name="tag")
        coord.mark_released(now=500.0)
        self.assertTrue(coord.get_status(505.0)["expired"])
        self.assertFalse(coord.get_status(520.0)["expired"])


if __name__ == "__main__":
    unittest.main()
