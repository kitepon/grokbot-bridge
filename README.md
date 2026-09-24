# grokbot-bridge

Shared **phone-call bridge** MCP for **[Grok Bot](https://grok.x.ai/)** agent meshes (streamable HTTP).

A local coding agent (Claude Code, Codex, Cursor, …) asks a Grok Bot “switchboard” agent to **wake** a teammate, then both sides talk **direct** through this MCP — the switchboard does **not** relay message bodies.

## Why

- Local agents often cannot message Grok Bot members over the host’s agent bus.
- Putting a middleman bot in the conversation path is noisy and wrong.
- One shared endpoint + a session id is enough: **ring → talk → hang up**.

## Flow

1. **Local** opens a session (`call_open`) → gets `session_id` (`ringing`).
2. **Grok Bot switchboard** wakes the member with MCP URL + `session_id` only (no body relay).
3. **Local** and **Grok Bot member** use `call_send` / `call_poll` on the same server (`from_party` / `party` = `local` | `member`).
4. Either side (or ops) calls `call_hangup`.

```text
Local agent ──wake──▶ Grok Bot switchboard ──wake──▶ Grok Bot member
     │                                                      │
     └──────────── grokbot-bridge MCP (send/poll) ──────────┘
```

## MCP tools

| Tool | Role |
|------|------|
| `call_open` | Create session (local → member) |
| `call_send` | Send a message |
| `call_poll` | Fetch new messages for your party |
| `call_list` | List / filter sessions |
| `call_hangup` | End the call |
| `call_info` | Session details |

Also exposes a small REST surface under `/v0` (same auth) and open `/health`.

## Quick start

```bash
cp .env.example .env
# set CALL_BRIDGE_TOKEN to a long random string

docker compose up -d --build
curl -sS http://127.0.0.1:18910/health
```

MCP endpoint: `http://127.0.0.1:18910/mcp`  
Auth: `Authorization: Bearer <CALL_BRIDGE_TOKEN>` on `/mcp` and `/v0/*` (`/health` is open).

### Client examples

**Cursor** (`~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "grokbot-bridge": {
      "url": "https://your-host.example/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      }
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add --transport http grokbot-bridge https://your-host.example/mcp \
  --header "Authorization: Bearer YOUR_TOKEN"
```

**Codex**:

```bash
export CALL_BRIDGE_TOKEN=YOUR_TOKEN
codex mcp add grokbot-bridge --url https://your-host.example/mcp \
  --bearer-token-env-var CALL_BRIDGE_TOKEN
```

**Grok Bot**: add the remote MCP URL with the same Bearer header in the account connectors.

Put a reverse proxy (Caddy, nginx, Cloudflare Tunnel, …) in front for HTTPS.

## Config

| Env | Default | Meaning |
|-----|---------|---------|
| `CALL_BRIDGE_TOKEN` | _(required)_ | Bearer token |
| `CALL_BRIDGE_HOST` | `0.0.0.0` | Bind host |
| `CALL_BRIDGE_PORT` | `18910` | Bind port |
| `CALL_BRIDGE_DB` | `data/calls.db` | SQLite path |
| `CALL_BRIDGE_ALLOWED_HOSTS` | `127.0.0.1:*,localhost:*` | Host header allowlist |
| `CALL_BRIDGE_ALLOWED_ORIGINS` | `http://127.0.0.1:*,http://localhost:*` | Origin allowlist |

## Stack

- Python 3.12 / FastMCP streamable HTTP (`mcp>=1.2,<2`)
- SQLite for sessions + messages
- Docker Compose for deploy

## License

MIT © kitepon.dev
