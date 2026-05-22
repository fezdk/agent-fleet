"""Main server — FastAPI app with REST API, WebSocket, and MCP server."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
from urllib.parse import urlencode, urlsplit, urlunsplit
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import uvicorn
from fastapi import FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
import websockets
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from pathlib import Path

from fleet_manager.config import load_config, get_config
from fleet_manager.db import (
    init_db, get_all_sessions, get_queued_messages,
    mark_message_delivered, update_status, create_session, get_session,
)
from fleet_manager.tmux_bridge import inject_input, session_exists, list_sessions as list_tmux_sessions
from fleet_manager.ws_manager import ws_manager
from fleet_manager.mcp_server import mcp
from fleet_manager.auth import AuthMiddleware, verify_ws_token
from fleet_manager.notifications import init_notifications, notify_stale
from fleet_manager.api.sessions import router as sessions_router
from fleet_manager.api.questions import router as questions_router
from fleet_manager.api.messages import router as messages_router
from fleet_manager.api.filesystem import router as filesystem_router

logger = logging.getLogger(__name__)

_start_time: datetime | None = None
_WORKSPACE_PREFIX = "/workspace"
_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


async def deliver_queued_for_session(session: dict, prefix: str) -> None:
    """Deliver queued messages to a session that is ready to receive input."""
    queued = get_queued_messages(session["session_id"])
    for msg in queued:
        content = msg["content"] if msg.get("raw") else f"{prefix} {msg['content']}"
        try:
            await inject_input(session["tmux_session"], session["tmux_pane"], content)
            mark_message_delivered(msg["message_id"])
            logger.info("Delivered queued message %s to %s", msg["message_id"], session["session_id"])
        except RuntimeError as e:
            logger.warning("Failed to deliver message %s: %s", msg["message_id"], e)
        break  # One message at a time to avoid overwhelming the session


async def _queue_delivery_loop(interval: int, prefix: str) -> None:
    """Periodically deliver queued messages to sessions that are IDLE or AWAITING_INPUT."""
    while True:
        await asyncio.sleep(interval)
        try:
            sessions = get_all_sessions()
            for session in sessions:
                if session["state"] not in ("IDLE", "AWAITING_INPUT"):
                    continue
                await deliver_queued_for_session(session, prefix)
        except Exception:
            logger.exception("Error in queue delivery loop")


async def _heartbeat_loop(stale_minutes: int) -> None:
    """Detect stale sessions that stopped reporting."""
    while True:
        await asyncio.sleep(60)
        try:
            sessions = get_all_sessions()
            for session in sessions:
                if session["state"] == "IDLE":
                    continue
                last_seen = session.get("last_seen")
                if not last_seen:
                    continue
                try:
                    last_dt = datetime.fromisoformat(last_seen).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                age_minutes = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                if age_minutes > stale_minutes:
                    alive = await session_exists(session["tmux_session"])
                    if not alive:
                        logger.warning("Session %s: tmux session gone, marking ERROR", session["session_id"])
                        updated = update_status(
                            session["session_id"], "ERROR",
                            "tmux session no longer exists",
                            detail=f"Last seen {int(age_minutes)}m ago, tmux session not found"
                        )
                        await ws_manager.broadcast("session:update", updated)
                        await notify_stale(session["session_id"], int(age_minutes))
                    else:
                        logger.warning("Session %s: stale (%dm since last report)", session["session_id"], int(age_minutes))
                        await ws_manager.broadcast("session:stale", {
                            "session_id": session["session_id"],
                            "minutes_since_update": int(age_minutes),
                        })
                        await notify_stale(session["session_id"], int(age_minutes))
                        # If stale session has queued messages, try delivering anyway —
                        # the session may have lost its fleet prompt via context compression
                        # and stopped reporting state, but is still accepting input.
                        queued = get_queued_messages(session["session_id"])
                        if queued:
                            cfg = get_config()
                            logger.info("Session %s: stale with %d queued messages, attempting delivery",
                                        session["session_id"], len(queued))
                            await deliver_queued_for_session(session, cfg.sessions.message_prefix)
        except Exception:
            logger.exception("Error in heartbeat loop")


async def _auto_discover_tmux(prefix: str) -> None:
    """Discover existing fleet tmux sessions and auto-register them."""
    try:
        tmux_sessions = await list_tmux_sessions()
        for name in tmux_sessions:
            if not name.startswith(prefix):
                continue
            session_id = name[len(prefix):]
            if not get_session(session_id):
                create_session(session_id, name)
                logger.info("Auto-discovered tmux session: %s -> %s", name, session_id)
    except RuntimeError:
        pass  # tmux not running


def _get_workspace_session(session_id: str) -> dict:
    session = get_session(session_id)
    if not session:
        raise KeyError(f"Session '{session_id}' not found")
    if session.get("session_type") != "opencode_web":
        raise ValueError(f"Session '{session_id}' is not an OpenCode web workspace")
    return session


def _workspace_session_from_host(host_header: str | None) -> str | None:
    if not host_header:
        return None
    host = host_header.split(":", 1)[0].strip().lower()
    if not host or "." not in host:
        return None
    session_id = host.split(".", 1)[0]
    try:
        session = _get_workspace_session(session_id)
    except (KeyError, ValueError):
        return None
    return str(session["session_id"])


def _workspace_upstream_base(session: dict) -> str:
    host = session.get("web_host") or "127.0.0.1"
    port = session.get("web_port")
    if port and session.get("process_pid"):
        return f"http://{host}:{port}"
    if session.get("web_url"):
        return str(session["web_url"]).rstrip("/")
    if not port:
        raise ValueError(f"Session '{session['session_id']}' has no web endpoint configured")
    return f"http://{host}:{port}"


def _workspace_proxy_base(session_id: str) -> str:
    return f"{_WORKSPACE_PREFIX}/{session_id}"


def _workspace_request_base(request: Request, session_id: str) -> str:
    host_session = _workspace_session_from_host(request.headers.get("host"))
    if host_session == session_id:
        return ""
    return _workspace_proxy_base(session_id)


def _rewrite_workspace_location(location: str, session: dict, request_base: str) -> str:
    parsed = urlsplit(location)
    upstream = urlsplit(_workspace_upstream_base(session))
    base = request_base

    if location.startswith("/"):
        return f"{base}{location}"

    if parsed.scheme and parsed.netloc == upstream.netloc:
        rewritten = parsed.path or "/"
        if parsed.query:
            rewritten = f"{rewritten}?{parsed.query}"
        if parsed.fragment:
            rewritten = f"{rewritten}#{parsed.fragment}"
        return f"{base}{rewritten}"

    return location


def _rewrite_root_paths(content: str, request_base: str, prefixes: tuple[str, ...]) -> str:
    base = request_base
    alternates = "|".join(re.escape(prefix) for prefix in prefixes)
    pattern = re.compile(rf'(?P<prefix>["\'(=:,\s])/(?P<target>{alternates})(?P<rest>[A-Za-z0-9_./?=&%-]*)')

    def repl(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}{base}/{match.group('target')}{match.group('rest')}"

    return pattern.sub(repl, content)


def _rewrite_workspace_text(content: str, session_id: str, request_base: str, content_type: str = "") -> str:
    base = request_base
    lowered = (content_type or "").lower()
    replacements = [
        ('href="/', f'href="{base}/'),
        ("href='/", f"href='{base}/"),
        ('src="/', f'src="{base}/'),
        ("src='/", f"src='{base}/"),
        ('content="/', f'content="{base}/'),
        ("content='/", f"content='{base}/"),
        ('url(/', f'url({base}/'),
        ('"/assets/', f'"{base}/assets/'),
        ("'/assets/", f"'{base}/assets/"),
        ('"/api/', f'"{base}/api/'),
        ("'/api/", f"'{base}/api/"),
        ('"/favicon', f'"{base}/favicon'),
        ("'/favicon", f"'{base}/favicon"),
        ('"/apple-touch', f'"{base}/apple-touch'),
        ("'/apple-touch", f"'{base}/apple-touch"),
        ('"/site.webmanifest', f'"{base}/site.webmanifest'),
        ("'/site.webmanifest", f"'{base}/site.webmanifest"),
        ('"/social-share', f'"{base}/social-share'),
        ("'/social-share", f"'{base}/social-share"),
        ('url("/', f'url("{base}/'),
        ("url('/", f"url('{base}/"),
        ('new URL("/', f'new URL("{base}/'),
        ("new URL('/", f"new URL('{base}/"),
        ('fetch("/', f'fetch("{base}/'),
        ("fetch('/", f"fetch('{base}/"),
        ('url:"/', f'url:"{base}/'),
        ("url:'/", f"url:'{base}/"),
    ]
    for source, target in replacements:
        content = content.replace(source, target)
    if "text/html" in lowered and "<head>" in content:
        pass
    if "javascript" in lowered or "application/json" in lowered:
        content = _rewrite_root_asset_query(content, session_id, request_base)
    if "javascript" in lowered:
        content = re.sub(r"\n//# sourceMappingURL=.*$", "", content, flags=re.MULTILINE)
    content = _rewrite_root_paths(
        content,
        request_base,
        (
            'assets', 'api', 'favicon', 'favicon-96x96-v3.png', 'favicon-v3.svg',
            'favicon-v3.ico', 'apple-touch-icon-v3.png', 'site.webmanifest',
            'social-share.png', 'manifest.json',
        ),
    )
    return content


def _workspace_response_needs_rewrite(content_type: str) -> bool:
    lowered = (content_type or "").lower()
    return any(kind in lowered for kind in ("text/html", "javascript", "text/css", "application/json"))


def _is_event_stream(content_type: str) -> bool:
    return "text/event-stream" in (content_type or "").lower()


def _filtered_query_items(query_params) -> list[tuple[str, str]]:
    return [(key, value) for key, value in query_params.multi_items() if key != "token"]


def _workspace_session_from_referer(referer: str | None) -> str | None:
    if not referer:
        return None
    try:
        path = urlsplit(referer).path
    except ValueError:
        return None
    prefix = f"{_WORKSPACE_PREFIX}/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    session_id = rest.split("/", 1)[0].strip()
    return session_id or None


def _rewrite_root_asset_query(content: str, session_id: str, request_base: str) -> str:
    base = request_base if request_base else ""
    workspace_base = _workspace_proxy_base(session_id)
    for marker in ('/assets/', 'assets/'):
        for quote in ('"', "'"):
            token = f"{quote}{marker}"
            start = 0
            while True:
                idx = content.find(token, start)
                if idx == -1:
                    break
                end = content.find(quote, idx + len(token))
                if end == -1:
                    break
                url = content[idx + 1:end]
                if marker == 'assets/' and idx > 0 and content[idx - 1] == '/':
                    start = end + 1
                    continue
                current_base = base if base else ''
                if current_base and url.startswith(current_base + '/assets/'):
                    start = end + 1
                    continue
                if not current_base and url.startswith('/assets/'):
                    start = end + 1
                    continue
                suffix = url[len('/assets/'):] if url.startswith('/assets/') else url[len('assets/'):]
                new_url = f"{current_base}/assets/{suffix}" if current_base else f"/assets/{suffix}"
                content = content[:idx + 1] + new_url + content[end:]
                start = idx + 1 + len(new_url) + 1
    return content


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _start_time
    _start_time = datetime.now(timezone.utc)

    cfg = load_config()
    init_db()
    logger.info("Database initialized")

    # Initialize notifications
    notifications_cfg = getattr(cfg, "notifications", None)
    init_notifications(notifications_cfg.__dict__ if notifications_cfg else None)

    # Auto-discover existing fleet tmux sessions
    await _auto_discover_tmux(cfg.tmux.session_prefix)

    # Start background tasks
    queue_task = asyncio.create_task(
        _queue_delivery_loop(cfg.sessions.queue_check_interval_seconds, cfg.sessions.message_prefix)
    )
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(cfg.sessions.stale_timeout_minutes)
    )

    # Start MCP session manager (sub-app lifespan doesn't auto-run when mounted)
    async with mcp.session_manager.run():
        yield

    queue_task.cancel()
    heartbeat_task.cancel()
    for t in (queue_task, heartbeat_task):
        try:
            await t
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Claude Fleet Manager", version="0.1.0", lifespan=lifespan)

# Auth middleware
app.add_middleware(AuthMiddleware)

# REST API routes
app.include_router(sessions_router)
app.include_router(questions_router)
app.include_router(messages_router)
app.include_router(filesystem_router)

# Mount MCP server (stateless Streamable HTTP — resilient to server restarts)
app.mount("/mcp", mcp.streamable_http_app())


# Auth check endpoint (skips auth middleware)
@app.get("/api/auth/check")
async def auth_check(authorization: str | None = Header(None)):
    token = get_config().server.auth_token
    if not token:
        return {"auth_required": False}
    result = {"auth_required": True}
    if authorization and authorization == f"Bearer {token}":
        result["valid"] = True
    elif authorization:
        result["valid"] = False
    return result


# Config endpoint
@app.get("/api/config")
async def get_config_endpoint():
    cfg = get_config()
    return {
        "terminal_mode": cfg.ui.terminal_mode,
    }


# Health endpoint
@app.get("/api/health")
async def health():
    sessions = get_all_sessions()
    return {
        "status": "ok",
        "uptime_seconds": int((datetime.now(timezone.utc) - _start_time).total_seconds()) if _start_time else 0,
        "sessions": len(sessions),
        "sessions_by_state": {
            state: sum(1 for s in sessions if s["state"] == state)
            for state in {"IDLE", "WORKING", "AWAITING_INPUT", "ERROR"}
            if any(s["state"] == state for s in sessions)
        },
        "ws_clients": ws_manager.client_count,
    }


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if not verify_ws_token(ws):
        await ws.close(code=4001, reason="Unauthorized")
        return
    await ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)


@app.get(f"{_WORKSPACE_PREFIX}/{{session_id}}/bootstrap.js")
async def workspace_bootstrap_js(session_id: str):
    return Response("", media_type="text/javascript")


@app.api_route(f"{_WORKSPACE_PREFIX}/{{session_id}}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.api_route(f"{_WORKSPACE_PREFIX}/{{session_id}}/{{path:path}}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def workspace_proxy(request: Request, session_id: str, path: str = ""):
    try:
        session = _get_workspace_session(session_id)
        upstream_base = _workspace_upstream_base(session)
    except (KeyError, ValueError) as exc:
        return Response(str(exc), status_code=404)

    upstream_path = f"/{path}" if path else "/"
    query = urlencode(_filtered_query_items(request.query_params), doseq=True)
    upstream_url = f"{upstream_base}{upstream_path}"
    if query:
        upstream_url = f"{upstream_url}?{query}"

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS and key.lower() != "authorization"
    }
    body = await request.body()

    client = httpx.AsyncClient(follow_redirects=False, timeout=None)
    try:
        upstream_request = client.build_request(request.method, upstream_url, headers=headers, content=body)
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("Workspace proxy unavailable for %s: %s", session_id, exc)
        return Response(
            (
                "<html><head>"
                "<meta http-equiv='refresh' content='1'>"
                "<script>setTimeout(function(){ location.reload(); }, 1000);</script>"
                "</head><body style='font-family:sans-serif;background:#111827;color:#e5e7eb;"
                "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
                "<div style='max-width:540px;padding:24px;text-align:center'>"
                "<h2 style='margin:0 0 12px'>Workspace restarting</h2>"
                "<p style='margin:0;color:#9ca3af'>The OpenCode workspace is temporarily unavailable while Fleet reconnects to it. Try again in a moment.</p>"
                "</div></body></html>"
            ),
            status_code=503,
            media_type="text/html",
        )

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS and key.lower() not in {"content-length", "content-encoding"}
    }
    request_base = _workspace_request_base(request, session_id)
    if "location" in response_headers:
        response_headers["location"] = _rewrite_workspace_location(response_headers["location"], session, request_base)
    content_type = upstream.headers.get("content-type", "")
    if _is_event_stream(content_type):
        async def stream_body():
            async for chunk in upstream.aiter_bytes():
                yield chunk

        async def close_stream() -> None:
            await upstream.aclose()
            await client.aclose()

        return StreamingResponse(
            stream_body(),
            status_code=upstream.status_code,
            headers=response_headers,
            media_type=content_type,
            background=BackgroundTask(close_stream),
        )

    content = await upstream.aread()
    await upstream.aclose()
    await client.aclose()
    if _workspace_response_needs_rewrite(content_type):
        text = upstream.text
        text = _rewrite_workspace_text(text, session_id, request_base, content_type)
        content = text.encode(upstream.encoding or "utf-8")
    return Response(content=content, status_code=upstream.status_code, headers=response_headers, media_type=None)


@app.websocket(f"{_WORKSPACE_PREFIX}/{{session_id}}")
@app.websocket(f"{_WORKSPACE_PREFIX}/{{session_id}}/{{path:path}}")
async def workspace_proxy_websocket(ws: WebSocket, session_id: str, path: str = ""):
    try:
        session = _get_workspace_session(session_id)
        upstream_base = _workspace_upstream_base(session)
    except (KeyError, ValueError):
        await ws.close(code=4404, reason="Workspace not found")
        return

    parsed = urlsplit(upstream_base)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    upstream_path = f"/{path}" if path else "/"
    query = urlencode([(key, value) for key, value in ws.query_params.multi_items() if key != "token"], doseq=True)
    upstream_url = urlunsplit((ws_scheme, parsed.netloc, upstream_path, query, ""))

    try:
        subprotocol_header = ws.headers.get("sec-websocket-protocol", "")
        subprotocols = [item.strip() for item in subprotocol_header.split(",") if item.strip()]
        upstream_headers = {
            key: value
            for key, value in {
                "origin": ws.headers.get("origin"),
                "cookie": ws.headers.get("cookie"),
                "user-agent": ws.headers.get("user-agent"),
            }.items()
            if value
        }

        async with websockets.connect(
            upstream_url,
            open_timeout=20,
            additional_headers=upstream_headers,
            subprotocols=subprotocols or None,
        ) as upstream:
            await ws.accept(subprotocol=upstream.subprotocol)
            async def client_to_upstream() -> None:
                while True:
                    message = await ws.receive()
                    if message.get("type") == "websocket.disconnect":
                        await upstream.close()
                        return
                    if message.get("text") is not None:
                        await upstream.send(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream.send(message["bytes"])

            async def upstream_to_client() -> None:
                while True:
                    message = await upstream.recv()
                    if isinstance(message, bytes):
                        await ws.send_bytes(message)
                    else:
                        await ws.send_text(message)

            client_task = asyncio.create_task(client_to_upstream())
            upstream_task = asyncio.create_task(upstream_to_client())
            done, pending = await asyncio.wait(
                {client_task, upstream_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
    except WebSocketDisconnect:
        return
    except Exception:
        await ws.close(code=1011, reason="Workspace proxy error")


@app.api_route("/assets/{path:path}", methods=["GET", "HEAD"])
async def workspace_asset_fallback(request: Request, path: str):
    session_id = request.query_params.get("workspace_session")
    if not session_id:
        session_id = _workspace_session_from_referer(request.headers.get("referer"))
    if not session_id:
        return Response('{"detail":"Not Found"}', status_code=404, media_type="application/json")
    return await workspace_proxy(request, session_id, f"assets/{path}")


# No-cache middleware for static web assets (edit HTML/CSS/JS without restarting)
class _NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        workspace_session_id = _workspace_session_from_host(request.headers.get("host"))
        if workspace_session_id:
            workspace_path = path.lstrip("/")
            return await workspace_proxy(request, workspace_session_id, workspace_path)
        # Skip middleware wrapping for MCP endpoints (breaks streaming)
        if path.startswith("/mcp"):
            return await call_next(request)
        response = await call_next(request)
        if path.endswith(('.html', '.css', '.js')) or path == '/':
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response

app.add_middleware(_NoCacheStaticMiddleware)

# Serve static web UI
web_dir = Path(__file__).parent.parent / "web"
if web_dir.exists():
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")


class _TokenRedactFilter(logging.Filter):
    """Redact auth tokens from uvicorn access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "args") and isinstance(record.args, tuple):
            record.args = tuple(
                str(a).replace(a.split("token=")[1].split("&")[0].split(" ")[0].split('"')[0], "***")
                if isinstance(a, str) and "token=" in a
                else a
                for a in record.args
            )
        msg = record.getMessage()
        if "token=" in msg:
            import re
            record.msg = re.sub(r"token=[^&\s\"']+", "token=***", record.msg)
            record.args = None
        return True


def _handle_sighup(*_args) -> None:
    """Re-exec the server process on SIGHUP for graceful restart."""
    logger.info("SIGHUP received — restarting server")
    os.execv(sys.executable, [sys.executable] + sys.argv)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    logging.getLogger("uvicorn.access").addFilter(_TokenRedactFilter())

    signal.signal(signal.SIGHUP, _handle_sighup)

    cfg = load_config()
    logger.info("Starting Fleet Manager on %s:%d", cfg.server.host, cfg.server.port)
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port)


if __name__ == "__main__":
    main()
