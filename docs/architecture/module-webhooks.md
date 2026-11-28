# Module webhooks: function, data flow and state (repaired range)

Scope: how the bridge server sends its two outgoing webhooks (`session.opened` / `bridge.link_down` to the switchboard,
the courier-silence wake) without holding their URL or key. Added 2026-11-28: an optional route through the
BellTeam authentication module's caller socket. Without it the old environment values work unchanged.
Inbound auth (`auth.py`, hashed `CALL_BRIDGE_TOKENS_FILE`) and the terminal-side token (`client-token.md`) are not drawn here.

## Function map

| Function | Role in this range |
|---|---|
| `module_http.configured(item)` | True when `CALL_BRIDGE_AUTH_SOCKET` and an item name are both set. |
| `module_http.post_json(item, payload, timeout)` | One request to the module: line JSON `{op:"http", name:item, method:"POST", path:"/", headers:{content-type}, body:base64, timeout_sec}` on the unix socket; one JSON line back. Returns the HTTP status. Refusal / error / no module raises `ModuleHttpError` with a short text; the module's text never carries the URL or key. |
| `wake._post_json(payload)` | Switchboard POST. Item `CALL_BRIDGE_SWITCHBOARD_AUTH_ITEM` set and module configured → `module_http.post_json`; else the environment URL and Authorization as before. Same result dict either way. |
| `wake.notify_ring` / `notify_link_down` | "Webhook set" means the environment URL **or** a configured module item. |
| `courier_watch.enabled()` / `notify(payload)` | Courier-silence wake. Item `CALL_BRIDGE_COURIER_WAKE_AUTH_ITEM` set and module configured → module route; else `CALL_BRIDGE_COURIER_WAKE_WEBHOOK_URL/_KEY` as before. |

## Data flow

```
bridge event (ring / link down / courier silence)
  -> wake._post_json | courier_watch.notify
       module item configured?  yes -> unix socket CALL_BRIDGE_AUTH_SOCKET -> module (adds URL + credential, POST)
                                                                           <- status only
                                no  -> env URL + Authorization (unchanged)
  -> same {status, detail} / log line as before
```

The URL and key stay inside the module; the bridge process holds only the socket path and the item names (not secrets).
The container gets the caller socket folder mounted; the module identifies the caller by that socket.

## State

No new state. A failed POST is logged and not retried (unchanged); `bridge.link_down` keeps its 60 s interval; the courier
wake keeps its durable claim before the POST.

Guard named: none added. A missing socket or a refusal ends that one POST as `error` exactly like an unreachable URL does today.
