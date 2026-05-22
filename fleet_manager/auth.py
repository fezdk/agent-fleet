"""Bearer token authentication middleware.

Uses a raw ASGI middleware instead of Starlette's BaseHTTPMiddleware to avoid
breaking streaming responses.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import WebSocket
from starlette.types import ASGIApp, Receive, Scope, Send

from fleet_manager.config import get_config
from fleet_manager import db

# Paths that skip auth
_SKIP_PREFIXES = ("/style", "/app.", "/favicon", "/workspace/", "/assets/")
_SKIP_EXACT = {"/", "/ws", "/api/auth/check", "/api/config"}


def _workspace_host_session(host_header: str) -> str | None:
    host = host_header.split(":", 1)[0].strip().lower()
    if not host or "." not in host:
        return None
    session_id = host.split(".", 1)[0]
    try:
        session = db.get_session(session_id)
    except Exception:
        return None
    if session and session.get("session_type") == "opencode_web":
        return session_id
    return None


class AuthMiddleware:
    """Raw ASGI middleware for bearer token auth.

    Passes requests through without wrapping the response body, so
    streaming responses work correctly.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        token = get_config().server.auth_token
        if not token:
            # Auth disabled — pass through
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Skip auth for public/static/MCP paths
        if path in _SKIP_EXACT or any(path.startswith(p) for p in _SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Extract token from headers or query string
        headers = dict(scope.get("headers", []))
        host_header = headers.get(b"host", b"").decode()

        if _workspace_host_session(host_header):
            await self.app(scope, receive, send)
            return

        auth_header = headers.get(b"authorization", b"").decode()
        query_string = scope.get("query_string", b"").decode()

        # Check Authorization header
        if auth_header == f"Bearer {token}":
            await self.app(scope, receive, send)
            return

        # Check query param
        for param in query_string.split("&"):
            if param.startswith("token=") and param[6:] == token:
                await self.app(scope, receive, send)
                return

        # Reject
        if scope["type"] == "http":
            body = json.dumps({"detail": "Invalid or missing auth token"}).encode()
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": body,
            })
        # For websocket, just close
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4001})


def verify_ws_token(ws: WebSocket) -> bool:
    """Check WebSocket connection for valid auth token."""
    token = get_config().server.auth_token
    if not token:
        return True
    if ws.query_params.get("token") == token:
        return True
    auth = ws.headers.get("authorization", "")
    return auth == f"Bearer {token}"
