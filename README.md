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
3. **Local** `call_send` delivers the text into the target Grok Bot agent through the host gateway (`POST /api/deliverAgentMessage`). That wakes the bot the same way Grok Bot agents wake each other. The text includes the session id and a one-line hint to reply with call-bridge MCP `call_send` (`from_party=member`). The result includes `delivery.status` (`delivered`, `target_not_found`, `not_member`, `unavailable`, or `error`). If `GROKBOT_GATEWAY_URL` or `GROKBOT_GATEWAY_TOKEN` is unset, or the gateway does not report `delivered`, the call fails and the message is not stored. **Member** `call_send` is unchanged (stored for the local side). `call_open`'s switchboard wake is unchanged.
4. Either side (or ops) calls `call_hangup`.

When a real MCP or REST request finds the directory unix socket unreachable, or the gateway forward down (connection failure, timeout, or gateway status `unavailable`), the server POSTs the same switchboard webhook once:

```json
{"event":"bridge.link_down","link":"directory","detail":"unix socket not found"}
```

`link` is `directory` or `gateway`. `detail` is a short error (`unix socket not found`, `timed out`, `request failed`, or `unavailable`). At most one of these wakes is sent per 60 seconds, in-process, with no background poll. `call_open`'s `session.opened` wake is unchanged. The caller is told the box link is down, the operator has been woken to reconnect, and to retry in about 30 seconds. `call_directory` still uses its existing fallback and adds that note. A local `call_send` that hits this returns the failure with that note and does not store the message. Ordinary gateway statuses (`target_not_found`, `not_member`, `empty`, HTTP 401) do not send this event.

`call_send` で `from_party="local"` のときは、相手エージェントへ直接届けてから保存する。保存される本文には、同じ `session_id` へ call-bridge MCP の `call_send`（`from_party=member`）で返答する案内が付く。案内は保存される本文と送信結果の両方に含まれる。配送が `delivered` のときだけ保存する。gateway の環境変数が無い、または配送が失敗したときはエラーを返し、保存しない。ディレクトリの UNIX ソケット、または gateway の転送が落ちているときは、同じスイッチボード webhook へ `bridge.link_down` を最大 60 秒に 1 回送り、呼び出し元には約 30 秒後の再試行を伝える。定期的な死活監視はしない。返信不要の通知だけ `reply_required=false` を指定する。`from_party="member"` の本文は変更せず、gateway にも送らない。MCP と REST のどちらでも同じ動作になる。返信依頼はメンバーへ送る指示であり、返答そのものを保証するものではない。

`call_send` の通常送信の引数例：

```json
{"session_id":"...","from_party":"local","message":"状況を教えてください"}
```

返信不要の通知の引数例：

```json
{"session_id":"...","from_party":"local","message":"共有のみです","reply_required":false}
```

REST の `POST /v0/sessions/{session_id}/messages` でも本文に
`{"from_party":"local","message":"共有のみです","reply_required":false}` を渡せる。

### Codex 親への返信自動配送

Codex から通話する端末では、ローカル MCP を登録すると `call_open` が親タスクを識別する。
ローカル MCP が通話の返信を裏で取得し、Codex の公式キューへ一通ずつ渡す。
親AI自身が `call_poll` を繰り返す必要はない。親のターンが進行中なら
`PostToolUse`／`Stop` hook が返信を同じターンへ差し込み、ターン終了後なら
キューが次のターンとして届ける。GrokBot メンバーは従来どおり公開 MCP に接続し、
返信には `from_party="member"` を使う。

対象は通常の Codex 親タスク。native sub-agent への自動配送は未対応。
有効化する端末には、まず `call-bridge` または `grokbot-bridge` を HTTP MCP として登録する（下の Codex の登録例を参照）。
`call-bridge-setup enable` を実行するシェルで、その登録の Bearer token 環境変数を利用可能にしておく。

```bash
python -m pip install git+https://github.com/kitepon/grokbot-bridge.git
call-bridge-setup enable
# Codex を完全終了して再起動
call-bridge-setup status
```

`enable` は既存の URL と token 環境変数名を読み、その MCP 登録をローカル MCP に切り替える。
認証値は製品の state directory（既定は `~/.grokbot-bridge`）の `auth.json` に本人だけが読める権限で保存し、Codex が環境変数を継承しない場合もローカル MCP が使用する。Git や Codex 設定には書かない。`disable` はそのファイルを削除する。
また、本製品専用の Codex hook を登録・承認する。他製品の hook は保持する。
設定変更前の `hooks.json` と `config.toml` は製品の state directory に tar で保存する。
元の HTTP MCP へ戻すときは `call-bridge-setup disable` を実行して Codex を再起動する。

`BRIDGE_TOKEN_MISSING` が出た場合は、既存の HTTP MCP 登録に指定した環境変数を
`enable` を実行するシェルへ渡し、`call-bridge-setup enable` を再実行する。
シェルに値があっても、起動済みの Codex MCP プロセスがその値を継承するとは限らない。
`enable` 後は Codex を完全終了して再起動する。`status` は登録と hook の状態を確認する。

返信は `session_id` と `seq` で順番に処理する。配送結果はローカル MCP の `call_info` に
`parent_delivery` として表示する。送信結果が不明なときは自動再送せず `unknown` と記録する。
返信本文は bridge に残り、手動で `call_poll` から確認できる。
ローカル MCP の再起動後は、記録された進行中の通話の受信を再開する。

現在の自動配送対象は Codex 親。Claude Code／Cursor の直接 HTTP 接続と手動 `call_poll` は従来どおり使える。

```text
Local agent ──call_open──▶ Grok Bot switchboard (wake only, unchanged)
Local agent ──call_send──▶ grokbot-bridge ──deliverAgentMessage──▶ Grok Bot member
Grok Bot member ──call_send──▶ grokbot-bridge (stored for the local side)
```

## MCP tools

| Tool | Role |
|------|------|
| `call_directory` | Phone book, built on that call from live seat profiles |
| `call_open` | Create session (local → member) |
| `call_send` | Send a message. Local sends wake the bot via the gateway (`delivery.status`); notices use `reply_required=false` |
| `call_poll` | Fetch new messages for your party |
| `call_list` | List / filter sessions |
| `call_hangup` | End the call |
| `call_info` | Session details |

Also exposes a small REST surface under `/v0` (same auth) and open `/health`.

## Phone directory

Clients only call `call_directory` (or `GET /v0/directory`). The server builds the book **on that request** from Grok Bot seat profiles. Changing a seat’s name, title, or description shows up on the **next** call. There is no periodic sync, no Marian routine, and no operator push after a role edit.

Source of truth is each seat’s **profile** (`name`, `title`, `description`) — used as-is (e.g. ラピ → title `インフラ統括`, `description` → `role`). There is **no** “may call” flag. Each member built from a profile also includes `id`: the seat directory name, which is the Grok Bot agent id (`profile.json` itself has no id field). Local `call_send` resolves `member_name` to that id and sends it as `toAgentId`. A remote directory passes `id` or `agentId` through. A `directory.json` entry without an id cannot wake the bot.

Lookup order:

1. **`CALL_BRIDGE_DIRECTORY_UNIX`** (preferred in production). HTTP GET over an `AF_UNIX` socket. The HTTP path is `CALL_BRIDGE_DIRECTORY_UNIX_PATH` (default `/v0/directory`). On main-server the container mounts the socket's parent directory at `/run/dirlive`, and the socket path is `/run/dirlive/dirlive.sock`.
2. **`CALL_BRIDGE_DIRECTORY_URL`** — plain HTTP GET, only if the unix socket is unset or that GET fails.
3. **Local profiles**, only after every configured remote GET has failed (or none is set): `CALL_BRIDGE_AGENTS_ROOT` if set and that directory exists, otherwise `/home/box/agent-data/agents` when the env var is unset and the path exists. This is for running call-bridge on the Grok Bot box itself. A copied agents tree on main-server is not the primary path.
4. **`directory.json`** — last-resort snapshot when the remotes failed and no local profile tree is available.

A live read reports `source: agent-profiles` and `agents_root`. A unix read also includes `directory_unix`; a URL read includes `directory_url`. The snapshot reports `source: directory.json` and does not claim to be a live profile read. If a remote was configured and failed before the snapshot was used, the response includes `directory_unix_error` and/or `directory_url_error`.

Prod wiring (operated outside this repo): the Grok Bot box runs `scripts/directory_live_server.py` on `127.0.0.1:18765`. SSH reverse-forwards that port to main-server (`ssh -R 127.0.0.1:18765:127.0.0.1:18765`). A host `socat` listens on a unix socket and dials `127.0.0.1:18765`. Compose mounts that socket's parent directory (`./dirlive:/run/dirlive`) so a recreated socket is visible without recreating the container. Set `CALL_BRIDGE_DIRECTORY_UNIX=/run/dirlive/dirlive.sock`. If `CALL_BRIDGE_DIRECTORY_UNIX_HOST` is set, point it at the directory (the default is `./dirlive`), not at the socket file.

```bash
# On the Grok Bot box (profiles live here):
python scripts/directory_live_server.py
# {"ok": true, "listen": "http://127.0.0.1:18765/v0/directory", ...}

# MCP clients — this is the whole interface
call_directory()
call_directory(query="インフラ")

# REST
curl -sS -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:18910/v0/directory?q=ラピ"
```

`scripts/sync_directory_from_agents.py` only writes an optional `directory.json` fallback. It is not how the book stays fresh, and nothing needs to run it after a profile edit.

## Quick start

```bash
cp .env.example .env
# set CALL_BRIDGE_TOKEN to a long random string

docker compose up -d --build
curl -sS http://127.0.0.1:18910/health
```

MCP endpoint: `http://127.0.0.1:18910/mcp`  
Auth: `Authorization: Bearer <CALL_BRIDGE_TOKEN>` on `/mcp` and `/v0/*` (`/health` is open). Without a token, `/mcp` and `/v0/*` are also open; use this only for local development.

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
| `CALL_BRIDGE_TOKEN` | _(unset)_ | Bearer token. Set it for production; if unset, MCP and REST are open for local development |
| `CALL_BRIDGE_HOST` | `0.0.0.0` | Bind host |
| `CALL_BRIDGE_PORT` | `18910` | Bind port |
| `CALL_BRIDGE_DB` | `data/calls.db` | SQLite path |
| `CALL_BRIDGE_ALLOWED_HOSTS` | `127.0.0.1:*,localhost:*` | Host header allowlist |
| `CALL_BRIDGE_ALLOWED_ORIGINS` | `http://127.0.0.1:*,http://localhost:*` | Origin allowlist |
| `CALL_BRIDGE_DIRECTORY_UNIX` | _(unset)_ | On each `call_directory`, HTTP GET over this `AF_UNIX` socket. Preferred prod source (`/run/dirlive/dirlive.sock`) |
| `CALL_BRIDGE_DIRECTORY_UNIX_PATH` | `/v0/directory` | HTTP path on that socket |
| `CALL_BRIDGE_DIRECTORY_URL` | _(unset)_ | HTTP GET used when the unix socket is unset or fails |
| `CALL_BRIDGE_DIRECTORY_URL_AUTH` | _(unset)_ | Bearer token sent on the unix and URL GETs (raw token or `Bearer …`) |
| `CALL_BRIDGE_DIRECTORY_URL_TIMEOUT` | `2.5` | Seconds for each remote GET |
| `CALL_BRIDGE_AGENTS_ROOT` | `/home/box/agent-data/agents` if that directory exists and the env var is unset | Local `profile.json` tree, used only when configured remotes fail (or none are set). When set, only that path is used |
| `CALL_BRIDGE_DIRECTORY` | `./directory.json` | Last-resort snapshot file |
| `CALL_BRIDGE_WAKE_WEBHOOK_URL` | _(unset)_ | Switchboard webhook for `session.opened` and, at most once per 60 seconds, `bridge.link_down`. Empty skips both POSTs |
| `CALL_BRIDGE_WAKE_WEBHOOK_AUTH` | _(unset)_ | `Authorization` header value for that POST |
| `CALL_BRIDGE_PUBLIC_MCP_URL` | `https://call.kitepon.dev/mcp` | MCP URL included in the wake envelope |
| `GROKBOT_GATEWAY_URL` | _(unset)_ | Grok Bot gateway origin, no path (for example `http://host.docker.internal:18766`). Local `call_send` POSTs `/api/deliverAgentMessage` and does not send an `Origin` header |
| `GROKBOT_GATEWAY_TOKEN` | _(unset)_ | Bearer token for that POST. Never logged. If either gateway variable is unset, local `call_send` fails instead of only storing |

Compose already sets `extra_hosts: ["host.docker.internal:host-gateway"]` so that hostname resolves inside the container. Recreate the container after changing `.env`. On the Grok Bot box, restart `scripts/directory_live_server.py` from this revision so live directory members include `id`.

`CALL_BRIDGE_SWITCHBOARD_WEBHOOK_URL` and `CALL_BRIDGE_SWITCHBOARD_WEBHOOK_AUTH` are aliases for the wake URL and auth value.

## Stack

- Python 3.12 in Docker (package requires Python 3.11+) / FastMCP streamable HTTP (`mcp>=1.2,<2`)
- SQLite for sessions + messages
- Docker Compose for deploy

## License

MIT © kitepon.dev
