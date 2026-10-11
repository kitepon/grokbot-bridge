#!/usr/bin/env node
// Cursor と Grok の会話が、返信を待つ時に背景で動かす命令。中身は aiterm-steer-delivery の runChannelReceiveMain。
//   node call-bridge-channel-receive.mjs --channel <uuid>
// 次に届いた返信を1行の JSON で出して終わる（0=受け取った、3=期限切れ、4=受け口が閉じた、1=誤り）。
// 受け取った時と期限切れの時は、同じ待ち受けを張り直す命令（next_wait_process）も付く。
import { load } from "./call-bridge-profile.mjs";

let loaded;
try {
    loaded = await load();
} catch (error) {
    process.stdout.write(JSON.stringify({ ok: false, code: "CALL_BRIDGE_RECEIVE_UNAVAILABLE",
        message: error instanceof Error ? error.message : String(error) }) + "\n");
    process.exit(1);
}
await loaded.steer.runChannelReceiveMain(loaded.profile);
