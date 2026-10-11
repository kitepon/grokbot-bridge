// call-bridge の Node の入口が共有する読み込み。
// 隣の steer.json（call-bridge-setup が書く）から、共通パッケージ aiterm-steer-delivery の場所と
// call-bridge の識別情報を読む。パッケージは利用者の端末に1つ入っている物を使い、ここへは同梱しない。
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const here = path.dirname(fileURLToPath(import.meta.url));

export async function load() {
    const config = JSON.parse(fs.readFileSync(path.join(here, "steer.json"), "utf8"));
    const steer = await import(pathToFileURL(config.library).href);
    const value = config.profile;
    // channels を持つ製品にだけ、番の終わりと会話の始まりの待ち受けが付く。
    // 期限（24時間）の知らせは持たない。丸1日止まっていた会話は起こさず、返信は担当フォルダの席へ渡る。
    const profile = { ...value, state_root: () => value.state_root, config_root: () => value.config_root, channels: {} };
    return { steer, profile };
}
