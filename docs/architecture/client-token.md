# Client token: function, data flow and state (repaired range)

Scope: how the terminal side (`call_bridge.local` MCP, the receiver/watchers, the Codex hook, `recover`,
`channel_recover`) gets the bearer token for the bridge, and how a 401 reaches the caller.
Added 2026-11-11: an optional token command resolved in process memory, so a host can keep the token in a
secret store (for example `op read op://…`, `op-agent pipe`, `pass`, a keychain CLI) instead of a plaintext
`auth.json`. The environment variable and `auth.json` stay the default; no secret store is required.
Server-side auth (`auth.py`), `directory._remote_headers` and the box courier (`CALL_BRIDGE_TOKEN_FILE`) are unchanged and not drawn here.

## Function map

| Function | Role in this range |
|---|---|
| `local._headers()` | Single place every terminal-side caller gets `Authorization`. Order: env `token_env` (default `CALL_BRIDGE_TOKEN`) → token command → `auth.json` (only when `enabled` and no command is set). |
| `client_token.token_command(config)` | The configured command: env `CALL_BRIDGE_TOKEN_COMMAND`, else `config.json` `token_command`. Not a secret (it names a reference, never holds the value). |
| `client_token.command_token(command)` | Runs the command (shell, no stdin, hidden window on Windows, 120 s timeout), keeps the first stdout line in `client_token._COMMAND_TOKENS[command]` (process memory). Failure → `BRIDGE_TOKEN_COMMAND_FAILED` with the exit code only; output is never put in errors or logs. |
| `client_token.forget_token()` | Drops the in-memory command token. Returns whether one was cached. Called on every 401 seen by `_remote_tool` and the watcher poll. |
| `local._remote_tool()` | MCP call to the bridge. A 401 (raised by the MCP client inside its TaskGroup) is unwrapped: forget the token and retry once if a command token was cached; otherwise `BRIDGE_UNAUTHORIZED`. Other HTTP status / transport errors become `BRIDGE_HTTP_<code>` / `BRIDGE_TRANSPORT` instead of "unhandled errors in a TaskGroup". |
| `Watchers._watch_claimed` | Calls `_headers()` every poll (unchanged). A 401 forgets the token and polls once more; a second consecutive 401 stops the watch as `failed BRIDGE_POLL_HTTP_401` (unchanged end state). |
| `setup.enable` (Codex) / `harness_setup.require_token` (Claude Code, Cursor, Grok) | With a token command configured: run it once to check it, save `token_command` in `config.json`, and do not write `auth.json`. An existing `auth.json` is left for the operator to delete after restarting (old processes may still read it); Codex `enable` returns a `warning` saying so. Without a command: unchanged (env → `auth.json`). |

## Data flow

```
config.json {enabled, token_env, token_command?}      env CALL_BRIDGE_TOKEN / CALL_BRIDGE_TOKEN_COMMAND
                     \                                   /
                      -> local._headers()  (every request; reads config.json each time)
                           env token set?            -> use it (fixed for the process)
                           token command set?        -> _COMMAND_TOKENS[cmd] or run cmd once -> memory only
                           enabled and auth.json?    -> read file each time (rotation by file replace)
                           none                      -> BRIDGE_TOKEN_MISSING
                      -> Authorization header -> bridge (MCP /mcp, REST /v0)
bridge 401 -> _remote_tool / watcher poll -> client_token.forget_token() -> next _headers() reruns the command
```

The command token never touches disk: not `auth.json`, not `config.json`, not the local ledger, not logs.

## State of the in-memory command token (per process)

| From | Event | To |
|---|---|---|
| empty | `_headers()` with a command configured | cached (command exit 0, one non-empty line) / error `BRIDGE_TOKEN_COMMAND_FAILED` (stays empty) |
| empty (error) | next `_headers()` | runs the command again (no back-off; the watcher's existing 15-attempt token-unreadable limit applies) |
| cached | bridge answers 401 | empty, request retried once (MCP call) / next poll (watcher) |
| cached | `token_command` changed in config.json | other key → resolved fresh on next use |
| cached | process exits | gone |

Guard named: none added. The watcher's existing stop after two consecutive 401s protects the bridge from a
poll every 2 s with a rejected token; self-exit is unchanged (the next `call_open`/`call_adopt` or receiver
recovery starts a new watch, and `_headers()` reruns the command).
