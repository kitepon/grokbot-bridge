#!/usr/bin/env node
// call_bridge（Python）から呼ぶ、Claude Code の会話あての配送の入口。結果は stdout へ1行の JSON。
// 共通パッケージ aiterm-steer-delivery の CLI は Codex だけなので、Claude Code の channel はここから呼ぶ。
//
//   open     --client <name> --meta <json>            → channel_id, session_id（道具を呼んだ会話に channel を開く）
//   open-wait --client <name>                          → channel_id, kind（cursor|background）, wait（待ち受けの命令の1行）
//   wait     --channel <uuid>                          → wait, closed, kind
//   own      --client <name> --meta <json> [--channel <uuid>] → session_id（道具を呼んだ会話）, channel_open, channel_session
//   owner    --channel <uuid>                          → session（受け口を開いた Claude Code の会話。ほかの受け口は null）, kind, closed
//   owners   --channels <uuid,uuid,...>                → owners（受け口ごとの、開いた Claude Code の会話。読めない受け口は null）
//   send     --channel <uuid> --delivery <uuid>       → state（本文は stdin）
//   state    --channel <uuid> --delivery <uuid>       → state（queued|sending|emitted|unknown|withdrawn|null）, closed,
//                                                       parent_alive・transcript_age（queued の時だけ。会話が生きているか）
//   withdraw --channel <uuid> --delivery <uuid>       → withdrawn（真なら、会話へは出ていない）
//   close    --channel <uuid>
//   setup    <enable|disable|status> --settings <file>          （Claude Code の hook。配送の hook と、後継へ通話を知らせる hook）
//   cursor-setup <enable|disable|status> [--hooks <file>]      （Cursor の hook）
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

function hookRuntime(steer, profile, name = profile.hooks.claude) {
    return { command: steer.setupNodeExecutable(), script: path.join(here, name) };
}

// Throughline が立てた後継の会話へ、前の会話が持っていた通話を知らせる hook（UserPromptSubmit）。
// 配送のパッケージが登録する hook とは入口の名前が違うので、パッケージの登録・解除はこれに触らない。ここで足し引きする。
const PROMPT_HOOK = "call-bridge-claude-prompt.mjs";
const isRecord = (value) => typeof value === "object" && value !== null && !Array.isArray(value);
const isPromptHook = (entry) => isRecord(entry) && typeof entry.command === "string"
    && (entry.command.includes(`/${PROMPT_HOOK}'`) || entry.command.includes(`\\${PROMPT_HOOK}'`));

function promptHookEntry(steer, profile) {
    return { type: "command", ...steer.claudeParentHookCommand(hookRuntime(steer, profile, PROMPT_HOOK)), timeout: 30 };
}

function readSettings(file) {
    if (!fs.existsSync(file)) return {};
    const document = JSON.parse(fs.readFileSync(file, "utf8"));
    if (!isRecord(document) || (document.hooks !== undefined && !isRecord(document.hooks))) {
        throw Object.assign(new Error("Claude の hook の設定を読めません"), { delivery_code: "config_invalid" });
    }
    return document;
}

function promptHookRegistered(document) {
    const groups = isRecord(document.hooks) ? document.hooks.UserPromptSubmit : undefined;
    return Array.isArray(groups) && groups.some(group => isRecord(group) && Array.isArray(group.hooks) && group.hooks.some(isPromptHook));
}

/** ほかの製品の UserPromptSubmit の hook と並びはそのまま保ち、この製品の1つだけを足す・外す。 */
function writePromptHook(file, entry) {
    const target = fs.existsSync(file) ? fs.realpathSync(file) : file;
    const current = readSettings(target);
    const previous = current.hooks?.UserPromptSubmit ?? [];
    if (!Array.isArray(previous) || previous.some(group => !isRecord(group) || !Array.isArray(group.hooks))) {
        throw Object.assign(new Error("Claude の UserPromptSubmit の hook の形を読めません"), { delivery_code: "config_invalid" });
    }
    const owned = previous.flatMap(group => group.hooks.filter(isPromptHook));
    if (entry !== null && owned.length === 1 && JSON.stringify(owned[0]) === JSON.stringify(entry)) return "unchanged";
    if (entry === null && owned.length === 0) return "unchanged";
    const retained = previous.map(group => ({ ...group, hooks: group.hooks.filter(hook => !isPromptHook(hook)) }))
        .filter(group => group.hooks.length > 0);
    const groups = entry === null ? retained : [...retained, { hooks: [entry] }];
    const hooks = { ...(current.hooks ?? {}) };
    if (groups.length > 0) hooks.UserPromptSubmit = groups; else delete hooks.UserPromptSubmit;
    const next = { ...current, hooks };
    fs.mkdirSync(path.dirname(target), { recursive: true });
    const temporary = `${target}.call-bridge-prompt-${process.pid}`;
    try {
        fs.writeFileSync(temporary, `${JSON.stringify(next, null, 2)}\n`, { mode: 0o600, flag: "wx" });
        fs.renameSync(temporary, target);
    } finally {
        if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
    }
    if (JSON.stringify(JSON.parse(fs.readFileSync(target, "utf8"))) !== JSON.stringify(next)) {
        throw Object.assign(new Error("Claude の hook の登録の読み戻しが一致しません"), { delivery_code: "config_readback_failed" });
    }
    return "configured";
}

/** Cursor と Grok の会話が、返信を待つ時に背景で動かす命令（その端末の shell に書ける1行）。 */
function waitCommand(steer, channelId) {
    return steer.waitProcessCommandLine(steer.channelReceiveProcess(
        path.join(here, "call-bridge-channel-receive.mjs"), channelId, steer.setupNodeExecutable()));
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
        case "open-wait": {
            // Cursor と Grok の会話に、待ち受けの命令で受け取る受け口を開く。
            // Cursor は hook が登録してあれば、作業中の会話へ次の道具の返りで差し込める（道具の返りの印で会話へ結ぶ）。
            // それ以外（Grok など）は、待ち受けの命令だけで受け取る。
            const client = option(args, "--client") ?? "";
            const parent = steer.isCursorMcpClient(client)
                ? steer.cursorParentFromRequest(profile, client, { hookRoot: steer.cursorHookRoot(profile) })
                : null;
            const channel = steer.openChannel(profile, parent);
            return { channel_id: channel.channel_id, kind: channel.kind, wait: waitCommand(steer, channel.channel_id) };
        }
        case "wait": {
            // 今の受け口の、待ち受けの命令。閉じていれば closed が真。
            const id = required(args, "--channel");
            const channel = steer.readChannel(profile, id);
            return { channel_id: id, kind: channel.kind, closed: steer.channelClosed(profile, id), wait: waitCommand(steer, id) };
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
        case "owner": {
            // 受け口を開いた会話を答える。受け口は作らない・変えない。
            const id = required(args, "--channel");
            const channel = steer.readChannel(profile, id);
            return { channel_id: id, kind: channel.kind ?? null, session: channel.claude?.session_id ?? null,
                     closed: steer.channelClosed(profile, id) };
        }
        case "owners": {
            // 受け口ごとの、開いた会話。後継へ「前の会話が持っていた通話」を知らせる時に、1回で引く。
            const owners = {};
            for (const id of required(args, "--channels").split(",").filter(Boolean)) {
                try { owners[id] = steer.readChannel(profile, id).claude?.session_id ?? null; } catch { owners[id] = null; }
            }
            return { owners };
        }
        case "send":
            return await steer.sendToChannel(profile, required(args, "--channel"), required(args, "--delivery"), await readStdin());
        case "state": {
            const id = required(args, "--channel");
            const state = steer.channelDeliveryState(profile, id, required(args, "--delivery"));
            // まだ誰も取っていない時だけ、会話が生きているかを見る。生きていれば、番の途中で待ち受けが居ないだけ
            // （番の終わりに取り出す）。channel を開いた process と、会話の記録が最後に書かれてからの秒数の2つを返す。
            // 起動し直して再開された会話は、開いた時の process が居なくても、記録が書かれ続ける。
            const channel = state === "queued" ? steer.readChannel(profile, id) : undefined;
            const parent = channel?.claude;
            const alive = parent ? steer.readRuntimeProcesses().some(row =>
                row.pid === parent.parent_pid && row.started_identity === parent.parent_started_identity) : null;
            return { state, closed: steer.channelClosed(profile, id), parent_alive: alive,
                     transcript_age: parent ? transcriptAge(parent.session_id) : null, kind: channel?.kind ?? null };
        }
        case "withdraw":
            return { withdrawn: steer.withdrawFromChannel(profile, required(args, "--channel"), required(args, "--delivery")) };
        case "close":
            steer.closeChannel(profile, required(args, "--channel"), option(args, "--reason"));
            return {};
        case "setup": {
            const file = required(args, "--settings");
            if (args[0] === "enable") {
                const result = steer.mergeClaudeParentHooks(profile, file, hookRuntime(steer, profile));
                return { result, prompt: writePromptHook(file, promptHookEntry(steer, profile)) };
            }
            if (args[0] === "disable") {
                const result = steer.removeClaudeParentHooks(profile, file);
                return { result, prompt: fs.existsSync(file) ? writePromptHook(file, null) : "unchanged" };
            }
            if (args[0] !== "status") throw Object.assign(new Error("enable・disable・status のどれかです"), { delivery_code: "CLI_USAGE" });
            const document = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : {};
            const scripts = steer.claudeParentHookScripts(profile, document);
            return { registered: steer.claudeParentHooksRegistered(profile, document),
                     scripts, missing: scripts.filter(script => !fs.existsSync(script)),
                     prompt_registered: promptHookRegistered(isRecord(document) ? document : {}) };
        }
        case "cursor-setup": {
            const file = option(args, "--hooks") ?? steer.cursorHooksFile();
            const hook = hookRuntime(steer, profile, profile.hooks.cursor);
            if (args[0] === "enable") return { result: steer.mergeCursorParentHooks(profile, file, hook), file };
            if (args[0] === "disable") return { result: steer.removeCursorParentHooks(profile, file), file };
            if (args[0] !== "status") throw Object.assign(new Error("enable・disable・status のどれかです"), { delivery_code: "CLI_USAGE" });
            const document = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : {};
            return { registered: steer.cursorParentHooksRegistered(profile, document), file };
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
