# Claude Code Web Manager

A web-based kanban board that orchestrates [Claude Code](https://docs.anthropic.com/en/docs/claude-code) tasks. Submit prompts through the UI, and the system executes them in isolated git worktrees with real-time progress via WebSocket. Useful for managing autonomous coding workflows on a personal dev server or cloud VM.

## Prerequisites

- **Python 3.11+**
- **Git**
- **[uv](https://docs.astral.sh/uv/)** (Python package manager)
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** (`claude` must be on your PATH)
- **A git repository** for the project you want Claude to work on

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/your-user/personal_coder.git
cd personal_coder
```

### 2. Install dependencies

```bash
cd claude-code-web-manager
uv sync --all-extras
```

### 3. Configure environment

Create a `.env` file in `claude-code-web-manager/`:

```bash
# Required — path to the git repo Claude will work in
BASE_REPO=/home/you/your-project

# Optional
DB_PATH=tasks.db                   # SQLite database location (default: tasks.db)
LOG_DIR=/home/you/task-logs        # Task log directory (default: /home/ubuntu/task-logs)
MAX_CONCURRENT=3                   # Max parallel tasks (default: 3)
API_KEY=                           # API key for auth; leave empty to disable
```

Make sure the `LOG_DIR` exists:

```bash
mkdir -p /home/you/task-logs
```

### 4. Start the server

```bash
cd claude-code-web-manager
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in your browser.

For development with auto-reload:

```bash
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

### 5. Run as a systemd service (optional)

Create `/etc/systemd/system/claude-manager.service`:

```ini
[Unit]
Description=Claude Code Web Manager
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/home/you/personal_coder/claude-code-web-manager
ExecStart=/home/you/.local/bin/uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000
Restart=on-failure
EnvironmentFile=/home/you/personal_coder/claude-code-web-manager/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now claude-manager
```

## Remote Access with Tailscale

[Tailscale](https://tailscale.com/) lets you securely access your server from anywhere without exposing ports to the public internet.

### Install Tailscale

```bash
# Ubuntu/Debian
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Follow the auth link to connect your machine to your tailnet.

### Option A: Access via Tailscale IP (private)

Once both your server and client device are on the same tailnet, access the manager at:

```
http://<server-tailscale-ip>:8000
```

Find your server's Tailscale IP with:

```bash
tailscale ip -4
```

### Option B: Tailscale Serve (HTTPS within your tailnet)

Proxy port 8000 behind Tailscale's built-in HTTPS:

```bash
tailscale serve --bg 8000
```

Access at `https://<your-machine-name>.<tailnet-name>.ts.net`. Only devices on your tailnet can reach it.

### Option C: Tailscale Funnel (public HTTPS)

Expose the manager publicly over the internet with a valid TLS certificate:

```bash
tailscale funnel --bg 8000
```

Access at `https://<your-machine-name>.<tailnet-name>.ts.net` from anywhere. Set `API_KEY` in your `.env` when using Funnel to protect the endpoint.

## Running Tests

```bash
cd claude-code-web-manager

# Unit + integration
uv run pytest tests/ -v --tb=short

# E2E (requires the server running on port 8000)
uv run uvicorn backend.main:app --port 8000 &
uv run pytest tests/e2e/ -v --tb=short
```

## License

[Boost Software License 1.0](LICENSE)
