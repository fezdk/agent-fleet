# Fleet Manager

Run and remotely control multiple AI agent sessions in parallel. Launch sessions across different projects, send instructions, answer questions, press terminal keys, and fork conversations — from anywhere: your phone, a laptop, or even another AI acting as an orchestrator.

Each agent session runs in a tmux pane, connects to the fleet manager via MCP for state reporting and question relay, and receives instructions through a REST API. A real-time web dashboard provides a visual interface for monitoring and interacting with the fleet, but the system is equally usable through the API alone.

## Architecture

```
Clients (Phone / Laptop / Orchestrator)
        │
        ▼
┌────────────────────────────────┐
│         The Service            │
│  REST API · WebSocket · Web UI │
│  MCP Server (HTTP) · tmux     │
└────┬──────────┬──────────┬─────┘
     │          │          │
   tmux:api   tmux:fe    tmux:ml
   agent     agent      agent
```

## Prerequisites

- **Python 3.11+**
- **tmux** — required for terminal session management
  ```bash
  # Ubuntu/Debian
  sudo apt install tmux
  # macOS
  brew install tmux
  ```
- **opencode**, **Claude Code**, **Copilot**, or **Codex** CLI installed and authenticated

## Installation

```bash
cd agent-fleet
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Starting the Server

The fleet manager server must be running before any sessions can connect.

```bash
cd agent-fleet
source .venv/bin/activate
python -m fleet_manager.server
```

To start it detached after a reboot, put `FLEET_AUTH_TOKEN` in `.env` and run:

```bash
scripts/start-server.sh
```

This starts the server on `http://127.0.0.1:7700` which hosts:

- **MCP Server** (Streamable HTTP at `/mcp/mcp`) — agent sessions connect here
- **REST API** (`/api/`) — sessions, questions, messages
- **WebSocket** (`/ws`) — live state updates to clients
- **Web UI** (`/`) — dashboard for monitoring and control

### OpenCode Workspaces

`opencode_web` sessions are project-scoped browser workspaces, not tmux-backed agent sessions.

Fleet can start `opencode serve` for a project, store its local port in the session database, and proxy the web UI through the main Fleet server. Workspaces appear alongside agent sessions in the dashboard with a **Web UI** badge and can be embedded in tab, side-tab, detail, and focus views.

Recommended setup:

- Main Fleet UI on `http://fleet.client.dk:7700`
- One workspace host per project, for example:
  - `http://eddiezone.fleet.client.dk:7700`
  - `http://mutti.fleet.client.dk:7700`

Using per-workspace hostnames isolates browser origin state cleanly and is more reliable than serving multiple OpenCode workspaces under one shared origin with different URL paths.

Workspace controls:

- **Reload** refreshes the embedded workspace frame without changing the process.
- **Restart** restarts a Fleet-managed `opencode serve` process on the stored port.
- **Open** launches the proxied workspace in a separate browser tab.
- **Remove Workspace** removes the session and stops the managed process when Fleet started it.

Environment overrides (set as env vars or in a `.env` file in the project root):
```bash
FLEET_PORT=7700
FLEET_HOST=127.0.0.1
FLEET_AUTH_TOKEN=your-secret-token
```

To gracefully restart the server (e.g. after a code update), send SIGHUP:
```bash
kill -HUP $(pgrep -f fleet_manager.server)
```

### Remote Access

The server binds to localhost by default. For remote access (e.g. from a laptop or phone), use an SSH tunnel:

```bash
# One-time tunnel (survives server restarts)
ssh -f -N -L 7700:127.0.0.1:7700 your-server

# Auto-reconnecting tunnel
autossh -M 0 -f -N -L 7700:127.0.0.1:7700 your-server
```

Then access the dashboard at `http://127.0.0.1:7700` on your local machine.

#### Sharing on the local network

By default the SSH tunnel only listens on `127.0.0.1`, so only the machine running the tunnel can connect. To make the dashboard available to other devices on the same network (phone, tablet, another laptop), bind the tunnel to all interfaces:

```bash
# Listen on 0.0.0.0 so any device on the LAN can reach port 7700
ssh -f -N -L 0.0.0.0:7700:127.0.0.1:7700 your-server

# Auto-reconnecting version
autossh -M 0 -f -N -L 0.0.0.0:7700:127.0.0.1:7700 your-server
```

Then access the dashboard from any device on the network at `http://<your-local-ip>:7700`. Find your local IP with:

```bash
hostname -I | awk '{print $1}'   # Linux
ipconfig getifaddr en0            # macOS
```

> **Note:** Your SSH server must allow this — ensure `GatewayPorts clientspecified` (or `yes`) is set in `/etc/ssh/sshd_config` on the machine running the tunnel. Also make sure `FLEET_AUTH_TOKEN` is set, since the dashboard will be exposed to the local network.

#### WireGuard VPN

Instead of SSH tunneling, you can use WireGuard to add both the server and clients (laptop, phone) to the same VPN network. This gives direct access without port forwarding:

```bash
# On the server, install WireGuard and set up a peer
sudo apt install wireguard-tools
wg-quick up wg0  # after configuring /etc/wireguard/wg0.conf

# Access from anywhere at http://<server-vpn-ip>:7700
```

This is simpler than SSH tunnels for multi-device access and works from anywhere on the internet.

## MCP Server Registration

Sessions started via `fleet start` or the web UI automatically register the MCP server with the agent — no manual setup needed.

If you want to manually connect an agent session to the fleet (outside of `fleet start`), register it yourself:

```bash
opencode mcp add --transport http --scope user fleet-manager http://127.0.0.1:7700/mcp/mcp

# If auth is enabled, include the token:
opencode mcp add --transport http --scope user fleet-manager http://127.0.0.1:7700/mcp/mcp \
  --header "Authorization: Bearer YOUR_TOKEN"
```

> **Note:** The fleet manager server must be running when the agent starts a session,
> otherwise the MCP connection will fail. The MCP transport uses stateless HTTP, so server
> restarts are transparent — no reconnection needed (see [MCP Connection Recovery](#mcp-connection-recovery)).

## Starting Sessions

### From the CLI

With the server running, open a new terminal and activate the venv:

```bash
cd agent-fleet
source .venv/bin/activate
```

Now `fleet` is available and can be run from any directory:

```bash
# Start a session (creates tmux + launches agent, then attaches)
fleet start --name api --project /path/to/api

# Start with a specific agent (default: opencode)
fleet start --name api --project /path/to/api --agent claude-code
fleet start --name ml --project /path/to/ml --agent copilot
fleet start --name docs --project /path/to/docs --agent codex

# Start multiple sessions in batch (detached mode)
fleet start --name api --project /path/to/api -d
fleet start --name frontend --project /path/to/frontend -d
fleet start --name ml --project /path/to/ml -d

# Then attach to any session
fleet attach api
```

### From the Web UI

Click **+ New Session** in the dashboard header. Select the agent (opencode, Claude Code, Copilot, or Codex), enter the project path (absolute path on the server), and optionally a session name (defaults to the directory name). The session launches detached — use the dashboard to monitor and interact.

For **OpenCode web UI** sessions, Fleet starts and proxies the workspace automatically by default. Project path is required; URL and port are only for advanced/manual attachment cases.

### Session Lifecycle

To detach from a session without stopping it, press **Ctrl+B, D** (the tmux status bar at the bottom reminds you of this). You can reattach later with `fleet attach <name>`.

Stopping a session (via `fleet stop <name>` or the **Stop** button in the web UI) kills the tmux session, terminates the agent, and removes the session from the database.

### Forking Sessions

Fork creates a new session that branches from an existing session's conversation history. The forked session starts with full context of what the original session was doing.

```bash
# Fork from CLI
fleet fork api api-refactor
fleet fork api api-refactor -d   # Forked session, detached

# Fork from the Web UI
# Click "Open" on a session, then click the "Fork" button in the focus modal header
```

Forking requires the source session to have reported its session ID (happens automatically on first `report_status` call). If the Fork button is not visible in the UI, the session hasn't reported yet.

### CLI Commands

```bash
fleet start --name <name> --project <path> [--agent opencode|claude-code|copilot|codex] # Start a session
fleet start --name <name> --project <path> -d # Start detached
fleet list                                    # List sessions
fleet attach <name>                           # Attach to tmux
fleet fork <name> <new-name>                  # Fork session (branch conversation)
fleet stop <name>                             # Stop session + cleanup
```

`fleet start` will:
1. Create a tmux session `fleet-<name>`
2. Register the MCP server with the agent (if not already registered)
3. Launch the agent with fleet instructions as the initial prompt
4. Attach to the session (unless `-d` is passed)

Supported agents: `opencode` (default), `claude-code`, `copilot`, `codex`. Only `claude-code` sessions can be forked.

Fleet instructions are injected as a system prompt at launch time — no modifications to the project's `CLAUDE.md` are needed.

### MCP Tools

Each agent session gets two tools:

| Tool | Purpose |
|---|---|
| `report_status` | Report state transitions (IDLE/WORKING/AWAITING_INPUT/ERROR) |
| `relay_question` | Mirror questions for remote clients before asking in terminal |

### MCP Connection Recovery

The MCP server uses stateless Streamable HTTP transport. Each request is self-contained with no session state, so server restarts are transparent to connected agent sessions — no reconnection or re-initialization needed.

If repeated MCP calls fail (e.g. server is down), sessions are instructed to:

1. Continue working normally without fleet tools
2. Retry once the server is back — stateless requests have no session to become stale
3. As a last resort, re-register via `opencode mcp remove/add`

### Web UI

Dashboard at `http://127.0.0.1:7700`:

- **View modes** — three switchable layouts, toggled via buttons in the header:
  - **List view** (default) — session cards with state indicators, pending question badges, and Open/detail buttons
  - **Tab view** — horizontal browser-style tabs above a full-height terminal with keys bar and message input; great for monitoring one session at a time
  - **Side-tab view** — left panel (280px) with session list showing name/state/summary, right side is terminal + controls; collapses to horizontal strip on mobile
- **Session filters** — filter the dashboard to all sessions, tmux-backed agents, or OpenCode web workspaces. Mobile uses a compact header toggle for the same filter.
- **Fleet settings** — click the gear icon to set a per-window browser title and default New Session type/agent. The title is stored in `sessionStorage` so different Fleet windows can be labeled independently; defaults are stored in `localStorage`.
- **Dark/light theme** — toggle via the sun/moon button in the header. Dark mode uses a Darcula-style palette; terminal stays dark in both modes. Theme preference persists in localStorage
- **Focus view** — click "Open" on a session card to get a large terminal output modal with auto-refresh (3s), an input bar for sending instructions, and a command dropdown for sending raw messages (slash commands, custom input without `[fleet]` prefix)
- **Multi view** — click "Multi View" in the header to see all session terminals side-by-side in a responsive grid, auto-refreshing. Click any pane to open its focus view
- **Session detail** — click the session name for the full detail page with questions, messages, terminal output, and status history
- **OpenCode web UI sessions** — open in tab view/workspace view rather than the agent detail page. Workspace controls such as reload/restart/open-in-tab live in the workspace toolbar.
- **Question modals** — when a session asks a question, a modal appears on top of the focus view for answering
- **New Session** — start sessions directly from the dashboard with project path autocomplete and auto-naming
- **File editor** — click "Edit" in the focus modal or tab/sidetab keys bar to open an in-browser file editor. Browse the session's project directory, open files in a monospace textarea, and save changes (Ctrl+S). Shows read/write permissions, rejects binary files and files over 500KB. Scoped to the session's project root for safety
- **Login** — when auth is enabled, a login prompt appears; token is stored in localStorage
- **Mobile responsive** — single-line sticky icon header, full-width focus/multi modals, compact keys bar, sidetab collapses to horizontal strip, dynamic viewport height (`dvh`) for correct sizing with mobile browser URL bars

View mode, theme, session filter, and New Session default preferences are persisted in localStorage and restored on page reload. The custom window title is stored per browser window.

### Orchestrator

An autonomous AI coordinator that monitors the fleet:

```bash
ANTHROPIC_API_KEY=sk-... fleet-orchestrator

# Options:
fleet-orchestrator --url http://127.0.0.1:7700 --model sonnet-4-20250514 --interval 10
```

The orchestrator:
- Auto-answers routine questions (yes/no confirmations, obvious defaults)
- Escalates complex decisions to the human
- Monitors fleet health
- Can dispatch tasks to idle sessions

### Notifications

Configure Telegram alerts for session events:

```bash
FLEET_TELEGRAM_TOKEN=bot123:ABC...
FLEET_TELEGRAM_CHAT_ID=12345678
```

Or in `config/default.json`:
```json
{
  "notifications": {
    "on_awaiting_input": true,
    "on_error": true,
    "on_task_complete": true,
    "on_stale": true,
    "telegram_token": "",
    "telegram_chat_id": ""
  }
}
```

## Authentication

Set `FLEET_AUTH_TOKEN` to protect access to the fleet manager's API, MCP, WebSocket, and Web UI. This is a local access control token for securing the fleet manager itself — it is not related to your AI provider API keys. Agent sessions use their own credentials independently.

```bash
FLEET_AUTH_TOKEN=your-secret-token python -m fleet_manager.server
```

When auth is enabled:
- The **Web UI** shows a login prompt on first visit. Enter the token to authenticate — it's stored in `localStorage` and persists across page reloads.
- **API requests** must include `Authorization: Bearer <token>` header.
- **MCP requests** require the same bearer token (sessions launched via `fleet start` are registered with the token automatically).
- **WebSocket** connections must include `?token=<token>` query parameter.
- A **logout** button appears in the header to clear the stored token.
- The `/api/auth/check` endpoint is publicly accessible and returns whether auth is enabled, so the UI can detect when login is needed.

When `FLEET_AUTH_TOKEN` is empty or unset, auth is disabled and everything works without a token. Recommended when exposing the server beyond localhost (e.g. via SSH tunnel to the local network).

## REST API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/sessions` | List all sessions |
| POST | `/api/sessions` | Register a session |
| POST | `/api/sessions/start` | Start a new session (creates tmux + launches agent). Body: `{"project": "...", "name": "", "agent": "opencode"}` |
| POST | `/api/sessions/web` | Register or start an OpenCode web workspace. Body: `{"project_root": "...", "name": "", "web_url": null, "web_port": null}` |
| GET | `/api/sessions/:id` | Session detail + status log |
| GET | `/api/sessions/:id/output` | Terminal output (via tmux) |
| POST | `/api/sessions/:id/restart` | Restart a Fleet-managed OpenCode web workspace |
| POST | `/api/sessions/:id/fork` | Fork session (branch conversation into new session) |
| DELETE | `/api/sessions/:id` | Stop + remove session (kills tmux or managed workspace process) |
| POST | `/api/sessions/:id/message` | Send instructions to session (`raw: true` to skip prefix) |
| POST | `/api/sessions/:id/keys` | Send raw keystrokes to the terminal (Enter, Escape, arrow keys, etc.) |
| POST | `/api/sessions/:id/unstick` | Emergency: send "wait" + Enter to wake a stuck session |
| POST | `/api/sessions/:id/remind` | Re-inject fleet instructions into a session that lost them |
| GET | `/api/filesystem/complete?path=...` | Directory completion for path input |
| GET | `/api/filesystem/list` | Browse directory within session's project root |
| GET | `/api/filesystem/read` | Read file content (500KB limit, UTF-8 only) |
| POST | `/api/filesystem/write` | Save file content (existing files only, atomic write) |
| GET | `/api/questions?pending=true` | Pending questions |
| GET | `/api/questions/:session_id` | Questions for a session |
| POST | `/api/questions/:id/answer` | Answer a question |
| GET | `/api/health` | Server health + stats |
| GET | `/api/auth/check` | Check if auth is required + validate token |
| GET | `/workspace/:session_id/...` | Proxy route for embedded OpenCode web workspaces |

## WebSocket Events

| Event | Direction | Data |
|---|---|---|
| `session:update` | server → client | Session object |
| `session:stale` | server → client | `{session_id, minutes_since_update}` |
| `question:new` | server → client | Question object |
| `question:answered` | server → client | Answered question object |
| `session:message` | server → client | Message delivery status |

## Security

- Bind to `127.0.0.1` by default (localhost only)
- Optional bearer token auth on all endpoints including MCP, API, WS, and Web UI (see [Authentication](#authentication))
- For remote access: use SSH tunnel, Tailscale, WireGuard, or Cloudflare Tunnel
- MCP endpoint requires the same bearer token as REST API when auth is enabled
- tmux exact session matching (`=` prefix) prevents cross-session interference

## Configuration

See `config/default.json` for all options. Key settings:

| Setting | Default | Description |
|---|---|---|
| `server.port` | 7700 | Server port |
| `server.host` | 127.0.0.1 | Bind address |
| `server.auth_token` | (empty) | Bearer token (disabled if empty) |
| `sessions.stale_timeout_minutes` | 15 | Mark sessions stale after N minutes |
| `sessions.message_prefix` | `[fleet]` | Prefix for remote instructions |
| `tmux.session_prefix` | `fleet-` | tmux session name prefix |

## Project Structure

```
fleet_manager/
  server.py            Main FastAPI app, background loops
  config.py            Configuration loader
  db.py                SQLite schema and CRUD
  mcp_server.py        MCP tools (report_status, relay_question)
  tmux_bridge.py       tmux capture/inject (exact session matching)
  ws_manager.py        WebSocket broadcast
  auth.py              Bearer token middleware
  session_launcher.py  Shared session start/stop logic (CLI + API)
  notifications.py     Telegram + notification rules
  orchestrator.py      AI orchestrator client
  prompt_template.py   Fleet system prompt generator
  cli.py               fleet CLI (thin wrapper around session_launcher)
  api/
    sessions.py        Session endpoints (incl. start/stop/fork)
    questions.py       Question endpoints
    messages.py        Message endpoints (raw mode support)
    filesystem.py      Path completion endpoint
web/
  index.html           Dashboard UI (modals: login, new session, focus, multi, question)
  app.js               Client-side JS
  style.css            Styles
config/
  default.json         Default configuration
tests/
  test_e2e.py          End-to-end tests
  test_mcp_client.py   MCP connectivity test
```
