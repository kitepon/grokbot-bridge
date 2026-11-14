# Public connection handoff

Owner: Bridge (Liter). Contract: `call-bridge.connection.v1`.

BellTeam runs `python3 scripts/export_connection.py --json` as the deploy
user, with cwd equal to `CALL_BRIDGE_DEPLOY_ROOT`. Explicit `--json` is
required. It passes no stdin or secret arguments. The installed equivalent
is `call-bridge-connection --json` or `python -m call_bridge.connection --json`.

## Function map

| Entry | Input | Output / ownership |
| --- | --- | --- |
| `connection.export_connection(root)` | Bridge-owned reference-only `root/connection-export.json` | Validate endpoint; run the configured credential provider once; validate the credential with `auth.Authenticator`; return v1 object. |
| `connection.main` / `scripts/export_connection.py` | Explicit `--json`; cwd is Bridge deployment root | Success: exit 0, exactly one JSON object plus newline on stdout, empty stderr. Failure: nonzero exit, empty stdout, credential-free stderr. |
| BellTeam import (bd10 / bd11) | Captured complete v1 output plus successful process exit | Validate exact schema/fields, persist only BellTeam's `callBridge` feature setting. Existing BellTeam code derives its own headers. Failure retains prior settings. |

Output has exactly three keys:

```json
{"schema":"call-bridge.connection.v1","url":"http://bridge.example:18910/mcp","token":"<secret>"}
```

`url` is an absolute HTTP(S) MCP endpoint reachable from the BellTeam
container, ending in `/mcp`, without whitespace, userinfo, query or fragment.
`token` is a nonempty raw Bearer credential without whitespace. Bridge
checks that it is registered as `system=bellteam`, without fixed `id`,
ops or courier privileges, and allows delegated `X-Call-Bridge-Caller-Id`.
BellTeam continues to set that header for each Bot itself.

## Data flow and state

```text
Bridge-owned connection-export.json {url, tokens_file, token_command}
  -> validate URL/config
  -> existing credential provider (one argv command, no shell/stdin, 45 s)
       -> raw credential in process memory only
  -> auth.Authenticator reads Bridge-owned hash registry and validates
  -> stdout {schema, url, token}; exit 0
  -> BellTeam stdin importer -> BellTeam-owned callBridge {url, token}
       -> existing BellTeam code derives its own 0600 headers
  -> BellTeam MCP relay / channel dispatcher -> Bridge public /mcp
```

Export never changes files, issues/revokes tokens, restarts services, probes
Bridge's network API or creates a call. The configured secret provider may
read 1Password; resolving Bridge's own credential remains Bridge's concern.
It is invoked once with stdin closed and its stdout/stderr captured. No
provider output enters diagnostics. The provider must be non-interactive
and must not write credentials or descendants to disk; 1Password remains
the credential store. BellTeam's outer deadline is 60 seconds.

Export has no durable state. Every error exits without JSON; correcting its
configuration/provider/registration and running it again leaves the error
condition. No retry loop, failure lock or new restriction on calls is added.
Consumer must check exit status before committing; EOF alone is not success.
Molly owns the host invocation/old-prepare retirement (bd10), and Mika owns
object validation and feature-setting persistence (bd11). They agreed this
intake instead of copying `.env` or Bridge-issued headers.

## Bridge-private configuration

`connection-export.json` is owned by Bridge's deploy user and contains
references only. Its three required fields are `url`, `tokens_file` (relative
to the deployment root or absolute), and `token_command` (nonempty argv array).
The command prints one raw credential, optionally with one final newline.
The consumer has no dependency on this private config or registry layout.

```json
{"url":"http://bridge.example:18910/mcp","tokens_file":"auth/tokens.json","token_command":["/path/to/noninteractive-secret-provider","<reference-only-arguments>"]}
```

The existing issuer `scripts/issue_token.py --system bellteam
--caller-id-header` and its server-startup reload requirement stay unchanged.
The exporter reads the already-issued credential; it never invokes issuance.

## Socket boundary

BellTeam owns its directory/delivery socket and announces its absolute host
directory to Bridge. Bridge owns setting the existing public launch inputs
`CALL_BRIDGE_BELLTEAM_SOCKET_HOST` and `CALL_BRIDGE_BELLTEAM_UNIX` and applying
its deployment procedure. BellTeam must stop editing Bridge's `.env`,
`op.env` and Compose configuration. Consumers receive the connection export
through process stdout only and own all imported settings themselves.

## Existing connections in this range

`auth.Authenticator` remains the owner of token registration and principal
validation. `server.BearerAuthMiddleware` applies it to public `/mcp` and
`/v0` requests; `server._open/_send` retain their existing party checks and
CallStore ownership. `bellteam.py` still uses the configured UNIX socket
for GET `/v0/directory` and POST `/v0/deliver`; delivery=running/steered
means acceptance, not reading or business completion. No call data flow,
database schema, caller-ID ownership or delivery behavior changes here.
