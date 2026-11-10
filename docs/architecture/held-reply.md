# Held reply: function, data flow and state (repaired range)

Scope: the local reply watcher in `src/call_bridge/local.py` when a reply is certainly not delivered
(`DeliveryError` with `outcome_unknown=False`, e.g. `AITERM_LAUNCH_FAILED` with `sent=False`).
Other paths (unknown outcome, receipts, channel recovery) are unchanged and not drawn here.

## Function map

| Function | Role in this range |
|---|---|
| `Watchers._watch_claimed` | Polls the bridge (`party=local`, peek for channel/manual). Calls `_handle` when there are messages, `hold_seq` allows it, and `_due` is true. |
| `Watchers._hold_due` | True when the per-call `hold_retry_at` time has passed (absent = due, so a restarted receiver retries once). |
| `Watchers._handle` | Reserves the seq, delivers by mode. On a certain failure calls `LocalStore.hold` and sets `hold_retry_at = now + _HOLD_RETRY_SECONDS` (1800 s). |
| `LocalStore.reserve` | A `failed` delivery row goes back to `sending` with the same deterministic `delivery_id` (no duplicate row). |
| `LocalStore.hold` | Delivery row `failed` + error; subscription `hold_seq = latest_seq`, `last_error`. |
| `Watchers._confirm_queued` / `_confirm_channel` | After a withdrawn reply also fails to reach the seat, call `LocalStore.rewind` (cursor back, `hold_seq = old after_seq`) and set the same `hold_retry_at`. |
| `relaunch.hand_over` | Launches/sends to the folder seat through Aiterm; a `sent=False` launch failure closes the leftover terminal. |

## Data flow

```
bridge REST poll (messages after after_seq)
  -> _watch_claimed: gate = messages AND (hold_seq is None OR last seq > hold_seq OR _hold_due) AND _due
  -> _handle -> reserve(seq) -> mode delivery (channel / hosted / queue / manual) -> Aiterm / Steer / Codex
       success            -> LocalStore.submitted / relaunched -> receipt to bridge
       certain failure    -> LocalStore.hold + hold_retry_at[call] (memory) -> receipt failed
       unknown outcome    -> LocalStore.stop(unknown) (unchanged; never retried automatically)
```

`hold_retry_at` is in memory only. It is not a ledger field; the durable record stays `deliveries.state=failed` and `subscriptions.hold_seq`.

## State transitions of a held reply

| From | Event | To |
|---|---|---|
| delivery `sending` | certain delivery failure | `failed`, subscription `hold_seq` set, retry time = now + 1800 s |
| delivery `submitted` (withdrawn) | seat hand-over fails certainly | `failed`, cursor rewound, `hold_seq` set, retry time = now + 1800 s |
| `failed` (held) | newer member reply arrives | retried from the held seq (existing exit) |
| `failed` (held) | retry time passed, no newer reply | retried from the held seq (self-exit added 2026-11-10) |
| `failed` (held) | receiver restarts | retried once immediately (memory empty), then the 1800 s spacing |
| retried | success | `submitted` / `relaunched`, `hold_seq` cleared |
| retried | certain failure again | `failed`, new retry time |
| retried | unknown outcome | subscription `unknown`; no automatic retry (no duplicate body) |

Guard named: the 1800 s spacing protects the terminal host from a seat launch on every 2 s poll while a launch keeps failing.
Self-exit: the retry time always passes, so a held reply leaves `failed` without any new input.
