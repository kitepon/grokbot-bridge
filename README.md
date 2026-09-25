# grokbot-bridge

Shared **phone-call bridge** MCP for **[Grok Bot](https://grok.x.ai/)** agent meshes (streamable HTTP).

A local coding agent (Claude Code, Codex, Cursor, …) asks Marian (the Grok Bot switchboard) to **wake** a teammate. A local `call_send` posts `session.message` — including the text — to Marian's webhook so she can relay it into the member's main chat. `session.opened` still has no message body. Member replies stay on this MCP.

## Why

- Local agents cannot deliver into a member's main chat through the host gateway (`deliverAgentMessage` lands in a box-local New Agent conversation).
- Local `call_send` wakes Marian with the text. She relays it into that main chat. Member replies still come back through this MCP.
- One shared endpoint + a session id is enough: **ring → relay → reply → hang up**.

## Flow

1. **Local** opens a session (`call_open` or `POST /v0/sessions`) → gets `session_id` (`ringing`). The server POSTs a wake envelope to the switchboard webhook (when `CALL_BRIDGE_WAKE_WEBHOOK_URL` is set) so the switchboard can wake the member. The body is session id, member name, local labels, purpose, and the public MCP URL — not message bodies. If the URL is unset, or the POST fails, the session is still returned; the response includes a non-fatal `wake` object (`status`: `ok`, `skipped`, or `error`).
2. **Grok Bot switchboard** wakes the member with MCP URL + `session_id` only (no body relay).
3. **Local** `call_send` POSTs `session.message` to the same switchboard webhook (`CALL_BRIDGE_WAKE_WEBHOOK_URL` / `CALL_BRIDGE_WAKE_WEBHOOK_AUTH`). That envelope includes the caller's text, `reply_required`, and the resolved `member_agent_id`, so Marian can relay it into the member's main chat. `delivery.status` is `delivered` when the webhook returns HTTP 2xx, and the message is stored only then. If the webhook URL is unset, or the POST fails, the call fails and the message is not stored. The stored copy still appends the reply hint when `reply_required` is true; the webhook `message` field is the caller's text. **Member** `call_send` is unchanged (stored for the local side, no webhook). `session.opened` still has no message body. `GROKBOT_GATEWAY_*` is not used.
4. Either side (or ops) calls `call_hangup`.

When a real MCP or REST request finds the directory unix socket unreachable (`unix socket not found`, `timed out`, or `request failed` — the socket never produced an HTTP response), the server POSTs the same switchboard webhook once:

```json
{"event":"bridge.link_down","link":"directory","detail":"unix socket not found"}
```

`link` is `directory`. At most one of these wakes is sent per 60 seconds, in-process, with no background poll. `call_open`'s `session.opened` wake is unchanged and still has no message body. `call_directory` still uses its existing fallback and adds a note that the box link is down, the operator has been woken to reconnect, and to retry in about 30 seconds. A local `call_send` that cannot resolve the member because of that failure returns the error with the same note and does not store. If the directory falls back to local profiles and the member resolves, `call_send` still posts `session.message` after the `bridge.link_down` wake. A failed `session.message` POST is not a down box link and does not send `bridge.link_down`. `GROKBOT_GATEWAY_*` is not used, so a gateway forward is no longer a link-down source.

`call_send` で `from_party="local"` のときは、同じスイッチボード webhook へ `session.message`（呼び出し側の本文、`member_agent_id`、`reply_required`）を送ってから保存する。保存される本文には、同じ `session_id` へ call-bridge MCP の `call_send`（`from_party=member`）で返答する案内が付く。webhook の `message` はその案内を含まない。配送が `delivered`（webhook が HTTP 2xx）のときだけ保存する。webhook URL が無い、または POST が失敗したときはエラーを返し、保存しない。`GROKBOT_GATEWAY_*` は使わない。ディレクトリの UNIX ソケットが落ちているときは、同じ webhook へ `bridge.link_down` を最大 60 秒に 1 回送る。定期的な死活監視はしない。返信不要の通知だけ `reply_required=false` を指定する。`from_party="member"` の本文は変更せず、webhook にも送らない。MCP と REST のどちらでも同じ動作になる。返信依頼はメンバーへ送る指示であり、返答そのものを保証するものではない。

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
`enable` は導入前から動く Codex の PID と生成時刻を記録し、そのプロセスが残る間は
`restart_required` を返す。`call_open` も同じ親プロセスへの配送を拒否する。
完全終了・再起動後に `call-bridge-setup status` の `ready` を確認する。

返信は `session_id` と `seq` で順番に処理する。配送結果はローカル MCP の `call_info` に
`parent_delivery` として表示する。送信結果が不明なときは自動再送せず `unknown` と記録する。
`submitted` は公式キューの受付を示し、親 AI の読了を示さない。hook がキューから
取り出し中なら `sending`、取り出しの中断や出力失敗なら `unknown` と
`CODEX_HOOK_DELIVERY_UNCONFIRMED` を表示する。hook が受け取らず待機中の Codex が
先に処理した入力の所有記録は、次の hook 実行時に整理する。
返信本文は bridge に残り、手動で `call_poll` から確認できる。
ローカル MCP の再起動後は、記録された進行中の通話の受信を再開する。

現在の自動配送対象は Codex 親。Claude Code／Cursor の直接 HTTP 接続と手動 `call_poll` は従来どおり使える。

```text
Local agent ──call_open──▶ switchboard webhook session.opened (no message body)
Local agent ──call_send──▶ grokbot-bridge ──session.message──▶ Marian (relay into the member's main chat)
Grok Bot member ──call_send──▶ grokbot-bridge (stored for the local side)
```

`session.message` uses schema `grokbot.call.v0`. `message` is the caller's text (not the stored reply hint). The webhook secret is an `Authorization` header, never a payload field.

```json
{
  "schema": "grokbot.call.v0",
  "event": "session.message",
  "session_id": "...",
  "member_name": "...",
  "member_agent_id": "...",
  "local_id": "...",
  "local_label": "...",
  "message": "...",
  "reply_required": true,
  "mcp_url": "https://call.kitepon.dev/mcp"
}
```

## MCP tools

| Tool | Role |
|------|------|
| `call_directory` | Phone book, built on that call from live seat profiles |
| `call_open` | Create session (local → member) |
| `call_send` | Send a message. Local sends `session.message` to Marian's webhook (`delivery.status` is `delivered` on HTTP 2xx). Notices use `reply_required=false`. Member sends are stored only |
| `call_poll` | Fetch new messages for your party |
| `call_list` | List / filter sessions |
| `call_hangup` | End the call |
| `call_info` | Session details |

Also exposes a small REST surface under `/v0` (same auth) and open `/health`.

## Phone directory

Clients only call `call_directory` (or `GET /v0/directory`). The server builds the book **on that request** from Grok Bot seat profiles. Changing a seat’s name, title, or description shows up on the **next** call. There is no periodic sync, no Marian routine, and no operator push after a role edit.

Source of truth is each seat’s **profile** (`name`, `title`, `description`) — used as-is (e.g. ラピ → title `インフラ統括`, `description` → `role`). There is **no** “may call” flag. Each member built from a profile also includes `id`: the seat directory name, which is the Grok Bot agent id (`profile.json` itself has no id field). Local `call_send` resolves `member_name` to that id and includes it as `member_agent_id` on `session.message`. A remote directory passes `id` or `agentId` through. A `directory.json` entry without an id cannot be relayed.

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
| `CALL_BRIDGE_WAKE_WEBHOOK_URL` | _(unset)_ | Switchboard webhook. `call_open` posts `session.opened` (no body; an empty URL skips that POST). Local `call_send` posts `session.message` (the text, `reply_required`, and `member_agent_id`; an empty URL fails the send and does not store). A down directory unix socket posts `bridge.link_down` at most once per 60 seconds |
| `CALL_BRIDGE_WAKE_WEBHOOK_AUTH` | _(unset)_ | `Authorization` header for those POSTs. Never placed in the payload |
| `CALL_BRIDGE_PUBLIC_MCP_URL` | `https://call.kitepon.dev/mcp` | MCP URL included in `session.opened` and `session.message` |

Compose sets `extra_hosts: ["host.docker.internal:host-gateway"]` so that hostname resolves inside the container. Recreate the container after changing `.env`. On the Grok Bot box, restart `scripts/directory_live_server.py` from this revision so live directory members include `id`.

`CALL_BRIDGE_SWITCHBOARD_WEBHOOK_URL` and `CALL_BRIDGE_SWITCHBOARD_WEBHOOK_AUTH` are aliases for the webhook URL and auth value (`session.opened`, `session.message`, and `bridge.link_down`).

## Stack

- Python 3.12 in Docker (package requires Python 3.11+) / FastMCP streamable HTTP (`mcp>=1.2,<2`)
- SQLite for sessions + messages
- Docker Compose for deploy

## License

MIT © kitepon.dev
