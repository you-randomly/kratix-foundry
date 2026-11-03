"""
Async HTTP + WebSocket reverse proxy for Foundry VTT with injected "Are you still there?" overlay.
"""

import asyncio
import re
from typing import Callable, Optional, Set

from foundry_lib.presence import PresenceCoordinator

SCRIPT_TAG = b'<script src="/__platform/presence.js" defer></script>'
_BODY_CLOSE_RE = re.compile(rb"(</body\s*>)", re.IGNORECASE)
_HTML_CLOSE_RE = re.compile(rb"(</html\s*>)", re.IGNORECASE)

HOP_BY_HOP_HEADERS: Set[str] = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

STARTUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="3">
  <title>Starting Foundry VTT...</title>
  <style>
    body {
      margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
      background: #12131a; color: #ececf1; font-family: system-ui, -apple-system, sans-serif;
    }
    .card {
      padding: 2rem 2.5rem; border-radius: 12px; background: #1c1e29;
      border: 1px solid rgba(255,255,255,0.1); text-align: center; max-width: 420px;
      box-shadow: 0 20px 40px rgba(0,0,0,0.45);
    }
    .spinner {
      width: 36px; height: 36px; margin: 0 auto 1rem; border-radius: 50%;
      border: 3px solid rgba(245,158,11,0.25); border-top-color: #f59e0b;
      animation: spin 0.9s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    p { color: #a1a1b5; font-size: 0.95rem; margin: 0.5rem 0 0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="spinner"></div>
    <h2 style="margin:0;font-size:1.2rem;">Starting Foundry VTT&hellip;</h2>
    <p>This page will connect automatically in a few seconds.</p>
  </div>
</body>
</html>
"""

PRESENCE_JS = r"""(function () {
  if (window.__foundryPlatformPresenceLoaded) return;
  window.__foundryPlatformPresenceLoaded = true;

  var STORAGE_KEY = "__foundry_platform_tab_id";
  var clientId = null;
  try {
    clientId = sessionStorage.getItem(STORAGE_KEY);
    if (!clientId) {
      clientId = (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : (Math.random().toString(36).slice(2) + Date.now().toString(36));
      sessionStorage.setItem(STORAGE_KEY, clientId);
    }
  } catch (e) {
    clientId = Math.random().toString(36).slice(2);
  }

  var state = {
    challenging: false,
    remainingSeconds: 0,
    totalSeconds: 90,
    contentionKind: null,
    targetInstance: "",
    canReleaseNow: false,
    releasing: false,
    toastMessage: ""
  };

  var originalTitle = document.title;
  var titleFlashToggle = false;
  var reloadScheduled = false;

  var host = document.createElement("div");
  host.id = "foundry-platform-presence-root";
  host.style.cssText = "position:fixed;inset:0;z-index:2147483647;pointer-events:none;";
  var shadow = host.attachShadow({ mode: "open" });

  shadow.innerHTML = [
    "<style>",
    ":host { all: initial; font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }",
    ".backdrop {",
    "  position: fixed; inset: 0; display: none; align-items: flex-start; justify-content: center;",
    "  padding-top: 8vh; background: rgba(10, 11, 16, 0.62); backdrop-filter: blur(3px);",
    "  pointer-events: auto;",
    "}",
    ".backdrop.visible { display: flex; }",
    ".card {",
    "  width: min(440px, calc(100vw - 32px)); background: #1b1d28; color: #f3f4f8;",
    "  border: 1px solid rgba(245, 158, 11, 0.45); border-radius: 12px; padding: 20px 22px;",
    "  box-shadow: 0 24px 48px rgba(0, 0, 0, 0.65); animation: slideIn 0.18s ease-out;",
    "}",
    "@keyframes slideIn { from { transform: translateY(-10px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }",
    ".badge {",
    "  display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600;",
    "  text-transform: uppercase; letter-spacing: 0.05em; color: #fbbf24;",
    "  background: rgba(245, 158, 11, 0.14); padding: 4px 10px; border-radius: 999px; margin-bottom: 10px;",
    "}",
    "h2 { margin: 0 0 8px; font-size: 18px; font-weight: 650; color: #ffffff; }",
    "p { margin: 0 0 14px; font-size: 14px; line-height: 1.45; color: #c4c7d4; }",
    ".timer-row { display: flex; justify-content: space-between; font-size: 13px; color: #e2e4ed; margin-bottom: 6px; }",
    ".timer-val { font-variant-numeric: tabular-nums; font-weight: 700; color: #fbbf24; }",
    ".bar-bg { height: 6px; border-radius: 999px; background: rgba(255,255,255,0.1); overflow: hidden; margin-bottom: 18px; }",
    ".bar-fill { height: 100%; background: linear-gradient(90deg, #f59e0b, #ef4444); transition: width 0.4s linear; }",
    ".actions { display: flex; gap: 10px; justify-content: flex-end; }",
    "button {",
    "  appearance: none; border: none; border-radius: 8px; padding: 9px 16px; font-size: 14px;",
    "  font-weight: 600; cursor: pointer; transition: filter 0.15s ease, transform 0.08s ease;",
    "}",
    "button:active { transform: scale(0.98); }",
    "button:disabled { opacity: 0.55; cursor: default; }",
    ".btn-stay { background: #f59e0b; color: #111218; flex: 1; }",
    ".btn-stay:hover:not(:disabled) { filter: brightness(1.08); }",
    ".btn-release { background: rgba(255,255,255,0.09); color: #e2e4ed; border: 1px solid rgba(255,255,255,0.16); }",
    ".btn-release:hover:not(:disabled) { background: rgba(239, 68, 68, 0.22); border-color: rgba(239, 68, 68, 0.5); color: #fecaca; }",
    ".toast {",
    "  position: fixed; bottom: 20px; right: 20px; display: none; background: #1e293b; color: #e2e8f0;",
    "  border: 1px solid rgba(34, 197, 94, 0.45); border-radius: 10px; padding: 10px 16px;",
    "  font-size: 13px; font-weight: 500; box-shadow: 0 12px 28px rgba(0,0,0,0.45); pointer-events: auto;",
    "}",
    ".toast.visible { display: block; }",
    "</style>",
    "<div class='backdrop' id='backdrop'>",
    "  <div class='card' role='alertdialog' aria-modal='true' aria-labelledby='title'>",
    "    <div class='badge' id='badge'>Action Required</div>",
    "    <h2 id='title'>Are you still using Foundry?</h2>",
    "    <p id='desc'></p>",
    "    <div class='timer-row'><span id='timer-label'>Releasing in</span><span class='timer-val' id='timer'>90s</span></div>",
    "    <div class='bar-bg'><div class='bar-fill' id='bar' style='width:100%'></div></div>",
    "    <div class='actions'>",
    "      <button type='button' class='btn-release' id='release-btn' style='display:none'>Release now</button>",
    "      <button type='button' class='btn-stay' id='stay-btn'>I'm still here</button>",
    "    </div>",
    "  </div>",
    "</div>",
    "<div class='toast' id='toast'></div>"
  ].join("\n");

  function mountHost() {
    if (document.body && !document.getElementById(host.id)) {
      document.body.appendChild(host);
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mountHost);
  } else {
    mountHost();
  }

  var backdropEl = shadow.getElementById("backdrop");
  var titleEl = shadow.getElementById("title");
  var descEl = shadow.getElementById("desc");
  var timerLabelEl = shadow.getElementById("timer-label");
  var timerEl = shadow.getElementById("timer");
  var barEl = shadow.getElementById("bar");
  var stayBtn = shadow.getElementById("stay-btn");
  var releaseBtn = shadow.getElementById("release-btn");
  var toastEl = shadow.getElementById("toast");
  var toastTimer = null;

  function showToast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add("visible");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toastEl.classList.remove("visible");
    }, 3200);
  }

  function restoreTitle() {
    if (document.title.indexOf("Still here?") !== -1) {
      document.title = originalTitle;
    } else {
      originalTitle = document.title;
    }
  }

  function render() {
    mountHost();
    if (!state.challenging && !state.releasing) {
      backdropEl.classList.remove("visible");
      restoreTitle();
      return;
    }
    backdropEl.classList.add("visible");

    if (state.releasing) {
      titleEl.textContent = state.contentionKind === "password_reset"
        ? "Restarting instance\u2026"
        : "Releasing license\u2026";
      descEl.textContent = "Redirecting in a moment\u2026";
      timerLabelEl.textContent = "Status";
      timerEl.textContent = "0s";
      barEl.style.width = "0%";
      stayBtn.disabled = true;
      releaseBtn.disabled = true;
      return;
    }

    stayBtn.disabled = false;
    releaseBtn.disabled = false;

    if (state.contentionKind === "password_reset") {
      titleEl.textContent = "Admin password update pending";
      descEl.textContent = "Foundry needs a quick restart to apply a new admin password. Confirm if you're actively working to delay the restart for 15 minutes.";
      timerLabelEl.textContent = "Restarting in";
      releaseBtn.textContent = "Restart now";
    } else if (state.contentionKind === "standby") {
      titleEl.textContent = "Are you still using Foundry?";
      descEl.textContent = "This instance was requested to return to standby. Confirm to keep it active for 15 minutes.";
      timerLabelEl.textContent = "Standby in";
      releaseBtn.textContent = "Release now";
    } else {
      var target = state.targetInstance ? ("'" + state.targetInstance + "'") : "Another instance";
      titleEl.textContent = "Are you still using Foundry?";
      descEl.textContent = target + " is waiting for the Foundry license. Confirm if you're still here to keep this instance active for 15 minutes.";
      timerLabelEl.textContent = "Switching in";
      releaseBtn.textContent = "Release now";
    }

    releaseBtn.style.display = state.canReleaseNow ? "inline-block" : "none";
    timerEl.textContent = Math.max(0, state.remainingSeconds) + "s";
    var pct = state.totalSeconds > 0
      ? Math.max(0, Math.min(100, (state.remainingSeconds / state.totalSeconds) * 100))
      : 0;
    barEl.style.width = pct.toFixed(1) + "%";
  }

  stayBtn.addEventListener("click", function () {
    stayBtn.disabled = true;
    fetch("/__platform/ack?clientId=" + encodeURIComponent(clientId), { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.challenging = false;
        state.releasing = false;
        render();
        showToast("Confirmed \u2014 keeping instance active for 15 minutes.");
      })
      .catch(function () { stayBtn.disabled = false; });
  });

  releaseBtn.addEventListener("click", function () {
    releaseBtn.disabled = true;
    stayBtn.disabled = true;
    fetch("/__platform/release?clientId=" + encodeURIComponent(clientId), { method: "POST" })
      .then(function (r) {
        if (!r.ok) {
          releaseBtn.disabled = false;
          stayBtn.disabled = false;
          return;
        }
        state.releasing = true;
        render();
        if (!reloadScheduled) {
          reloadScheduled = true;
          setTimeout(function () { window.location.reload(); }, 2500);
        }
      })
      .catch(function () {
        releaseBtn.disabled = false;
        stayBtn.disabled = false;
      });
  });

  function pollStatus() {
    fetch("/__platform/status?clientId=" + encodeURIComponent(clientId), { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        var wasChallenging = state.challenging;
        state.challenging = !!data.challenging;
        state.remainingSeconds = data.remainingSeconds || 0;
        state.totalSeconds = data.totalSeconds || 90;
        state.contentionKind = data.contentionKind || null;
        state.targetInstance = data.targetInstance || "";
        state.canReleaseNow = !!data.canReleaseNow;

        if (wasChallenging && data.expired && !data.snoozed) {
          state.releasing = true;
          render();
          if (!reloadScheduled) {
            reloadScheduled = true;
            setTimeout(function () { window.location.reload(); }, 3000);
          }
          return;
        }
        if (!data.challenging) {
          state.releasing = false;
        }
        render();
      })
      .catch(function () {});
  }

  setInterval(function () {
    if (state.challenging && !state.releasing) {
      state.remainingSeconds = Math.max(0, state.remainingSeconds - 1);
      titleFlashToggle = !titleFlashToggle;
      if (document.title.indexOf("Still here?") === -1) {
        originalTitle = document.title;
      }
      document.title = titleFlashToggle
        ? ("\u26A0\uFE0F [" + state.remainingSeconds + "s] Still here? \u2014 " + originalTitle)
        : originalTitle;
      render();
    }
  }, 1000);

  window.addEventListener("pagehide", function () {
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/__platform/leave?clientId=" + encodeURIComponent(clientId));
      }
    } catch (e) {}
  });

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) pollStatus();
  });

  pollStatus();
  setInterval(pollStatus, 3000);
})();
"""


def inject_presence_script(html_bytes: bytes) -> bytes:
    """Inject the /__platform/presence.js <script> tag into an HTML document if not already present."""
    if b"/__platform/presence.js" in html_bytes:
        return html_bytes
    if _BODY_CLOSE_RE.search(html_bytes):
        return _BODY_CLOSE_RE.sub(SCRIPT_TAG + rb"\1", html_bytes, count=1)
    if _HTML_CLOSE_RE.search(html_bytes):
        return _HTML_CLOSE_RE.sub(SCRIPT_TAG + rb"\1", html_bytes, count=1)
    return html_bytes + SCRIPT_TAG


def _connection_header_tokens(headers) -> Set[str]:
    tokens: Set[str] = set()
    conn_val = headers.get("Connection", "")
    if conn_val:
        for part in conn_val.split(","):
            tok = part.strip().lower()
            if tok:
                tokens.add(tok)
    return tokens


def create_proxy_app(
    coordinator: PresenceCoordinator,
    upstream_url: str = "http://127.0.0.1:30000",
    on_state_change: Optional[Callable[[], None]] = None,
):
    """Build the aiohttp.web.Application wrapping Foundry VTT on upstream_url."""
    from aiohttp import ClientConnectorError, ClientSession, ClientTimeout, WSMsgType, web
    from multidict import CIMultiDict
    from yarl import URL

    upstream_base = upstream_url.rstrip("/")
    client_session_key = web.AppKey("client_session", ClientSession)

    async def on_startup(app: web.Application) -> None:
        timeout = ClientTimeout(total=None, connect=5, sock_connect=5, sock_read=None)
        app[client_session_key] = ClientSession(timeout=timeout, auto_decompress=False)

    async def on_cleanup(app: web.Application) -> None:
        session: Optional[ClientSession] = app.get(client_session_key)
        if session is not None:
            await session.close()

    async def handle_healthz(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def handle_presence_js(_request: web.Request) -> web.Response:
        return web.Response(
            text=PRESENCE_JS,
            content_type="application/javascript",
            charset="utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    async def handle_status(request: web.Request) -> web.Response:
        client_id = request.query.get("clientId", "").strip()
        if client_id:
            coordinator.record_tab_heartbeat(client_id)
        return web.json_response(
            coordinator.get_status(),
            headers={"Cache-Control": "no-store"},
        )

    async def handle_ack(request: web.Request) -> web.Response:
        client_id = request.query.get("clientId", "").strip()
        if client_id:
            coordinator.record_tab_heartbeat(client_id)
        status = coordinator.acknowledge()
        if on_state_change:
            on_state_change()
        return web.json_response(status)

    async def handle_release(request: web.Request) -> web.Response:
        ok, message = coordinator.request_release()
        if not ok:
            return web.json_response({"ok": False, "message": message}, status=409)
        if on_state_change:
            on_state_change()
        return web.json_response({"ok": True, "message": message, "status": coordinator.get_status()})

    async def handle_leave(request: web.Request) -> web.Response:
        client_id = request.query.get("clientId", "").strip()
        if client_id:
            coordinator.remove_tab(client_id)
        return web.Response(status=204)

    async def _proxy_websocket(request: web.Request, session: ClientSession) -> web.StreamResponse:
        target_url = URL(f"{upstream_base}{request.raw_path}", encoded=True)
        ws_headers = CIMultiDict()
        for k, v in request.headers.items():
            kl = k.lower()
            if kl in HOP_BY_HOP_HEADERS or kl.startswith("sec-websocket-"):
                continue
            ws_headers.add(k, v)

        protocols_hdr = request.headers.get("Sec-WebSocket-Protocol", "")
        protocols = [p.strip() for p in protocols_hdr.split(",") if p.strip()]

        try:
            ws_upstream = await session.ws_connect(
                target_url,
                headers=ws_headers,
                protocols=protocols,
                max_msg_size=0,
                autoping=True,
                autoclose=False,
            )
        except ClientConnectorError:
            return web.Response(status=503, text="Foundry VTT is starting up")
        except Exception as exc:
            return web.Response(status=502, text=f"Upstream WebSocket error: {exc}")

        ws_client = web.WebSocketResponse(
            protocols=protocols,
            max_msg_size=0,
            autoping=True,
            autoclose=False,
        )
        # Preserve any Set-Cookie headers returned on the upstream handshake
        if getattr(ws_upstream, "_response", None) is not None:
            for cookie_val in ws_upstream._response.headers.getall("Set-Cookie", []):
                ws_client.headers.add("Set-Cookie", cookie_val)

        await ws_client.prepare(request)
        coordinator.ws_opened()

        async def client_to_upstream() -> None:
            async for msg in ws_client:
                if msg.type == WSMsgType.TEXT:
                    await ws_upstream.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await ws_upstream.send_bytes(msg.data)
                elif msg.type == WSMsgType.CLOSE:
                    await ws_upstream.close(code=ws_client.close_code or 1000, message=msg.extra or b"")
                    break
                elif msg.type == WSMsgType.ERROR:
                    break

        async def upstream_to_client() -> None:
            async for msg in ws_upstream:
                if msg.type == WSMsgType.TEXT:
                    await ws_client.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await ws_client.send_bytes(msg.data)
                elif msg.type == WSMsgType.CLOSE:
                    await ws_client.close(code=ws_upstream.close_code or 1000, message=msg.extra or b"")
                    break
                elif msg.type == WSMsgType.ERROR:
                    break

        t1 = asyncio.create_task(client_to_upstream())
        t2 = asyncio.create_task(upstream_to_client())
        try:
            _, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            coordinator.ws_closed()
            if not ws_upstream.closed:
                await ws_upstream.close()
            if not ws_client.closed:
                await ws_client.close()

        return ws_client

    async def handle_proxy(request: web.Request) -> web.StreamResponse:
        session: ClientSession = request.app[client_session_key]

        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await _proxy_websocket(request, session)

        target_url = URL(f"{upstream_base}{request.raw_path}", encoded=True)
        req_conn_tokens = _connection_header_tokens(request.headers)
        accept_hdr = request.headers.get("Accept", "")
        wants_html = request.method == "GET" and "text/html" in accept_hdr.lower()

        fwd_headers = CIMultiDict()
        for k, v in request.headers.items():
            kl = k.lower()
            if kl in HOP_BY_HOP_HEADERS or kl in req_conn_tokens:
                continue
            if wants_html and kl == "accept-encoding":
                continue
            fwd_headers.add(k, v)
        if wants_html:
            fwd_headers["Accept-Encoding"] = "identity"

        # Add X-Forwarded-For if not already set by Gateway
        if "X-Forwarded-For" not in fwd_headers and request.remote:
            fwd_headers["X-Forwarded-For"] = request.remote

        body = None if request.method in ("GET", "HEAD") else request.content

        try:
            upstream_resp = await session.request(
                request.method,
                target_url,
                headers=fwd_headers,
                data=body,
                allow_redirects=False,
            )
        except ClientConnectorError:
            if wants_html:
                return web.Response(status=503, text=STARTUP_HTML, content_type="text/html")
            return web.json_response({"error": "Foundry VTT is starting up"}, status=503)

        async with upstream_resp:
            content_type = upstream_resp.headers.get("Content-Type", "")
            content_encoding = upstream_resp.headers.get("Content-Encoding", "").lower()
            is_injectable_html = (
                request.method == "GET"
                and "text/html" in content_type.lower()
                and content_encoding in ("", "identity")
            )

            resp_conn_tokens = _connection_header_tokens(upstream_resp.headers)
            out_headers = CIMultiDict()
            for k, v in upstream_resp.headers.items():
                kl = k.lower()
                if kl in HOP_BY_HOP_HEADERS or kl in resp_conn_tokens:
                    continue
                if is_injectable_html and kl == "content-length":
                    continue
                out_headers.add(k, v)

            if is_injectable_html:
                raw_html = await upstream_resp.read()
                patched_html = inject_presence_script(raw_html)
                out_headers["Content-Length"] = str(len(patched_html))
                return web.Response(
                    status=upstream_resp.status,
                    headers=out_headers,
                    body=patched_html,
                )

            stream_resp = web.StreamResponse(
                status=upstream_resp.status,
                reason=upstream_resp.reason,
                headers=out_headers,
            )
            await stream_resp.prepare(request)
            async for chunk in upstream_resp.content.iter_chunked(65536):
                await stream_resp.write(chunk)
            await stream_resp.write_eof()
            return stream_resp

    app = web.Application(client_max_size=0)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/__platform/healthz", handle_healthz)
    app.router.add_get("/__platform/presence.js", handle_presence_js)
    app.router.add_get("/__platform/status", handle_status)
    app.router.add_post("/__platform/ack", handle_ack)
    app.router.add_post("/__platform/release", handle_release)
    app.router.add_post("/__platform/leave", handle_leave)
    app.router.add_route("*", "/{path_info:.*}", handle_proxy)
    return app
