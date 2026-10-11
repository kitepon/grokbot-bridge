#!/usr/bin/env node
// Claude Code の UserPromptSubmit の hook。Throughline が立てた後継の会話へ、前の会話が持っていた通話を知らせる。
//
// 後継の最初の文は「Throughline自動継続 <引き継ぎID>」で始まる。その時だけ、call_bridge.handoff_hint（Python）に尋ねて、
// 前の会話の通話があれば「最初に call_adopt で引き取る」を、追加の文脈として会話へ渡す。引き取る道具は、会話が自分で呼ぶ。
// ほかの文では、何も読まず、何も出さずに 0 で終わる。どんな失敗でも 0 で終わる（2 で終わると、Claude Code が文を捨てる）。
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const PREFIX = /^\s*Throughline自動継続\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f-])/u;

try {
    const event = JSON.parse(fs.readFileSync(0, "utf8"));
    const match = typeof event?.prompt === "string" ? PREFIX.exec(event.prompt) : null;
    if (match) {
        const here = path.dirname(fileURLToPath(import.meta.url));
        const config = JSON.parse(fs.readFileSync(path.join(here, "steer.json"), "utf8"));
        if (typeof config.python === "string" && fs.existsSync(config.python)) {
            const done = spawnSync(config.python, ["-m", "call_bridge.handoff_hint", match[1]],
                { encoding: "utf8", timeout: 25000, windowsHide: true, stdio: ["ignore", "pipe", "ignore"] });
            const hint = done.status === 0 && typeof done.stdout === "string" ? done.stdout.trim() : "";
            if (hint) {
                process.stdout.write(JSON.stringify({
                    hookSpecificOutput: { hookEventName: "UserPromptSubmit", additionalContext: hint } }) + "\n");
            }
        }
    }
} catch { /* 何も出さない */ }
process.exit(0);
