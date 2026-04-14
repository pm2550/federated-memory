# Federated Memory Server

A self-hosted MCP (Model Context Protocol) server that gives AI coding assistants
persistent, cross-workspace memory. Each machine/tool/project gets its own
workspace; all workspaces are readable by any connected AI, but writes are
scoped -- creating a **federated** knowledge graph that grows with every session.

```
 Machine A         Machine B         Machine C
 Claude Code       Codex CLI         Cursor / ...
 (win / mac)       (linux)           (any OS)
     |                 |                 |
     | SessionEnd hook | cron scan       | MCP read
     | + manifest sync | + manifest sync |
     v                 v                 v
+---------------------------------------------------+
|              Memory Server (Go + MCP)              |
|                                                    |
|  /data/memory/                                     |
|    _meta/workspaces.json                           |
|    workspaces/                                     |
|      machineA-claude-projectX/                     |
|        identity.json                               |
|        overview.md                                 |
|        knowledge/*.md          (distilled)         |
|        sessions/YYYY/MM/*.jsonl.gz  (raw archives) |
|      machineB-codex-projectY/                      |
|      ...                                           |
+---------------------------------------------------+
```

## Features

- **12 MCP tools** for reading/writing knowledge and session archives
- **Dual-token auth**: AI clients get read + limited write; sync scripts get full upload
- **Per-workspace write isolation** with write_token validation
- **Session archiving**: verbatim JSONL transcripts, gzip-compressed, SHA256 deduped
- **Knowledge versioning**: `.versions/` directory preserves old versions on overwrite
- **Cross-workspace search**: ripgrep-powered search across knowledge and sessions
- **Client installer**: one-line setup for Claude Code, Codex CLI, and cron/schtasks
- **Manifest-based sync**: declarative file tracking with local state caching
- **Security hardening**: nginx TLS termination, fail2ban, rate limiting, path sandboxing

## Requirements

**Server:**
- Linux (Ubuntu 22.04+ recommended)
- Go 1.23+
- nginx (for TLS termination and rate limiting)
- 1 vCPU / 512 MB RAM minimum (text-only storage scales to hundreds of GB)

**Client:**
- Python 3.10+
- Claude Code and/or Codex CLI (optional, any MCP-compatible tool works)

## Quick Start

### 1. Build

```bash
# Native
make build

# Cross-compile for Linux server
make build-linux
```

### 2. Server Setup

```bash
# On your Linux server:
sudo bash deploy/install.sh

# Create environment file
sudo tee /opt/mcp-memory-server/.env << EOF
MCP_TOKEN=$(openssl rand -hex 32)
SYNC_TOKEN=$(openssl rand -hex 32)
MEMORY_ROOT=/data/memory
PORT=8000
EOF

# Start
sudo systemctl start mcp-memory
```

The install script sets up:
- systemd service with memory limits and security hardening
- nginx reverse proxy (HTTPS on 7443, HTTP on 7444)
- Self-signed TLS certificate (replace with Let's Encrypt for production)
- fail2ban to block brute-force auth attempts
- iptables rate limiting on new connections
- Daily backup cron (14-day retention)

### 3. Client Setup

```bash
# On each machine that should connect:
cd client/
bash install.sh <AI_TOKEN> --sync-token <SYNC_TOKEN> \
    --url http://YOUR_SERVER_IP:7444/mcp \
    --machine mymachine
```

This will:
1. Configure Claude Code and/or Codex CLI to use the memory MCP server
2. Install `memory_sync.py` to `~/.memory-sync/bin/`
3. Set up a Claude SessionEnd hook for automatic session uploads
4. Schedule periodic sync (manifests + Codex session scanning)

### 4. Verify

```bash
# Test the connection
python ~/.memory-sync/bin/memory_sync.py test

# Create a manifest for a workspace
python ~/.memory-sync/bin/memory_sync.py init-manifest \
    --tool claude-code --project myproject --cwd ~/projects/myproject

# Run a sync
python ~/.memory-sync/bin/memory_sync.py sync
```

## Architecture

### Security Model (4 layers)

| Layer | Mechanism | What it protects |
|-------|-----------|-----------------|
| L0 | Dual bearer tokens (AI / Sync) | Transport auth |
| L1 | Session binding (`register_workspace`) | AI writes scoped to own workspace |
| L2 | Per-workspace `write_token` | Sync uploads validated per-workspace |
| L3 | Path sandboxing (no `..`, no null bytes) | Filesystem traversal prevention |

**AI token** (`MCP_TOKEN`): Read everything + write own overview + delete own knowledge.

**Sync token** (`SYNC_TOKEN`): Upload knowledge and sessions with explicit workspace_id + write_token.

### MCP Tools

| Tool | Auth | Description |
|------|------|-------------|
| `get_overview` | Any | Entry point: global README + all workspaces with summaries |
| `list_workspaces` | Any | List registered workspaces with counts |
| `register_workspace` | Any | Bind session to a workspace (AI) or get write_token (Sync) |
| `list_knowledge` | Any | List knowledge md files in a workspace |
| `read_knowledge` | Any | Read a knowledge md file |
| `search_knowledge` | Any | Full-text search across knowledge files |
| `list_sessions` | Any | List session archives (newest first) |
| `read_session` | Any | Read a session archive with pagination |
| `search_sessions` | Any | Search across compressed session archives |
| `write_overview` | AI only | Write own workspace overview.md |
| `delete_knowledge` | AI only | Delete own workspace knowledge file |
| `upload_knowledge` | Sync only | Upload knowledge with write_token validation |
| `upload_session` | Sync only | Upload session archive with deduplication |

### Client Sync Flow

```
Claude SessionEnd hook
  -> memory_sync.py claude-hook
    -> upload session transcript
    -> sync all manifest-tracked knowledge files

Scheduled task (cron / schtasks)
  -> memory_sync.py sync
    -> sync manifest-tracked knowledge
    -> scan ~/.codex/sessions for new rollout files
    -> scan ~/.claude/projects for historical sessions
```

### Manifest Format

Each workspace has a manifest at `~/.memory-sync/manifests/<workspace_id>.json`:

```json
{
  "workspace_id": "mymachine-claude-code-myproject",
  "machine": "mymachine",
  "tool": "claude-code",
  "project": "myproject",
  "tracked": [
    {"local": "~/notes/design.md", "upload": "design.md"}
  ],
  "tracked_dirs": [
    {
      "local": "~/.claude/projects/my-project-slug/memory",
      "upload_prefix": "auto/",
      "glob": "*.md"
    }
  ]
}
```

## Configuration

### Server Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MCP_TOKEN` | Yes | - | Bearer token for AI clients |
| `SYNC_TOKEN` | No | - | Bearer token for sync scripts |
| `MEMORY_ROOT` | No | `/data/memory` | Root directory for all memory data |
| `PORT` | No | `8000` | HTTP listen port (behind nginx) |

### Client Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MCP_MEMORY_TOKEN` | Yes | - | Bearer token (set by install.sh) |
| `SYNC_TOKEN` | No | - | Sync script token (set by install.sh) |
| `MCP_MEMORY_URL` | No | config.json | Override server URL |
| `MEMORY_SYNC_HOME` | No | `~/.memory-sync` | Override config directory |

## Local Development

```bash
# Run locally with test data
make run
# Server at http://127.0.0.1:8000/mcp with token "dev-token"

# Run tests
make test
```

## File Structure

```
cmd/server/main.go              Entry point
internal/
  auth/middleware.go             Dual-token bearer auth
  config/config.go              Environment-based config
  federation/
    layout.go                   Path layout, slugging, sandboxing
    registry.go                 Workspace registry (file-backed)
    tools.go                    All 13 MCP tool handlers
  sandbox/sandbox.go            Path validation for raw tools
  tools/tools.go                Legacy raw filesystem tools
client/
  install.sh                    One-line client installer
  memory_sync.py                Sync daemon + Claude/Codex hooks
deploy/
  install.sh                    Server first-time setup
  deploy.sh                     Binary deploy + rollback
  nginx.conf                    Reverse proxy config
  mcp-memory.service            systemd unit
  backup.sh                     Daily backup script
  fail2ban-*.conf               Auth brute-force protection
scripts/                        E2E tests and utilities
server_content/                 Default INDEX.md and README.md
SPEC.md                         Detailed design specification
Makefile
```

## License

MIT
