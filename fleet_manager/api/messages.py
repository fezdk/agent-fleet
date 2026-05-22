"""REST API routes for sending messages (instructions) to sessions."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from fleet_manager import db
from fleet_manager.config import get_config
from fleet_manager.tmux_bridge import inject_input, send_raw_keys
from fleet_manager.ws_manager import ws_manager

router = APIRouter(prefix="/api/sessions", tags=["messages"])


def _require_agent_session(session_id: str) -> dict:
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")
    if session.get("session_type") != "agent":
        raise HTTPException(400, f"Session '{session_id}' does not support terminal controls")
    return session


class MessagePayload(BaseModel):
    content: str
    from_client: str = "web"
    urgent: bool = False
    raw: bool = False  # If True, skip the [fleet] prefix


@router.post("/{session_id}/message")
async def send_message(session_id: str, payload: MessagePayload):
    session = _require_agent_session(session_id)

    cfg = get_config()
    prefix = cfg.sessions.message_prefix
    inject_content = payload.content if payload.raw else f"{prefix} {payload.content}"

    message = db.create_inbox_message(session_id, payload.content, payload.from_client, raw=payload.raw)

    state = session["state"]

    # Decide delivery strategy based on session state
    if state == "IDLE" or payload.urgent:
        # Inject immediately
        await inject_input(session["tmux_session"], session["tmux_pane"], inject_content)
        db.mark_message_delivered(message["message_id"])
        message["delivered"] = True
        message["delivery_method"] = "immediate"
    elif state == "AWAITING_INPUT":
        # Inject immediately (answering a pending question)
        await inject_input(session["tmux_session"], session["tmux_pane"], payload.content)
        db.mark_message_delivered(message["message_id"])
        message["delivered"] = True
        message["delivery_method"] = "awaiting_input"
    else:
        # WORKING — queue for later delivery
        message["delivery_method"] = "queued"

    await ws_manager.broadcast("session:message", message)
    return message


# Allowed tmux key names to prevent injection
_ALLOWED_KEYS = {
    "Up", "Down", "Left", "Right",
    "Enter", "Escape", "Tab", "BTab",
    "Space", "BSpace",
    "Home", "End", "PageUp", "PageDown",
    "DC",  # Delete
    "y", "n",
    "C-c", "C-d", "C-z",
    # Mouse scroll (for opencode TUI mode)
    "ScrollUp", "ScrollDown",
}


class KeysPayload(BaseModel):
    keys: list[str]


@router.post("/{session_id}/keys")
async def send_keys(session_id: str, payload: KeysPayload):
    """Send raw keystrokes to a session's tmux pane (no [fleet] prefix)."""
    session = _require_agent_session(session_id)

    # Validate keys to prevent arbitrary command injection
    for key in payload.keys:
        if key not in _ALLOWED_KEYS:
            raise HTTPException(400, f"Key '{key}' is not allowed")

    await send_raw_keys(session["tmux_session"], session["tmux_pane"], payload.keys)
    return {"sent": payload.keys}


@router.post("/{session_id}/unstick")
async def unstick_session(session_id: str):
    """Emergency unstick: send 'wait' + two Enters to wake a stuck session.

    Use when Claude Code finished or was canceled but didn't report status,
    leaving the fleet manager in WORKING state and queuing all messages.
    """
    session = _require_agent_session(session_id)

    await inject_input(session["tmux_session"], session["tmux_pane"], "wait")
    return {"unstuck": True, "session_id": session_id}


_REMIND_TEMPLATE = (
    "[fleet] REMINDER - READ CAREFULLY:\n"
    "You are fleet session '{session_id}'.\n\n"
    "CRITICAL RULES - STRICTLY FOLLOW THESE:\n\n"
    "1. Call report_status IMMEDIATELY when:\n"
    "   - Task starts -> state=WORKING\n"
    "   - Task completes -> state=IDLE (always, even if nothing happened)\n"
    "   - Task stopped -> state=IDLE\n"
    "   - Error -> state=ERROR\n"
    "   - About to ask question -> state=AWAITING_INPUT\n"
    "   - Question answered -> state=WORKING\n\n"
    "2. NEVER finish a task without report_status(state='IDLE')\n"
    "3. NEVER walk away without report_status(state='IDLE')\n"
    "4. If user says 'stop', 'done', 'thanks' -> report_status(state='IDLE')\n"
    "5. Before asking questions, call relay_question FIRST, then ask as plain text\n"
    "6. NEVER use AskUserQuestion - use plain text only\n"
    "7. Messages prefixed [fleet] are remote instructions - execute them\n"
    "8. MCP tools: report_status, relay_question\n\n"
    "Example: await report_status(session_id='{session_id}', state='IDLE', summary='Task complete')"
)


@router.post("/{session_id}/remind")
async def remind_session(session_id: str):
    """Re-inject fleet instructions into a session that may have lost them via context compression."""
    session = _require_agent_session(session_id)

    message = _REMIND_TEMPLATE.format(session_id=session_id)
    await inject_input(session["tmux_session"], session["tmux_pane"], message)
    return {"reminded": True, "session_id": session_id}
