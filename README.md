# grokbot-bridge

Shared **phone-call bridge** MCP for **[Grok Bot](https://grok.x.ai/)** agent meshes (streamable HTTP).

A local coding agent (Claude Code, Codex, Cursor, …) asks a Grok Bot “switchboard” agent to **wake** a teammate, then both sides talk **direct** through this MCP — the switchboard does **not** relay message bodies.

## Why

- Local agents often cannot message Grok Bot members over the host’s agent bus.
- Putting a middleman bot in the conversation path is noisy and wrong.
- One shared endpoint + a session id is enough: **ring → talk → hang up**.

## Flow

1. **Local** opens a session (`call_open` or `POST /v0/sessions`) → gets `session_id` (`ringing`). The server POSTs a wake envelope to the switchboard webhook (when `CALL_BRIDGE_WAKE_WEBHOOK_URL` is set) so the switchboard can wake the member. The body is session id, member name, local labels, purpose, and the public MCP URL — not message bodies. If the URL is unset, or the POST fails, the session is still returned; the response includes a non-fatal `wake` object (`status`: `ok`, `skipped`, or `error`).
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
| `call_directory` | Phone book, built on that call from live seat profiles |
| `call_open` | Create session (local → member) |
| `call_send` | Send a message |
| `call_poll` | Fetch new messages for your party |
| `call_list` | List / filter sessions |
| `call_hangup` | End the call |
| `call_info` | Session details |

Also exposes a small REST surface under `/v0` (same auth) and open `/health`.

## Phone directory

Clients only call `call_directory` (or `GET /v0/directory`). The server builds the book **on that request** from Grok Bot seat profiles. A profile edit is visible on the **next** call. There is no periodic sync and no post-edit push.

Source of truth is each seat’s **profile** (`name`, `title`, `description`) — used as-is (e.g. ラピ → title `インフラ統括`, `description` → `role`). There is **no** “may call” flag.

Lookup order:

1. **`CALL_BRIDGE_DIRECTORY_URL`** (preferred in production). call-bridge runs on main-server; the live `profile.json` files stay on the Grok Bot box. On the box, `scripts/directory_live_server.py` serves `GET /v0/directory` from `/home/box/agent-data/agents` (or `CALL_BRIDGE_AGENTS_ROOT`) on every request, bound to `127.0.0.1`. A reverse SSH tunnel makes that port reachable from the container, for example `http://172.17.0.1:18911/v0/directory` or `http://host.docker.internal:18911/v0/directory` (compose maps `host-gateway`). The tunnel must listen on an address the container can route to. A copied `agents/` tree on main-server is not the primary path — it goes stale.
2. **Local profiles**, when this process is on the box: `CALL_BRIDGE_AGENTS_ROOT` if set and that directory exists, otherwise `/home/box/agent-data/agents` when the env var is unset and the path exists.
3. **`directory.json`** — last-resort snapshot only, when the URL is unset or unreachable and no local profile tree is available.

A live read reports `source: agent-profiles` and `agents_root` (URL reads also include `directory_url`). The snapshot reports `source: directory.json` and does not claim to be a live profile read, even if the file was generated from profiles.

If the URL is set but the GET fails, the server falls through to the local tree and then the snapshot so the call still returns a book. The snapshot response includes `directory_url_error` in that case.

```bash
# On the Grok Bot box (profiles live here):
python scripts/directory_live_server.py
# {"ok": true, "listen": "http://127.0.0.1:18911/v0/directory", ...}

# MCP clients — this is the whole interface
call_directory()
call_directory(query="インフラ")

# REST
curl -sS -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:18910/v0/directory?q=ラピ"
```

`scripts/sync_directory_from_agents.py` only writes an optional `directory.json` fallback for hosts that cannot reach profiles. It is not how the book stays fresh.

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
| `CALL_BRIDGE_DIRECTORY_URL` | _(unset)_ | On each `call_directory`, GET this URL (live profiles). Preferred prod source |
| `CALL_BRIDGE_DIRECTORY_URL_AUTH` | _(unset)_ | Bearer token for that GET (raw token or `Bearer …`) |
| `CALL_BRIDGE_DIRECTORY_URL_TIMEOUT` | `2.5` | Seconds for that GET |
| `CALL_BRIDGE_AGENTS_ROOT` | `/home/box/agent-data/agents` if that directory exists and the env var is unset | Local `profile.json` tree. Used when the URL is unset or unreachable. When set, only that path is used |
| `CALL_BRIDGE_DIRECTORY` | `./directory.json` | Last-resort snapshot file |
| `CALL_BRIDGE_WAKE_WEBHOOK_URL` | _(unset)_ | Switchboard wake webhook. Empty skips the POST |
| `CALL_BRIDGE_WAKE_WEBHOOK_AUTH` | _(unset)_ | `Authorization` header value for that POST |
| `CALL_BRIDGE_PUBLIC_MCP_URL` | `https://call.kitepon.dev/mcp` | MCP URL included in the wake envelope |

`CALL_BRIDGE_SWITCHBOARD_WEBHOOK_URL` and `CALL_BRIDGE_SWITCHBOARD_WEBHOOK_AUTH` are aliases for the wake URL and auth value.

## Stack

- Python 3.12 / FastMCP streamable HTTP (`mcp>=1.2,<2`)
- SQLite for sessions + messages
- Docker Compose for deploy

## License

MIT © kitepon.dev
