import os
import sys
import unittest
from aiohttp import ClientSession, WSMsgType, web
from multidict import CIMultiDict

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
lib_path = os.path.join(project_root, "lib")
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)

from foundry_lib.activity import ActivityEvaluation
from foundry_lib.presence import PresenceCoordinator
from foundry_lib.proxy import SCRIPT_TAG, create_proxy_app, inject_presence_script


class TestProxyWrapper(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream_requests = []
        self.wake_calls = 0

        async def up_setup(request: web.Request) -> web.Response:
            self.upstream_requests.append((request.method, request.raw_path, dict(request.headers)))
            headers = CIMultiDict()
            headers.add("Set-Cookie", "session=abc; Path=/; HttpOnly")
            headers.add("Set-Cookie", "theme=dark; Path=/")
            html = "<!DOCTYPE html><html><head><title>Foundry</title></head><body><h1>Setup</h1></body></html>"
            return web.Response(text=html, content_type="text/html", headers=headers)

        async def up_redirect(_request: web.Request) -> web.Response:
            return web.Response(status=302, headers={"Location": "/setup"})

        async def up_asset(request: web.Request) -> web.Response:
            self.upstream_requests.append((request.method, request.raw_path, dict(request.headers)))
            return web.Response(body=b"\x89PNG\r\n\x1a\nrawbytes", content_type="image/png")

        async def up_upload(request: web.Request) -> web.Response:
            payload = await request.read()
            return web.json_response({"bytes": len(payload), "raw_path": request.raw_path})

        async def up_socket(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await ws.send_str(f"echo:{msg.data}")
                elif msg.type == WSMsgType.BINARY:
                    await ws.send_bytes(b"bin:" + msg.data)
            return ws

        upstream_app = web.Application(client_max_size=0)
        upstream_app.router.add_get("/setup", up_setup)
        upstream_app.router.add_get("/go-setup", up_redirect)
        upstream_app.router.add_get("/worlds/my%20world/map.png", up_asset)
        upstream_app.router.add_get("/worlds/my world/map.png", up_asset)
        upstream_app.router.add_post("/upload", up_upload)
        upstream_app.router.add_get("/socket.io/", up_socket)

        self.upstream_runner = web.AppRunner(upstream_app)
        await self.upstream_runner.setup()
        self.upstream_site = web.TCPSite(self.upstream_runner, "127.0.0.1", 0)
        await self.upstream_site.start()
        up_port = self.upstream_site._server.sockets[0].getsockname()[1]
        self.upstream_url = f"http://127.0.0.1:{up_port}"

        self.coordinator = PresenceCoordinator(instance_name="tag")

        def on_wake():
            self.wake_calls += 1

        proxy_app = create_proxy_app(
            coordinator=self.coordinator,
            upstream_url=self.upstream_url,
            on_state_change=on_wake,
        )
        self.proxy_runner = web.AppRunner(proxy_app)
        await self.proxy_runner.setup()
        self.proxy_site = web.TCPSite(self.proxy_runner, "127.0.0.1", 0)
        await self.proxy_site.start()
        proxy_port = self.proxy_site._server.sockets[0].getsockname()[1]
        self.proxy_url = f"http://127.0.0.1:{proxy_port}"

        self.client = ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.proxy_runner.cleanup()
        await self.upstream_runner.cleanup()

    def test_inject_presence_script_unit(self):
        raw = b"<html><body><div>Hello</div></body></html>"
        injected = inject_presence_script(raw)
        self.assertIn(SCRIPT_TAG, injected)
        # Idempotent if called twice
        self.assertEqual(injected, inject_presence_script(injected))

    async def test_html_injection_and_multi_set_cookie_preservation(self):
        async with self.client.get(
            f"{self.proxy_url}/setup",
            headers={"Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "gzip, br"},
        ) as resp:
            self.assertEqual(resp.status, 200)
            body = await resp.read()
            self.assertIn(SCRIPT_TAG, body)
            cookies = resp.headers.getall("Set-Cookie", [])
            self.assertEqual(len(cookies), 2)
            # Upstream should have received Accept-Encoding: identity for HTML
            _, _, up_headers = self.upstream_requests[-1]
            self.assertEqual(up_headers.get("Accept-Encoding"), "identity")

    async def test_redirect_not_followed_by_proxy(self):
        async with self.client.get(f"{self.proxy_url}/go-setup", allow_redirects=False) as resp:
            self.assertEqual(resp.status, 302)
            self.assertEqual(resp.headers.get("Location"), "/setup")

    async def test_binary_passthrough_and_encoded_url_and_upload_streaming(self):
        async with self.client.get(f"{self.proxy_url}/worlds/my%20world/map.png") as resp:
            self.assertEqual(resp.status, 200)
            data = await resp.read()
            self.assertEqual(data, b"\x89PNG\r\n\x1a\nrawbytes")
            self.assertNotIn(SCRIPT_TAG, data)
            _, raw_path, _ = self.upstream_requests[-1]
            self.assertEqual(raw_path, "/worlds/my%20world/map.png")

        big_payload = b"x" * (512 * 1024)
        async with self.client.post(f"{self.proxy_url}/upload", data=big_payload) as resp:
            self.assertEqual(resp.status, 200)
            info = await resp.json()
            self.assertEqual(info["bytes"], len(big_payload))

    async def test_websocket_bidirectional_relay_and_tracking(self):
        self.assertEqual(self.coordinator.open_ws_count, 0)
        async with self.client.ws_connect(f"{self.proxy_url}/socket.io/?EIO=4&transport=websocket") as ws:
            self.assertEqual(self.coordinator.open_ws_count, 1)

            await ws.send_str("42[\"world\",{}]")
            reply = await ws.receive()
            self.assertEqual(reply.type, WSMsgType.TEXT)
            self.assertEqual(reply.data, "echo:42[\"world\",{}]")

            await ws.send_bytes(b"\x01\x02\x03")
            bin_reply = await ws.receive()
            self.assertEqual(bin_reply.type, WSMsgType.BINARY)
            self.assertEqual(bin_reply.data, b"bin:\x01\x02\x03")

        # Give proxy cleanup task a tick to finalize ws_closed()
        async with self.client.get(f"{self.proxy_url}/__platform/healthz") as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(self.coordinator.open_ws_count, 0)

    async def test_platform_endpoints_status_ack_and_release_guard(self):
        # 1. Healthz & JS asset
        async with self.client.get(f"{self.proxy_url}/__platform/healthz") as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(await r.json(), {"ok": True})

        async with self.client.get(f"{self.proxy_url}/__platform/presence.js") as r:
            self.assertEqual(r.status, 200)
            js_text = await r.text()
            self.assertIn("foundry-platform-presence-root", js_text)

        # 2. Heartbeat via /__platform/status registers tab
        async with self.client.get(f"{self.proxy_url}/__platform/status?clientId=tab-a") as r:
            status = await r.json()
            self.assertFalse(status["challenging"])
            self.assertEqual(self.coordinator.active_tab_count(), 1)

        # 3. Start contention with 1 player connected -> release rejected with 409, ack succeeds
        ev_players = ActivityEvaluation(
            is_safe=False,
            reasons=["1 player(s) in world"],
            connected_players=1,
            world_active=True,
        )
        self.coordinator.evaluate_contention(True, "switch", "pf2e", ev_players)

        async with self.client.get(f"{self.proxy_url}/__platform/status?clientId=tab-a") as r:
            st = await r.json()
            self.assertTrue(st["challenging"])
            self.assertFalse(st["canReleaseNow"])

        async with self.client.post(f"{self.proxy_url}/__platform/release?clientId=tab-a") as r:
            self.assertEqual(r.status, 409)

        async with self.client.post(f"{self.proxy_url}/__platform/ack?clientId=tab-a") as r:
            self.assertEqual(r.status, 200)
            ack_st = await r.json()
            self.assertTrue(ack_st["snoozed"])
            self.assertFalse(ack_st["challenging"])
            self.assertGreaterEqual(self.wake_calls, 1)

        # 4. When 0 players connected -> POST /__platform/release succeeds with 200
        self.coordinator.connected_players = 0
        async with self.client.post(f"{self.proxy_url}/__platform/release?clientId=tab-a") as r:
            self.assertEqual(r.status, 200)
            rel = await r.json()
            self.assertTrue(rel["ok"])
            self.assertTrue(self.coordinator.release_now_requested)

        # 5. Tab leave removes heartbeat
        async with self.client.post(f"{self.proxy_url}/__platform/leave?clientId=tab-a") as r:
            self.assertEqual(r.status, 204)
            self.assertEqual(self.coordinator.active_tab_count(), 0)

    async def test_startup_503_page_when_upstream_unreachable(self):
        # Stop upstream server to simulate Foundry booting
        await self.upstream_runner.cleanup()

        async with self.client.get(
            f"{self.proxy_url}/setup",
            headers={"Accept": "text/html"},
        ) as r:
            self.assertEqual(r.status, 503)
            text = await r.text()
            self.assertIn("Starting Foundry VTT", text)

        async with self.client.get(
            f"{self.proxy_url}/api/status",
            headers={"Accept": "application/json"},
        ) as r:
            self.assertEqual(r.status, 503)
            data = await r.json()
            self.assertIn("starting up", data["error"])


if __name__ == "__main__":
    unittest.main()
