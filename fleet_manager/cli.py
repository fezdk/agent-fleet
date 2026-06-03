"""CLI wrapper for managing fleet sessions."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from fleet_manager.db import init_db
from fleet_manager.session_launcher import LaunchError, start_session, stop_session, fork_session


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def _fleet_api(port: int, method: str, path: str) -> dict | list | None:
    """Quick helper to call the fleet manager API. Returns None on failure."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", method=method,
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def cmd_start(args: argparse.Namespace) -> None:
    name = args.name
    project = str(Path(args.project).resolve())
    agent = args.agent
    port = args.port

    try:
        session = asyncio.run(start_session(name, project, agent, port))
    except LaunchError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print(f"Started session '{name}'")
    print(f"\n  Project:  {project}")
    print(f"  Agent:    {agent}")
    print(f"  tmux:     fleet-{name}")
    print(f"  Web UI:   http://127.0.0.1:{port}")

    if args.detach:
        print(f"  Attach:   fleet attach {name}")
    else:
        print(f"\nAttaching to session (detach with Ctrl+B, D)...")
        os.execvp("tmux", ["tmux", "attach-session", "-t", f"fleet-{name}"])


def cmd_list(args: argparse.Namespace) -> None:
    result = _tmux("list-sessions", "-F", "#{session_name}")
    if result.returncode != 0:
        print("No tmux sessions running.")
        return

    prefix = "fleet-"
    found = False
    for line in result.stdout.strip().split("\n"):
        if line.startswith(prefix):
            name = line[len(prefix):]
            cwd_result = _tmux("display-message", "-t", line, "-p", "#{pane_current_path}")
            cwd = cwd_result.stdout.strip() if cwd_result.returncode == 0 else "?"
            print(f"  {name:20s} {cwd}")
            found = True
    if not found:
        print("No fleet sessions running.")


def cmd_attach(args: argparse.Namespace) -> None:
    tmux_name = f"fleet-{args.name}"
    result = _tmux("has-session", "-t", tmux_name)
    if result.returncode != 0:
        print(f"Session '{args.name}' not found.")
        sys.exit(1)
    os.execvp("tmux", ["tmux", "attach-session", "-t", tmux_name])


def cmd_fork(args: argparse.Namespace) -> None:
    port = args.port
    try:
        session = asyncio.run(fork_session(args.name, args.new_name, port))
    except LaunchError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print(f"Forked session '{args.name}' -> '{args.new_name}'")
    print(f"\n  Project:  {session.get('project_root', '?')}")
    print(f"  tmux:     fleet-{args.new_name}")

    if args.detach:
        print(f"  Attach:   fleet attach {args.new_name}")
    else:
        print(f"\nAttaching to session (detach with Ctrl+B, D)...")
        os.execvp("tmux", ["tmux", "attach-session", "-t", f"fleet-{args.new_name}"])


def cmd_stop(args: argparse.Namespace) -> None:
    stopped = asyncio.run(stop_session(args.name))
    if stopped:
        print(f"Stopped session '{args.name}'")
    else:
        print(f"Session '{args.name}' not found (tmux session not running)")


def main() -> None:
    init_db()
    parser = argparse.ArgumentParser(prog="fleet", description="Agent Fleet Manager CLI")
    sub = parser.add_subparsers(dest="command")

    start_p = sub.add_parser("start", help="Start a new fleet-managed agent session")
    start_p.add_argument("--name", required=True, help="Session name")
    start_p.add_argument("--project", default=".", help="Project directory")
    start_p.add_argument("--agent", default="opencode", choices=["opencode", "claude-code", "copilot", "codex"],
                         help="Agent to launch (default: opencode)")
    start_p.add_argument("--port", type=int, default=7700, help="Fleet manager port")
    start_p.add_argument("-d", "--detach", action="store_true",
                         help="Don't attach to the session (useful for batch-starting multiple sessions)")

    sub.add_parser("list", help="List fleet sessions")

    attach_p = sub.add_parser("attach", help="Attach to a fleet session")
    attach_p.add_argument("name", help="Session name")

    fork_p = sub.add_parser("fork", help="Fork an existing session (branch conversation)")
    fork_p.add_argument("name", help="Source session name")
    fork_p.add_argument("new_name", help="New session name")
    fork_p.add_argument("--port", type=int, default=7700, help="Fleet manager port")
    fork_p.add_argument("-d", "--detach", action="store_true", help="Don't attach to the new session")

    stop_p = sub.add_parser("stop", help="Stop a fleet session")
    stop_p.add_argument("name", help="Session name")

    args = parser.parse_args()
    if args.command == "start":
        cmd_start(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "attach":
        cmd_attach(args)
    elif args.command == "fork":
        cmd_fork(args)
    elif args.command == "stop":
        cmd_stop(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
