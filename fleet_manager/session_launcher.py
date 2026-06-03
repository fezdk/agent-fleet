"""Shared session launch/stop logic used by both CLI and API."""

from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request

from fleet_manager import db
from fleet_manager.config import get_config
from fleet_manager.prompt_template import generate_prompt
from fleet_manager.tmux_bridge import session_exists, kill_session

logger = logging.getLogger(__name__)

TMUX_PREFIX = "fleet-"
WEB_HOST = "127.0.0.1"
USER_BIN_PATHS = ("~/.opencode/bin", "~/.npm-global/bin", "~/.local/bin")


class LaunchError(Exception):
    """Raised when session launch fails validation or setup."""


def _tmux_sync(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def _env_with_user_bins() -> dict[str, str]:
    env = os.environ.copy()
    paths = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    for path in reversed(USER_BIN_PATHS):
        expanded = os.path.expanduser(path)
        if expanded not in paths:
            paths.insert(0, expanded)
    env["PATH"] = os.pathsep.join(paths)
    return env


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((WEB_HOST, 0))
        return int(sock.getsockname()[1])


def _wait_for_http(url: str, timeout_seconds: float = 20.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status < 500:
                    return
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
        time.sleep(0.5)
    raise LaunchError(f"Timed out waiting for web workspace at {url}: {last_error}")


def _log_file_for_session(name: str) -> str:
    return f"/tmp/fleet-opencode-web-{name}.log"


async def start_session(
    name: str,
    project: str,
    agent: str = "opencode",
    port: int = 7700,
) -> dict:
    """Start a new fleet-managed agent session.

    Validates inputs, creates tmux session, registers MCP, launches agent.
    Supports agents: "claude-code", "opencode", "copilot", "codex"
    Returns the created session dict.

    Raises LaunchError on validation failures.
    """
    # Validate agent
    valid_agents = ["claude-code", "opencode", "copilot", "codex"]
    if agent not in valid_agents:
        raise LaunchError(f"Unknown agent '{agent}'. Valid options: {', '.join(valid_agents)}")

    tmux_name = f"{TMUX_PREFIX}{name}"

    # Validate project path
    if not os.path.isdir(project):
        raise LaunchError(f"Project path does not exist: {project}")

    # Check for name collision
    if await session_exists(tmux_name):
        raise LaunchError(f"tmux session '{tmux_name}' already exists")
    if db.get_session(name):
        raise LaunchError(f"Session '{name}' already exists")

    # Create tmux session with configured dimensions
    cfg = get_config()
    result = _tmux_sync(
        "new-session", "-d", "-s", tmux_name, "-c", project,
        "-x", str(cfg.tmux.default_width), "-y", str(cfg.tmux.default_height),
    )
    if result.returncode != 0:
        raise LaunchError(f"Failed to create tmux session: {result.stderr.strip()}")
    _tmux_sync("set-option", "-t", f"={tmux_name}", "status-left", f" [{name}] ")
    _tmux_sync("set-option", "-t", f"={tmux_name}", "status-right", " Detach: Ctrl+B, D  %H:%M ")
    _tmux_sync("set-option", "-t", f"={tmux_name}", "status-style", "bg=#0969da,fg=#ffffff")
    # Enable mouse scrollback and increase history buffer
    _tmux_sync("set-option", "-t", f"={tmux_name}", "mouse", "on")
    _tmux_sync("set-option", "-t", f"={tmux_name}", "history-limit", "10000")
    # Set pane colors to match dashboard terminal theme
    _tmux_sync("select-pane", "-t", f"={tmux_name}:0", "-P", "bg=#1e1e2e,fg=#cdd6f4")

    # Build MCP URL, config, and fleet system prompt
    mcp_url = f"http://127.0.0.1:{port}/mcp/mcp"
    auth_token = cfg.server.auth_token
    mcp_config = {
        "fleet-manager": {
            "type": "remote",
            "url": mcp_url,
            "enabled": True
        }
    }
    if auth_token:
        mcp_config["fleet-manager"]["headers"] = {
            "Authorization": f"Bearer {auth_token}"
        }

    # Build fleet system prompt
    fleet_prompt = generate_prompt(name, mcp_url=mcp_url)

    # Build launch command based on agent type
    if agent == "opencode":
        agent_cmd = "opencode"
        config_file = f"{project}/opencode.json"
        config_content = json.dumps({"mcp": mcp_config}, indent=2)
        config_check = f"if [ ! -f {project}/opencode.json ]; then\n"
        config_write = f"  cat > {project}/opencode.json << 'OPENCODE_EOF'\n{config_content}\nOPENCODE_EOF\n"
    elif agent == "copilot":
        agent_cmd = "copilot"
        agent_arg = "-i"  # interactive mode with initial prompt
        config_file = f"{project}/.copilot.json"
        config_content = json.dumps({"mcp": mcp_config}, indent=2)
        config_check = f"if [ ! -f {project}/.copilot.json ]; then\n"
        config_write = f"  cat > {project}/.copilot.json << 'COPILOT_EOF'\n{config_content}\nCOPILOT_EOF\n"
    elif agent == "codex":
        agent_cmd = "codex"
        config_check = ""
        config_write = ""
    else:  # claude-code
        agent_cmd = "claude"
        config_file = f"{project}/.claude.json"
        config_content = json.dumps({"mcp": mcp_config}, indent=2)
        config_check = f"if [ ! -f {project}/.claude.json ]; then\n"
        config_write = f"  cat > {project}/.claude.json << 'CLAUDE_EOF'\n{config_content}\nCLAUDE_EOF\n"

    # Write a launcher script that handles config creation + agent start
    script_file = f"/tmp/fleet-launch-{name}.sh"
    with open(script_file, "w") as f:
        f.write(f'#!/bin/bash\n')
        f.write(f'export PATH="$HOME/.opencode/bin:$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"\n')
        if config_check:
            f.write(config_check)
            f.write(config_write)
            f.write(f'fi\n')
        if agent == "codex" and auth_token:
            f.write(f'export FLEET_AUTH_TOKEN={shlex.quote(auth_token)}\n')
        f.write(f'sleep 1\n')
        f.write(f'cd {project} && FLEET_SESSION_ID={name} exec {agent_cmd} ')
        if agent == "copilot":
            # Use heredoc to avoid shell quoting issues
            f.write(f'-i "$(cat <<\'FLEET_PROMPT_EOF\'\n')
            f.write(fleet_prompt)
            f.write(f'\nFLEET_PROMPT_EOF\n')
            f.write(f')"\n')
        elif agent == "codex":
            f.write(f"-c {shlex.quote(f'mcp_servers.fleet-manager.url={json.dumps(mcp_url)}')} ")
            if auth_token:
                f.write("-c " + shlex.quote("mcp_servers.fleet-manager.bearer_token_env_var=\"FLEET_AUTH_TOKEN\"") + " ")
            f.write(f'"$(cat <<\'FLEET_PROMPT_EOF\'\n')
            f.write(fleet_prompt)
            f.write(f'\nFLEET_PROMPT_EOF\n')
            f.write(f')"\n')
        else:
            f.write(f'--prompt "$(cat <<\'FLEET_PROMPT_EOF\'\n')
            f.write(fleet_prompt)
            f.write(f'\nFLEET_PROMPT_EOF\n')
            f.write(f')"\n')
    os.chmod(script_file, 0o755)

    # Register session in DB
    session = db.create_session(name, tmux_name, "0", project, agent)

    # Wait for shell to be ready before sending keys
    time.sleep(1)

    # Launch via the script (avoids send-keys quoting issues)
    _tmux_sync("send-keys", "-t", f"={tmux_name}:0", f"bash {script_file}", "Enter")
    logger.info("Launched %s in session '%s'", agent, name)

    return session


async def fork_session(
    source_name: str,
    new_name: str,
    port: int = 7700,
) -> dict:
    """Fork an existing Claude Code session — creates a new fleet session that branches
    from the source session's Claude conversation history.

    Only Claude Code (claude-code) sessions can be forked.
    Requires the source session to have reported its claude_session_id.

    Raises LaunchError on validation failures.
    """
    source = db.get_session(source_name)
    if not source:
        raise LaunchError(f"Source session '{source_name}' not found")

    source_agent = source.get("agent", "claude-code")
    if source_agent != "claude-code":
        raise LaunchError(f"Only Claude Code sessions can be forked (source is {source_agent})")

    claude_sid = source.get("claude_session_id")
    if not claude_sid:
        raise LaunchError(
            f"Session '{source_name}' has no Claude session ID — "
            "it must report status at least once before it can be forked"
        )

    project = source.get("project_root")
    if not project:
        raise LaunchError(f"Session '{source_name}' has no project root")

    tmux_name = f"{TMUX_PREFIX}{new_name}"

    # Check for name collision
    if await session_exists(tmux_name):
        raise LaunchError(f"tmux session '{tmux_name}' already exists")
    if db.get_session(new_name):
        raise LaunchError(f"Session '{new_name}' already exists")

    # Create tmux session with configured dimensions
    cfg = get_config()
    result = _tmux_sync(
        "new-session", "-d", "-s", tmux_name, "-c", project,
        "-x", str(cfg.tmux.default_width), "-y", str(cfg.tmux.default_height),
    )
    if result.returncode != 0:
        raise LaunchError(f"Failed to create tmux session: {result.stderr.strip()}")
    _tmux_sync("set-option", "-t", f"={tmux_name}", "status-left", f" [{new_name}] ")
    _tmux_sync("set-option", "-t", f"={tmux_name}", "status-right", " Detach: Ctrl+B, D  %H:%M ")
    _tmux_sync("set-option", "-t", f"={tmux_name}", "status-style", "bg=#0969da,fg=#ffffff")
    _tmux_sync("set-option", "-t", f"={tmux_name}", "mouse", "on")
    _tmux_sync("set-option", "-t", f"={tmux_name}", "history-limit", "10000")
    _tmux_sync("select-pane", "-t", f"={tmux_name}:0", "-P", "bg=#1e1e2e,fg=#cdd6f4")

    # Build MCP URL, config, and fleet system prompt
    mcp_url = f"http://127.0.0.1:{port}/mcp/mcp"
    auth_token = cfg.server.auth_token
    mcp_config = {
        "fleet-manager": {
            "type": "remote",
            "url": mcp_url,
            "enabled": True
        }
    }
    if auth_token:
        mcp_config["fleet-manager"]["headers"] = {
            "Authorization": f"Bearer {auth_token}"
        }
    fleet_prompt = generate_prompt(new_name, mcp_url=mcp_url)

    # Write launcher script.
    # Creates .claude.json with MCP config, then runs claude with fork.
    import json
    script_file = f"/tmp/fleet-launch-{new_name}.sh"
    tmux_target = f"={TMUX_PREFIX}{new_name}:0"
    with open(script_file, "w") as f:
        f.write(f'#!/bin/bash\n')
        f.write(f'export PATH="$HOME/.opencode/bin:$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"\n')
        f.write(f'# Create .claude.json with MCP config only if not exists\n')
        f.write(f'if [ ! -f {project}/.claude.json ]; then\n')
        f.write(f'  cat > {project}/.claude.json << \'CLAUDE_EOF\'\n')
        f.write(json.dumps({"mcp": mcp_config}, indent=2))
        f.write(f'\nCLAUDE_EOF\n')
        f.write(f'fi\n')
        f.write(f'sleep 1\n')
        f.write(f'cd {project} && FLEET_SESSION_ID={new_name} exec claude --session {claude_sid} --fork \\\n')
        f.write(f'  --prompt "$(cat <<\'FLEET_PROMPT_EOF\'\n')
        f.write(fleet_prompt)
        f.write(f'\nFLEET_PROMPT_EOF\n')
        f.write(f')"\n')
    os.chmod(script_file, 0o755)

    # Register session in DB with claude-code agent
    session = db.create_session(new_name, tmux_name, "0", project, "claude-code")

    # Wait for shell to be ready before sending keys
    time.sleep(1)

    _tmux_sync("send-keys", "-t", f"={tmux_name}:0", f"bash {script_file}", "Enter")
    logger.info("Forked session '%s' from '%s' (claude_sid=%s)", new_name, source_name, claude_sid)

    return session


async def start_web_session(
    name: str,
    project: str,
    web_port: int | None = None,
) -> dict:
    """Start a managed project-scoped OpenCode web workspace."""
    if not os.path.isdir(project):
        raise LaunchError(f"Project path does not exist: {project}")
    if db.get_session(name):
        raise LaunchError(f"Session '{name}' already exists")

    port = web_port or _pick_free_port()
    log_file = _log_file_for_session(name)
    with open(log_file, "w") as log_handle:
        proc = subprocess.Popen(
            ["opencode", "serve", "--hostname", WEB_HOST, "--port", str(port)],
            cwd=project,
            env=_env_with_user_bins(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    upstream_url = f"http://{WEB_HOST}:{port}/"
    try:
        _wait_for_http(upstream_url)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            pass
        raise

    return db.create_session(
        name,
        "",
        project_root=project,
        agent="opencode",
        session_type="opencode_web",
        web_port=port,
        web_host=WEB_HOST,
        process_pid=proc.pid,
    )


async def restart_web_session(name: str) -> dict:
    session = db.get_session(name)
    if not session:
        raise LaunchError(f"Session '{name}' not found")
    if session.get("session_type") != "opencode_web":
        raise LaunchError(f"Session '{name}' is not an OpenCode web workspace")

    project = session.get("project_root")
    if not project:
        raise LaunchError(f"Session '{name}' has no project root")

    pid = session.get("process_pid")
    if pid:
        try:
            os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
            time.sleep(1)
        except OSError:
            pass

    port = session.get("web_port") or _pick_free_port()
    log_file = _log_file_for_session(name)
    with open(log_file, "w") as log_handle:
        proc = subprocess.Popen(
            ["opencode", "serve", "--hostname", WEB_HOST, "--port", str(port)],
            cwd=project,
            env=_env_with_user_bins(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    upstream_url = f"http://{WEB_HOST}:{port}/"
    _wait_for_http(upstream_url)
    return db.update_web_session(name, web_port=port, web_host=WEB_HOST, process_pid=proc.pid)


async def stop_session(name: str) -> bool:
    """Stop a fleet session — kill tmux, clean up prompt file, remove from DB.

    Returns True if the session was found and stopped.
    """
    session = db.get_session(name)
    if session and session.get("session_type") == "opencode_web":
        pid = session.get("process_pid")
        if pid:
            try:
                os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
            except OSError:
                pass
        log_file = _log_file_for_session(name)
        if os.path.exists(log_file):
            os.remove(log_file)
        db.delete_session(name)
        return True

    tmux_name = f"{TMUX_PREFIX}{name}"

    tmux_killed = await kill_session(tmux_name)

    # Clean up temp files
    for f in (f"/tmp/fleet-prompt-{name}.txt", f"/tmp/fleet-launch-{name}.sh"):
        if os.path.exists(f):
            os.remove(f)

    db.delete_session(name)
    return tmux_killed
