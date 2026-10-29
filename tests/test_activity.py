import os
import sys
import unittest

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
lib_path = os.path.join(project_root, "lib")
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)

from foundry_lib.activity import (
    ConnectionInfo,
    DownloadTracker,
    SessionTracker,
    evaluate_activity,
    is_public_ip,
    normalize_ip,
    parse_endpoint,
    parse_ss,
)

SAMPLE_SS_OUTPUT = """
=== 1793299431
0      0      [::ffff:10.244.3.24]:30000 [::ffff:10.244.2.26]:45729
	 cubic wscale:10,10 rto:204 rtt:0.118/0.092 bytes_received:9274795 bytes_sent:6847409 lastsnd:1356 lastrcv:1344
all TIME-WAIT 10.244.3.24:33030 104.20.36.157:443
0      0      10.244.3.24:41234 18.237.161.213:443
	 cubic wscale:10,10 bytes_received:50000 bytes_sent:2000
"""

class TestActivity(unittest.TestCase):

    def test_normalize_ip(self):
        self.assertEqual(normalize_ip("[::ffff:10.244.2.26]"), "10.244.2.26")
        self.assertEqual(normalize_ip("::ffff:10.244.2.26"), "10.244.2.26")
        self.assertEqual(normalize_ip("192.168.1.5"), "192.168.1.5")
        self.assertEqual(normalize_ip("127.0.0.1"), "127.0.0.1")

    def test_is_public_ip(self):
        # Private / loopback / link-local / k8s service
        self.assertFalse(is_public_ip("127.0.0.1"))
        self.assertFalse(is_public_ip("::1"))
        self.assertFalse(is_public_ip("10.244.2.26"))
        self.assertFalse(is_public_ip("10.96.0.1"))
        self.assertFalse(is_public_ip("192.168.1.10"))
        self.assertFalse(is_public_ip("172.16.5.1"))
        self.assertFalse(is_public_ip("[::ffff:10.244.3.24]"))

        # Public IPs (Cloudflare, AWS, DNS)
        self.assertTrue(is_public_ip("104.20.36.157"))
        self.assertTrue(is_public_ip("18.237.161.213"))
        self.assertTrue(is_public_ip("1.1.1.1"))
        self.assertTrue(is_public_ip("8.8.8.8"))

    def test_parse_endpoint(self):
        ip, port = parse_endpoint("[::ffff:10.244.3.24]:30000")
        self.assertEqual(ip, "10.244.3.24")
        self.assertEqual(port, 30000)

        ip, port = parse_endpoint("104.20.36.157:443")
        self.assertEqual(ip, "104.20.36.157")
        self.assertEqual(port, 443)

    def test_parse_ss(self):
        conns = parse_ss(SAMPLE_SS_OUTPUT)
        self.assertEqual(len(conns), 3)

        # Inbound connection from gateway
        c0 = conns[0]
        self.assertEqual(c0.local_port, 30000)
        self.assertEqual(c0.remote_ip, "10.244.2.26")
        self.assertEqual(c0.remote_port, 45729)
        self.assertEqual(c0.bytes_received, 9274795)
        self.assertEqual(c0.bytes_sent, 6847409)
        self.assertEqual(c0.lastrcv, 1344)
        self.assertEqual(c0.state, "ESTAB")

        # Closed connection to Cloudflare
        c1 = conns[1]
        self.assertEqual(c1.state, "TIME-WAIT")
        self.assertEqual(c1.remote_ip, "104.20.36.157")
        self.assertEqual(c1.remote_port, 443)

        # Outbound connection to AWS
        c2 = conns[2]
        self.assertEqual(c2.remote_ip, "18.237.161.213")
        self.assertEqual(c2.remote_port, 443)
        self.assertEqual(c2.bytes_received, 50000)

    def test_session_tracker_idle_vs_active(self):
        tracker = SessionTracker(idle_threshold_seconds=300.0, max_heartbeat_rate_bps=1.0)
        t0 = 1000.0

        conn1 = ConnectionInfo(
            local_ip="10.244.3.24", local_port=30000,
            remote_ip="10.244.2.26", remote_port=45729,
            state="ESTAB", bytes_received=100
        )

        # 1. At start (t0), session duration is 0 < 300s -> active
        active, idle = tracker.update([conn1], now=t0)
        self.assertEqual(active, 1)
        self.assertEqual(idle, 0)

        # 2. At t0 + 200s (duration < 300s) -> still active
        conn1.bytes_received = 150
        active, idle = tracker.update([conn1], now=t0 + 200.0)
        self.assertEqual(active, 1)
        self.assertEqual(idle, 0)

        # 3. At t0 + 350s with only heartbeat traffic:
        # Received 100 bytes over 350s = 0.28 B/s (below max_heartbeat_rate_bps 1.0)
        conn1.bytes_received = 200
        active, idle = tracker.update([conn1], now=t0 + 350.0)
        self.assertEqual(active, 0)
        self.assertEqual(idle, 1)

        # 4. At t0 + 360s, burst of 5 KB received -> active!
        conn1.bytes_received = 5200
        active, idle = tracker.update([conn1], now=t0 + 360.0)
        self.assertEqual(active, 1)
        self.assertEqual(idle, 0)

        # 5. Connection closes -> 0 active, 0 idle
        active, idle = tracker.update([], now=t0 + 370.0)
        self.assertEqual(active, 0)
        self.assertEqual(idle, 0)

    def test_download_tracker(self):
        tracker = DownloadTracker(settle_seconds=60.0)
        t0 = 1000.0

        out_conn = ConnectionInfo(
            local_ip="10.244.3.24", local_port=41000,
            remote_ip="104.20.36.157", remote_port=443,  # Public Cloudflare IP
            state="ESTAB", bytes_received=100000
        )

        # 1. Outbound stream active
        downloading, settling = tracker.update([out_conn], now=t0)
        self.assertTrue(downloading)
        self.assertFalse(settling)

        # 2. Download finishes and connection drops at t0 + 10s
        downloading, settling = tracker.update([], now=t0 + 10.0)
        self.assertFalse(downloading)
        self.assertTrue(settling)

        # 3. Still within 60s settle window at t0 + 50s
        downloading, settling = tracker.update([], now=t0 + 50.0)
        self.assertFalse(downloading)
        self.assertTrue(settling)

        # 4. Settle elapsed at t0 + 75s
        downloading, settling = tracker.update([], now=t0 + 75.0)
        self.assertFalse(downloading)
        self.assertFalse(settling)

    def test_evaluate_activity(self):
        # Case 1: Active players in world -> blocks
        eval1 = evaluate_activity(
            connected_players=2, world_active=True,
            active_sessions=0, idle_sessions=0,
            downloads_active=False, downloads_settling=False
        )
        self.assertFalse(eval1.is_safe)
        self.assertIn("2 player(s) connected to world", eval1.reasons)

        # Case 2: 0 players, but active Setup session -> blocks
        eval2 = evaluate_activity(
            connected_players=0, world_active=False,
            active_sessions=1, idle_sessions=0,
            downloads_active=False, downloads_settling=False
        )
        self.assertFalse(eval2.is_safe)
        self.assertIn("1 active browser session(s) (Setup)", eval2.reasons)

        # Case 3: 0 players, 0 active sessions, download active -> blocks
        eval3 = evaluate_activity(
            connected_players=0, world_active=False,
            active_sessions=0, idle_sessions=1,
            downloads_active=True, downloads_settling=False
        )
        self.assertFalse(eval3.is_safe)
        self.assertIn("package download in progress", eval3.reasons)

        # Case 4: 0 players, 0 active sessions, 1 idle session, no downloads -> SAFE!
        eval4 = evaluate_activity(
            connected_players=0, world_active=False,
            active_sessions=0, idle_sessions=1,
            downloads_active=False, downloads_settling=False
        )
        self.assertTrue(eval4.is_safe)
        self.assertEqual(eval4.reasons, [])

        # Case 5: 0 players, 0 sessions, no downloads -> SAFE!
        eval5 = evaluate_activity(
            connected_players=0, world_active=False,
            active_sessions=0, idle_sessions=0,
            downloads_active=False, downloads_settling=False
        )
        self.assertTrue(eval5.is_safe)
        self.assertEqual(eval5.reasons, [])

if __name__ == "__main__":
    unittest.main()
