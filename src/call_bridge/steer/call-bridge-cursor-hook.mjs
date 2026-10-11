#!/usr/bin/env node
// Cursor が起こす hook の入口。中身は aiterm-steer-delivery の runCursorHookMain。
// 道具の返りに載せた印で通話の受け口をその会話へ結び、作業中の会話には、次の道具の返りで返信を差し込む。
// このファイルの名前で、設定の中の call-bridge の hook を見分ける。名前を変えない。
import { load } from "./call-bridge-profile.mjs";

let loaded;
try {
    loaded = await load();
} catch {
    // 読み込めない時も、Cursor の道具の実行は止めない。何も差し込まない答えを返して終わる。
    process.stdout.write("{}\n");
    process.exit(0);
}
await loaded.steer.runCursorHookMain(loaded.profile);
