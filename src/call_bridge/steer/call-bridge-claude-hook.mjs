#!/usr/bin/env node
// Claude Code が起こす hook の入口。中身は aiterm-steer-delivery の runClaudeHookMain。
// このファイルの名前で、設定の中の call-bridge の hook を見分ける。名前を変えない。
import { load } from "./call-bridge-profile.mjs";

let loaded;
try {
    loaded = await load();
} catch (error) {
    // 読み込めない時に 2 で終わると、Claude Code は道具の実行を拒み、止まった会話を起こす。1 で終わる（何もしない）。
    process.stderr.write(`CALL_BRIDGE_HOOK_UNAVAILABLE: ${error instanceof Error ? error.message : String(error)}\n`);
    process.exit(1);
}
await loaded.steer.runClaudeHookMain(loaded.profile);
