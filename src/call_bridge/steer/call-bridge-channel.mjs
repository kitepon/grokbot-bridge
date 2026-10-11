#!/usr/bin/env node
// call_bridge（Python）から呼ぶ、Claude Code の会話あての配送の入口。結果は stdout へ1行の JSON。
// 共通パッケージ aiterm-steer-delivery の CLI は Codex だけなので、Claude Code の channel はここから呼ぶ。
//
//   open     --client <name> --meta <json>            → channel_id, session_id（道具を呼んだ会話に channel を開く）
//   own      --client <name> --meta <json> [--channel <uuid>] → session_id（道具を呼んだ会話）, channel_open, channel_session
//   send     --channel <uuid> --delivery <uuid>       → state（本文は stdin）
//   state    --channel <uuid> --delivery <uuid>       → state（queued|sending|emitted|unknown|withdrawn|null）, closed,
//                                                       parent_alive・transcript_age（queued の時だけ。会話が生きているか）
//   withdraw --channel <uuid> --delivery <uuid>       → withdrawn（真なら、会話へは出ていない）
//   close    --channel <uuid>
//   setup    <enable|disable|status> --settings <file>
//
// 失敗は {ok:false, code, message, outcome_unknown} を返し、exit 1。
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { here, load } from "./call-bridge-profile.mjs";

function option(args, name) {
    const index = args.indexOf(name);
    return index >= 0 ? args[index + 1] : undefined;
}

function required(args, name) {
    const value = option(args, name);
    if (value === undefined) throw Object.assign(new Error(`${name} がありません`), { delivery_code: "CLI_USAGE" });
    return value;
}

async function readStdin() {
    let text = "";
    process.stdin.setEncoding("utf8");
    for await (const chunk of process.stdin) text += chunk;
    return text;
}

/** Claude Code の会話の記録が最後に書かれてからの秒数。記録が見つからなければ null。 */
function transcriptAge(sessionId) {
    const root = path.join(process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude"), "projects");
    let newest = null;
    let projects = [];
    try { projects = fs.readdirSync(root); } catch { return null; }
    for (const project of projects) {
        try { newest = Math.max(newest ?? 0, fs.statSync(path.join(root, project, `${sessionId}.jsonl`)).mtimeMs); } catch { /* このフォルダには無い */ }
    }
    return newest === null ? null : Math.max(0, (Date.now() - newest) / 1000);
}

function hookRuntime(steer, profile) {
    return { command: steer.setupNodeExecutable(), script: path.join(here, profile.hooks.claude) };
}

async function run(argv) {
    const { steer, profile } = await load();
    const [command, ...args] = argv;
    switch (command) {
        case "open": {
            const parent = steer.claudeParentFromRequest(profile, required(args, "--client"),
                JSON.parse(required(args, "--meta")), steer.claudeHookRoot(profile));
            if (parent === null) throw Object.assign(new Error("Claude Code の会話ではありません"), { delivery_code: "CLAUDE_PARENT_UNSUPPORTED" });
            const channel = steer.openChannel(profile, parent);
            return { channel_id: channel.channel_id, session_id: channel.claude.session_id };
        }
        case "own": {
            // 道具を呼んだ会話と、通話に付いている受け口の持ち主・開閉を答える。受け口は作らない。
            const parent = steer.claudeParentFromRequest(profile, required(args, "--client"),
                JSON.parse(required(args, "--meta")), steer.claudeHookRoot(profile));
            if (parent === null) throw Object.assign(new Error("Claude Code の会話ではありません"), { delivery_code: "CLAUDE_PARENT_UNSUPPORTED" });
            const id = option(args, "--channel");
            let open = false, session = null;
            if (id) {
                try {
                    session = steer.readChannel(profile, id).claude?.session_id ?? null;
                    open = !steer.channelClosed(profile, id);
                } catch { /* 受け口の記録が無い。閉じている物として扱う */ }
            }
            return { session_id: parent.session_id, channel_open: open, channel_session: session };
        }
        case "send":
            return await steer.sendToChannel(profile, required(args, "--channel"), required(args, "--delivery"), await readStdin());
        case "state": {
            const id = required(args, "--channel");
            const state = steer.channelDeliveryState(profile, id, required(args, "--delivery"));
            // まだ誰も取っていない時だけ、会話が生きているかを見る。生きていれば、番の途中で待ち受けが居ないだけ
            // （番の終わりに取り出す）。channel を開いた process と、会話の記録が最後に書かれてからの秒数の2つを返す。
            // 起動し直して再開された会話は、開いた時の process が居なくても、記録が書かれ続ける。
            const parent = state === "queued" ? steer.readChannel(profile, id).claude : undefined;
            const alive = parent ? steer.readRuntimeProcesses().some(row =>
                row.pid === parent.parent_pid && row.started_identity === parent.parent_started_identity) : null;
            return { state, closed: steer.channelClosed(profile, id), parent_alive: alive,
                     transcript_age: parent ? transcriptAge(parent.session_id) : null };
        }
        case "withdraw":
            return { withdrawn: steer.withdrawFromChannel(profile, required(args, "--channel"), required(args, "--delivery")) };
        case "close":
            steer.closeChannel(profile, required(args, "--channel"), option(args, "--reason"));
            return {};
        case "setup": {
            const file = required(args, "--settings");
            if (args[0] === "enable") return { result: steer.mergeClaudeParentHooks(profile, file, hookRuntime(steer, profile)) };
            if (args[0] === "disable") return { result: steer.removeClaudeParentHooks(profile, file) };
            if (args[0] !== "status") throw Object.assign(new Error("enable・disable・status のどれかです"), { delivery_code: "CLI_USAGE" });
            const document = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : {};
            const scripts = steer.claudeParentHookScripts(profile, document);
            return { registered: steer.claudeParentHooksRegistered(profile, document),
                     scripts, missing: scripts.filter(script => !fs.existsSync(script)) };
        }
        default:
            throw Object.assign(new Error(`未対応の命令です: ${command}`), { delivery_code: "CLI_USAGE" });
    }
}

try {
    process.stdout.write(JSON.stringify({ ok: true, ...await run(process.argv.slice(2)) }) + "\n");
} catch (error) {
    process.stdout.write(JSON.stringify({
        ok: false, code: error?.delivery_code ?? (typeof error?.code === "string" ? error.code : "CHANNEL_FAILED"),
        message: error?.message ?? String(error), outcome_unknown: error?.outcome_unknown === true }) + "\n");
    process.exitCode = 1;
}
