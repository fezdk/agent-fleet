"""REST API routes for sessions."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from fleet_manager import db
from fleet_manager.config import get_config
from fleet_manager.tmux_bridge import capture_output
from fleet_manager.session_launcher import (
    start_session,
    stop_session,
    fork_session,
    start_web_session,
    restart_web_session,
    LaunchError,
)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class RegisterPayload(BaseModel):
    session_id: str
    tmux_session: str | None = None
    tmux_pane: str = "0"
    project_root: str | None = None
    session_type: str = "agent"
    agent: str = "opencode"
    web_url: str | None = None
    web_port: int | None = None


class StartPayload(BaseModel):
    agent: str = "opencode"
    name: str = ""
    project: str


class WebSessionPayload(BaseModel):
    name: str = ""
    project_root: str
    web_url: str | None = None
    web_port: int | None = None


class RestartPayload(BaseModel):
    pass


class ForkPayload(BaseModel):
    new_name: str


@router.get("")
async def list_sessions():
    sessions = db.get_all_sessions()
    for s in sessions:
        s["queued_messages"] = len(db.get_queued_messages(s["session_id"]))
    return sessions


@router.post("")
async def register_session(payload: RegisterPayload):
    existing = db.get_session(payload.session_id)
    if existing:
        raise HTTPException(409, f"Session '{payload.session_id}' already exists")
    tmux_session = payload.tmux_session or f"fleet-{payload.session_id}"
    session = db.create_session(
        payload.session_id,
        tmux_session,
        payload.tmux_pane,
        payload.project_root,
        payload.agent,
        payload.session_type,
        payload.web_url,
        payload.web_port,
    )
    return session


@router.post("/start")
async def start_new_session(payload: StartPayload):
    project = payload.project.rstrip("/")
    name = payload.name.strip() or project.rstrip("/").rsplit("/", 1)[-1]
    agent = payload.agent.strip() or "opencode"

    cfg = get_config()
    try:
        session = await start_session(name, project, agent, cfg.server.port)
    except LaunchError as e:
        raise HTTPException(400, str(e))
    return session


@router.post("/web")
async def register_web_session(payload: WebSessionPayload):
    project_root = payload.project_root.rstrip("/")
    name = payload.name.strip() or project_root.rsplit("/", 1)[-1]
    if not project_root:
        raise HTTPException(400, "project_root is required")
    if db.get_session(name):
        raise HTTPException(409, f"Session '{name}' already exists")

    if payload.web_url:
        return db.create_session(
            name,
            "",
            project_root=project_root,
            agent="opencode",
            session_type="opencode_web",
            web_url=payload.web_url,
            web_port=payload.web_port,
            web_host="127.0.0.1",
        )

    try:
        return await start_web_session(name, project_root, payload.web_port)
    except LaunchError as e:
        raise HTTPException(400, str(e))


@router.post("/{session_id}/restart")
async def restart_session(session_id: str, _payload: RestartPayload | None = None):
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")
    if session.get("session_type") != "opencode_web":
        raise HTTPException(400, f"Session '{session_id}' cannot be restarted via this endpoint")
    try:
        return await restart_web_session(session_id)
    except LaunchError as e:
        raise HTTPException(400, str(e))


@router.get("/{session_id}")
async def get_session(session_id: str):
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")

    cfg = get_config()
    session["status_log"] = db.get_status_log(session_id, cfg.ui.max_status_history)
    return session


@router.get("/{session_id}/output")
async def get_session_output(session_id: str, lines: int | None = None):
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")
    if session.get("session_type") != "agent":
        raise HTTPException(400, f"Session '{session_id}' has no terminal output")

    cfg = get_config()
    capture_lines = min(lines or cfg.ui.terminal_capture_lines, 5000)
    try:
        output = await capture_output(
            session["tmux_session"],
            session["tmux_pane"],
            capture_lines,
        )
        return {"output": output, "lines": capture_lines}
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.post("/{session_id}/fork")
async def fork_existing_session(session_id: str, payload: ForkPayload):
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")
    if session.get("session_type") != "agent":
        raise HTTPException(400, f"Session '{session_id}' cannot be forked")

    cfg = get_config()
    try:
        session = await fork_session(session_id, payload.new_name.strip(), cfg.server.port)
    except LaunchError as e:
        raise HTTPException(400, str(e))
    return session


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")

    await stop_session(session_id)
    return {"deleted": True}


@router.post("/{session_id}/set_idle")
async def set_session_idle(session_id: str):
    """Set a session to IDLE state to allow queued messages to be delivered."""
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")

    from fleet_manager.server import deliver_queued_for_session
    from fleet_manager.config import get_config
    
    cfg = get_config()
    # Update state to IDLE
    db.update_status(session_id, "IDLE", f"Manually set to IDLE", session.get("project_root"))
    # Deliver queued messages
    session = db.get_session(session_id)
    await deliver_queued_for_session(session, cfg.sessions.message_prefix)
    
    return {"ok": True, "session": session}
