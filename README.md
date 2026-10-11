# grokbot-bridge

## BellTeamとの直通

電話帳はGrokBotのプロフィールとBellTeamのBot一覧を要求ごとに合わせて返す。各項目の`system`と`id`で宛先を区別する。BellTeam側のUNIXソケットを`CALL_BRIDGE_BELLTEAM_UNIX`に設定し、その親ディレクトリをコンテナの`/run/bellteam`へマウントする。BellTeam側には`BELLTEAM_CALL_BRIDGE_SOCKET`を設定する。BellTeam宛ての本文とBellTeam発信者宛ての返信はこのソケットを通り、マリアンは通らない。

BellTeam宛ての配送受付は最大30秒待つ。UNIXソケットへの接続前の失敗は`delivery.status=error`で本文を保存しない。接続後のタイムアウトやHTTP 5xxは、BellTeam側へ届いた可能性があるため`delivery.status=unknown`として本文を通話履歴へ保存する。`unknown`を見て同じ本文を自動再送しない。BellTeamの通話記録と配送状態を`session_id`で確認してから対応を決める。`delivered`はBellTeam受付を示し、相手Botの読了を示さない。

BellTeam宛ての開始例。`member_name`は名前またはBot IDを指定できる。同名のBotがいる場合はIDを指定する。BellTeamのBotが発信する場合は`local_system="bellteam"`と自身のBot IDを`local_id`に指定する。

```json
{"local_id":"caller-id","local_label":"呼び出し元","member_name":"bot-xxxxxxxx","member_system":"bellteam","local_system":"local"}
```

`call_open`のあと`call_send(session_id, from_party="local", message="...")`で着信させる。BellTeam宛ての`call_open`だけでは相手Botを起こさない。返信は相手Botが同じ`session_id`へ`from_party="member"`で送る。BellTeam発信の通話ではブリッジが返信を発信Botへ渡す。GrokBot発信者と一般のローカルAIは`call_poll(party="local")`で返信を読む。ローカルCodexの既存の自動配送はそのまま使える。

GrokBot宛ての本文はマリアンを通らない。本文は橋に保存し、マリアンは本文なしの着信（`session.opened`）で相手を起こすだけ。相手は`call_poll`で本文を読む。GrokBotのAIがレート制限中でも、電話帳、BellTeam宛ての直通、保存済み返信の取得は独立して動く。Webhookの2xxとBellTeamの受付は、相手Botの読了を示さない。

## 端末の会話が止まっている時

端末（local）が開いた通話へ member が返した時、結び付いた会話が返信を受け取れない状態なら、端末の側が担当フォルダの席へ渡す。通話の `session_id` は変わらないので、member は今までどおり同じ通話へ返せばよい。

- 席は Aiterm の session で、端末・ハーネス・フォルダごとに1つ（名前は `cb-<ハーネス>-<フォルダ名>-<8桁>`）。動いていればそこへ送り、無ければ立てる。止まった会話に付いていた通話は、その席へ付け替える。
- Codex の会話は、キューへ入れる前に状態を読む。会話が無い・途中で止められている時は、Throughline の続きの会話（`throughline auto-handoff status --json`）があればそこへ付け替え、無ければ席へ渡す。中断から120秒以内は待つ。
- キューへ入れた返信は、50秒後にキューから出たかを確かめる。番が走っていれば待つ。寝たまま・止められたままなら、キューから取り消したのを確かめてから席へ渡す。確かめられない時は `unknown` で止め、渡さない。
- Claude Code の会話は、hook が入っていれば、返信を会話へ自動で渡す（`call_open` の結果の `parent_delivery.state` が `watching`）。番を終えて止まっている会話は、返信で起きる。作業中の会話には、その番へ入る（同じ番の2通目からは、番の終わりに入る）。
- Claude Code の会話へ入れた返信は、10秒後に会話へ出たかを確かめる。誰も取り出していなくて、会話も生きていない時は、取り下げてから席へ渡す。取り下げた返信が何通かあれば、まとめて1回で渡す。生きている会話（channel を開いた process が居るか、会話の記録がこの2分のうちに書かれている）は、30分まで待つ。会話が終わった時は hook が受け口を閉じるので、次の返信はすぐ席へ渡る。
- Cursor・Grok と、hook の無い Claude Code の会話は、返信を自分で `call_poll` する（`parent_delivery.state` が `manual`。Claude Code では `reason` に理由が出る）。会話が生きている間は、その会話の MCP が通話の鍵を持つ。会話が終わった後に届いた返信は、常駐の受け取り係が席へ渡す。
- 確かに届けていない失敗（席が起動の画面で止まった、フォルダが分からない等）では見張りを止めず、同じ通話に新しい返信が来た時に、その返信からやり直す。時間で繰り返す再試行はしない。
- 立てた結果・送った結果が分からない時は、立て直さない・送り直さない。本文は通話に残る。

member から見える物：

- `call_send(from_party="member")` の結果。相手が BellTeam 以外なら `delivery.status=stored` と、相手が最後に取りに来た時刻（`local_seen_at`）。`stored` は受領を表さない。
- `call_info` の `local_delivery`。seq ごとに `submitted`（会話のキューか席へ入れた）、`started`（会話が番を始めた）、`fetched`（会話が自分で取りに来た）、`relaunched`（席へ渡した。`detail` に理由）、`failed`、`unknown`。`conversation` は `codex:<会話>`、`claude:<受け口>`、`hosted:<席>` のどれか。
- `call_history`。両方の発言を seq の順に読む（既読にしない）。席は、これで前のやりとりを読む。

端末の側の道具：`call_adopt(session_id)` は、新しい会話（Codex か、hook の入った Claude Code）が通話を自分へ付け替える。席が受け取った通話を、人が見ている会話へ戻す時にも使う。

### 常駐の受け取り係

Codex が1つも動いていない時と、Claude Code・Cursor・Grok の会話が終わった後に、返信を受け取る。ログイン中だけ動き、端末に1つ。`call-bridge-setup enable` が登録して起こし、`disable` が外す。

| OS | 登録先 | 動く場所 |
|---|---|---|
| macOS | `~/Library/LaunchAgents/dev.kitepon.call-bridge.receiver.plist` | 画面のある session（Aqua） |
| Linux | `~/.config/systemd/user/call-bridge-receiver.service` | 利用者の session（linger は設定しない） |
| Windows | タスク スケジューラの `call-bridge-receiver`（ログオン時） | 利用者の画面のある session |

```bash
call-bridge-setup receiver status      # {"receiver": "running" | "registered" | "not_registered" | "failed: …"}
call-bridge-setup receiver install
call-bridge-setup receiver uninstall
```

Windows の常駐は console を持たない（`pythonw`）。動いている間に起こす子の process（Codex の App Server、aiterm-steer-delivery、Throughline、Aiterm）は、端末の窓を出さない指定で起こす。指定が無いと、起こすたびに端末の窓が 0.2〜0.4 秒出る（2026-10-10 に fox で測った）。

席を立てるには、その端末に `aiterm-mcp`、tmux（Windows は psmux）、使うハーネスの CLI が要る。寝ている Codex アプリの会話を起こすのは aiterm-steer-delivery 0.4.1 以上（macOS・Windows）。

### Claude Code・Cursor・Grok への登録

```bash
call-bridge-setup harness claude-code enable   # status／disable も同じ形
call-bridge-setup harness cursor enable
call-bridge-setup harness grok enable
```

各 CLI の利用者の設定へ、ローカル MCP（`python -m call_bridge.local`）を `call-bridge` の1項目だけ書く。合言葉は、この端末の `auth.json`（Codex で `enable` 済み）か、環境変数から受け取って `auth.json` に置く。席から通話へ返すにも、この登録が要る。

Claude Code には、返信を会話へ自動で渡すための hook も登録する（`status` の `delivery` が `automatic`）。

- 使うのは共通パッケージ aiterm-steer-delivery（0.4.2 以降）の channel。Claude Code の公式の hook（`asyncRewake`）が、止まっている会話を起こして本文を渡す。パッケージの CLI は Codex だけなので、call-bridge の Node の入口（`src/call_bridge/steer/`）から呼ぶ。入口は置き場の `steer/` へ写し、パッケージの場所を隣の `steer.json` に書く。入れ直した後は、もう一度 `enable` を流す。
- 登録先は Claude Code の利用者の設定（`~/.claude/settings.json`。`CLAUDE_CONFIG_DIR` があればその中）。`PreToolUse`・`PostToolUse`（`call_open` と `call_adopt` だけ）、`Stop`、`SessionStart`、`SessionEnd` に1つずつ足す。ほかの hook は変えない。書き換える前の控えが `settings.json.call-bridge-backup` に残る。
- 効くのは、登録の後に起きた会話。前から動いている会話は、今までどおり自分で取りに来る。
- パッケージが無い・古い端末でも、ローカル MCP の登録は済ませる。その時は `warning` に理由が出て、`delivery` は `manual`。
- 待ち受けは24時間で切れる。丸1日だれも話しかけていない会話は起こさず、返信は30分後に席へ渡る。

### Windows で入れ直す時

`uv tool install --force` は、動いている process が掴んでいるフォルダを消せずに途中で止まり、入っていた版が壊れた状態で残る。先に名前を変えてから入れる。

```powershell
schtasks /End /TN call-bridge-receiver
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*call_bridge.receiver*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Rename-Item "$env:APPDATA\uv\tools\grokbot-bridge" "grokbot-bridge.old-$(Get-Date -Format yyyyMMdd-HHmmss)"
uv tool install git+https://github.com/kitepon/grokbot-bridge.git
$env:CALL_BRIDGE_TOKEN = (Get-Content "$HOME\.grokbot-bridge\auth.json" | ConvertFrom-Json).token
call-bridge-setup enable
```

古いフォルダは、掴んでいる process（Codex が起こしたローカル MCP）が終わってから消す。`enable` は、aiterm-steer-delivery と node が PATH に見える所で流す。`enable` が配送の hook を登録し直した時は `restart_required` になり、Codex を起こし直すまで、動いている Codex の会話は新しい通話を開けない。

### 続きの会話（Throughline）

Throughline 0.16.15 以降では `throughline auto-handoff successor --thread <id> --json` で続きを引く（件数の上限が無く、消された続きは数えない）。続きを作っている途中（`pending.in_flight`）は待つ。引き継ぎが途中で止まっている時は、引き継ぎ ID と理由を付けて `failed` にし、席は立てない（`throughline auto-handoff resume` が正しい入口）。それより古い版では、`auto-handoff status --json` の一覧（20件まで）からたどる。

### 確かめた範囲（2026-10-10、本番の通話）

席が立つ → 通話を付け替える → member が `call_info` で結果を読める → 席が `call_send` で同じ通話へ返す、を実物で通した。

| | Linux | macOS | Windows |
|---|---|---|---|
| Codex | 通った | 通った | 通った |
| Claude Code | 通った | 通った | 通った |
| Cursor | 通った | 通った | 通った |
| Grok | 通った | 通った | 通っていない（席は立ち、文は入った。Grok の `user_prompt_submit` の hook が時間切れになり、その後 Grok が答えなかった） |

常駐の受け取り係は、3つの OS とも登録して動かした（systemd のユーザー単位、LaunchAgent、ログオン時のタスク）。

確かめていない事：

- 各ハーネスの会話の中から `call_open` する所（親の見分け、フォルダの読み取り）。試験では、端末で通話を開いて結び付けを直接作った。
- Codex で、止められた会話あての返信が続きの会話へ届く所（続きを引く所だけ、本物の記録で見た）。キューへ入れた後の確かめと取り消し。
- 常駐が、ログアウト・ログイン・再起動の後に戻る所。

席は、承認を聞かない形で立つ（Claude Code は bypass permissions、Cursor は Run Everything、Grok は always-approve）。立った席は、その端末でほぼ何でも実行できる。

Codex のアプリの会話として新しく立てる道は、入っていない（立つのは端末の中の席）。`codex exec` の親は今までどおりで、次のプロンプトまで返信を待つ。

Shared **phone-call bridge** MCP for **[Grok Bot](https://grok.x.ai/)** agent meshes (streamable HTTP).

A local coding agent (Claude Code, Codex, Cursor, …) calls either system through this MCP. Calls to GrokBot use Marian's webhook. Calls to BellTeam use its UNIX socket. Member replies remain in the call history; replies to a BellTeam caller are also delivered to that Bot.

## Why

- GrokBot's host gateway cannot deliver into a member's main chat (`deliverAgentMessage` lands in a box-local New Agent conversation), so a GrokBot member has to be woken by Marian. The wake is only a ring: local `call_send` stores the text in the bridge, and the member reads it with `call_poll`. Marian never carries the text.
- For a BellTeam target, local `call_send` uses BellTeam's UNIX socket. A reply to a BellTeam caller also uses that socket.
- Both routes use the same MCP endpoint and `session_id`.

## Flow

### GrokBot target

1. **Local** opens a session (`call_open` or `POST /v0/sessions`) and gets a `session_id`. Nothing is posted yet; `wake.status` is `skipped`.
2. **Local** `call_send` stores the text. If the member has no unread message waiting in this session, the server first POSTs `session.opened` to the switchboard with the session ID, labels, and resolved `member_agent_id`, but no message body, and **GrokBot switchboard** wakes the member with the MCP URL and `session_id`. While an earlier message is still unread, that ring is still outstanding, so further sends are stored without another ring; after `CALL_BRIDGE_RERING_SECONDS` (default 600) of an unread message the next send rings again. A needed ring that does not return HTTP 2xx, or an unset URL, returns an error without storing the message.
3. **GrokBot member** reads the waiting text with `call_poll(party="member")`; one poll returns every unread message. The stored copy includes a reply hint when `reply_required=true`. The member's `call_send(from_party="member")` stores the reply without a webhook POST. If the caller is a BellTeam Bot, the bridge also delivers that reply through BellTeam's UNIX socket.
4. Either side (or ops) calls `call_hangup`.

### BellTeam target

1. **Local** `call_open(member_system="bellteam")` creates the session without waking a Bot.
2. **Local** `call_send` sends the body to BellTeam's UNIX socket. On confirmed acceptance, the bridge stores it and returns `delivery.status=delivered`. If the connection fails before the request, it returns `error` without storing. If the request may have been accepted but its result is unavailable, it stores the message with `delivery.status=unknown`; do not resend blindly.
3. The BellTeam member's `call_send(from_party="member")` stores the reply. If `local_system="bellteam"`, the bridge also sends that reply through BellTeam's UNIX socket to the calling Bot; that delivery can likewise be `delivered`, `error`, or `unknown`. Other callers retrieve the stored reply with `call_poll`, or receive it through the Codex local MCP when configured.
4. Either side (or ops) calls `call_hangup`.

When a real MCP or REST request finds the directory unix socket unreachable (`unix socket not found`, `timed out`, or `request failed` — the socket never produced an HTTP response), the server POSTs the same switchboard webhook once:

```json
{"event":"bridge.link_down","link":"directory","detail":"unix socket not found"}
```

`link` is `directory`. At most one of these wakes is sent per 60 seconds, in-process, with no background poll. `call_directory` uses its fallback and adds a note that the GrokBot box link is down. A GrokBot-targeted `call_send` that cannot resolve the member returns that note without storing; if fallback profiles provide an ID, it still rings with `session.opened` after the `bridge.link_down` wake. A failed ring does not send `bridge.link_down`. BellTeam delivery uses its separate UNIX socket and does not ring Marian. `GROKBOT_GATEWAY_*` is not used.

`call_send(from_party="local")`は宛先の所属で配送先を選ぶ。GrokBot宛ては本文を橋に保存し、相手に未読がなければマリアンのWebhookへ本文なしの`session.opened`を送って起こす（HTTP 2xxで受付確認後に保存する）。未読が残っている間の続報は起こし直さず保存だけする。BellTeam宛てはBellTeamのUNIXソケットへ直接送る。受付結果が不明なら`delivery.status=unknown`で保存し、二重配送を避けるため自動再送しない。保存する本文には同じ`session_id`での返信案内を付けるが、BellTeamへ渡す本文には付けない。`from_party="member"`の返信は保存し、BellTeam発信者宛てだけは同じUNIXソケットへも届ける。返信不要の通知には`reply_required=false`を指定する。MCPとRESTは同じ動作を使う。返信依頼は相手への指示であり、返答を保証しない。

`call_send` の通常送信の引数例：

```json
{"session_id":"...","from_party":"local","message":"状況を教えてください"}
```

返信不要の通知の引数例：

```json
{"session_id":"...","from_party":"local","message":"共有のみです","reply_required":false}
```

REST の `POST /v0/sessions/{session_id}/messages` でも本文に
`{"from_party":"local","message":"共有のみです","reply_required":false}` を渡せる。

### Codex 親への返信自動配送

Codex から通話する端末では、ローカル MCP を登録すると `call_open` が親タスクを識別する。
ローカル MCP でも `call_open(member_system="bellteam")` でBellTeam宛てを選べる。
`member_system`の既定は`grokbot`で、遠隔MCPへ渡す。`local_system`は指定した時だけ渡し、省くと遠隔MCPが接続のトークンの所属を使う（共通トークンでは`local`）。
ローカル MCP が通話の返信を裏で取得し、Codex 親へ一通ずつ渡す。
親AI自身が `call_poll` を繰り返す必要はない。継続型の親では共通パッケージ
[aiterm-steer-delivery](https://github.com/kitepon/aiterm-steer-delivery)（Aitermと同じ配送）がCodexの公式キューへ一度だけ入れる。
進行中ならパッケージの `PostToolUse`／`Stop` hook が返信を同じターンへ差し込み、ターン終了後ならキューが次のターンとして届ける。
hook が起動する Codex は、公式 Desktop 同梱の CLI（macOS／Windows／Linux）を先に探し、無ければ通常の Codex CLI（0.154 以上）を使う。短命な `codex exec` の親では返信をブリッジの通話履歴に保持し、次に親がプロンプトを受けた時、同期 `UserPromptSubmit` hook が取得してモデルへ渡す。親 CLI の終了時に子プロセスも止まる Windows でも、この経路は常駐プロセスを必要としない。GrokBot メンバーは従来どおり公開 MCP に接続し、
返信には `from_party="member"` を使う。

対象は通常の Codex 親タスク。native sub-agent への自動配送は未対応。
有効化する端末には、まず `call-bridge` または `grokbot-bridge` を HTTP MCP として登録する（下の Codex の登録例を参照）。
`call-bridge-setup enable` を実行するシェルで、その登録の Bearer token 環境変数を利用可能にしておく。

```bash
python -m pip install git+https://github.com/kitepon/grokbot-bridge.git
npm install -g aiterm-steer-delivery@^0.1.5
call-bridge-setup enable
# Codex を完全終了して再起動
call-bridge-setup status
```

`uv tool` で入れた端末は、`pip install` の代わりに `uv tool install git+https://github.com/kitepon/grokbot-bridge.git` を使う。

既存の端末を更新する手順：

```bash
uv tool upgrade grokbot-bridge            # pip の場合は pip install -U git+https://github.com/kitepon/grokbot-bridge.git
npm install -g aiterm-steer-delivery@^0.1.5
call-bridge-setup enable
# Codex を完全終了して再起動
call-bridge-setup status
```

`enable` をやり直すと、旧版が登録した本製品の `PostToolUse`／`Stop` hook を外し、パッケージの Steer hook に入れ替える。
Windows で Steer hook を使うには PowerShell 7（`pwsh.exe`）が要る。Codex は hook を PowerShell 7 で起動する。

`enable` は既存の URL と token 環境変数名を読み、その MCP 登録をローカル MCP に切り替える。
Node 製 Codex の場合は実行中の Node の絶対パスも製品設定へ保存し、MCP の PATH が狭い環境でも返信配送用 App Server を起動する。CLI や Node を移動した後は `enable` を再実行する。
認証値は製品の state directory（既定は `~/.grokbot-bridge`）の `auth.json` に本人だけが読める権限で保存し、Codex が環境変数を継承しない場合もローカル MCP が使用する。Git や Codex 設定には書かない。`disable` はそのファイルを削除する。
ローカル MCP と返信の見張りは、問い合わせのたびに `auth.json` を読み直すので、ファイルを差し替えれば動いたまま新しいトークンへ移る。読めない状態が約30秒続いた時だけ見張りを止める。ただし環境変数（既定は `CALL_BRIDGE_TOKEN`）が設定されていればそちらが優先され、プロセスの起動時の値から変わらない。トークンを入れ替える端末では、MCP を環境変数なしで起動し、`auth.json` で渡す。
また、`codex exec` の親へ返信を渡す本製品専用の `UserPromptSubmit` hook を登録・承認し、
`aiterm-steer-delivery codex setup enable` でパッケージの Steer hook を登録する。他製品の hook は保持する。
旧版が登録した本製品の `PostToolUse`／`Stop` hook は外す。
hook を外したり置き換えたりして他の hook の位置が動く時は、Codex が位置ごとに持つ承認をその hook の新しい位置へ写す。そのため、後ろにある他製品の hook の承認が外れることはない。
`aiterm-steer-delivery` の場所（`AITERM_STEER_DELIVERY` か PATH）と Node の絶対パスは製品設定へ保存し（npm のシムではなく、同じ場所にあるパッケージの `dist/cli.js` を Node で直接起動する。`enable` と `status` は 0.1.5 以上かを確かめる）、パッケージへ渡す識別情報は state directory の `steer-profile.json` に置く。
設定変更前の `hooks.json` と `config.toml` は製品の state directory に tar で保存する。
元の HTTP MCP へ戻すときは `call-bridge-setup disable` を実行して Codex を再起動する。
`aiterm-steer-delivery` が消えていても `disable` は call-bridge の設定を元へ戻す。その場合、Steer hook を外せなかったことを結果の `warning` で知らせる。

`BRIDGE_TOKEN_MISSING` が出た場合は、既存の HTTP MCP 登録に指定した環境変数を
`enable` を実行するシェルへ渡し、`call-bridge-setup enable` を再実行する。
シェルに値があっても、起動済みの Codex MCP プロセスがその値を継承するとは限らない。
`enable` 後は Codex を完全終了して再起動する。`status` は登録と hook の状態を確認する。
結果の `steer` はパッケージの Steer hook の状態（`ready`／`restart_required`／`disabled`）。
`enable` は導入前から動く Codex の PID と生成時刻を記録し、そのプロセスが残る間は
`restart_required` を返す。`call_open` も同じ親プロセスへの配送を拒否する。
完全終了・再起動後に `call-bridge-setup status` の `ready` を確認する。

返信は `session_id` と `seq` で順番に処理する。配送結果はローカル MCP の `call_info` に
`parent_delivery` として表示する。送信結果が不明なときは自動再送せず `unknown` と記録する。
`submitted` は公式キューの受付、`injected` は `codex exec` のプロンプト hook が返信を出力したことを示し、いずれも親 AI の読了を示さない。hook がキューから
取り出し中なら `sending`、取り出しの中断や出力失敗なら `unknown` と
`CODEX_HOOK_DELIVERY_UNCONFIRMED` を表示する。hook が受け取らず待機中の Codex が
先に処理した入力の所有記録は、次の hook 実行時に整理する。
返信本文は bridge に残り、手動で `call_poll` から確認できる。
継続型 Codex のローカル MCP は再起動後に進行中の通話の受信を再開する。`codex exec` の返信は親の次のプロンプトまで通話履歴に残る。

現在の自動配送対象は Codex 親。Claude Code／Cursor の直接 HTTP 接続と手動 `call_poll` は従来どおり使える。

GrokBot宛てだけの経路：

```text
Local agent ──call_open──▶ grokbot-bridge (session only, nobody is woken)
Local agent ──call_send──▶ grokbot-bridge (text stored) ──session.opened──▶ Marian (ring only, no message body)
Grok Bot member ──call_poll──▶ grokbot-bridge (reads the text directly)
Grok Bot member ──call_send──▶ grokbot-bridge (stored for the local side)
```

`session.opened` uses schema `grokbot.call.v0` and never carries the caller's text. The webhook secret is an `Authorization` header, never a payload field.

```json
{
  "schema": "grokbot.call.v0",
  "event": "session.opened",
  "session_id": "...",
  "status": "ringing",
  "member_name": "...",
  "member_agent_id": "...",
  "local_id": "...",
  "local_label": "...",
  "purpose": null,
  "mcp_url": "https://call.kitepon.dev/mcp",
  "created_at": "..."
}
```

## MCP tools

| Tool | Role |
|------|------|
| `call_directory` | Phone book, built on that call from live seat profiles |
| `call_open` | Create session (local → member) |
| `call_send` | Send a message. GrokBot targets use Marian's webhook; BellTeam targets use its UNIX socket. A member reply is stored and is also delivered by UNIX socket when the caller is in BellTeam. Results can be `delivered`, `error`, or `unknown` for BellTeam delivery. Notices use `reply_required=false` |
| `call_poll` | Fetch new messages for your party |
| `call_list` | List / filter sessions |
| `call_hangup` | End the call |
| `call_info` | Session details, with `local_delivery` (what the local side did with each member message) |
| `call_history` | Both parties' messages in order, without marking them delivered |
| `call_receipt` | Local side reports what it did with a member message |

Also exposes a small REST surface under `/v0` (same auth) and open `/health`.

## Phone directory

Clients only call `call_directory` (or `GET /v0/directory`). The server reads Grok Bot seat profiles and, when configured, BellTeam's directory **on that request**. Changing a profile shows up on the **next** call. Each member has `system` (`grokbot` or `bellteam`) and its existing `id`. One directory can remain available when the other fails.

For GrokBot seats, the source of truth is each seat’s **profile** (`name`, `title`, `description`) — used as-is (e.g. ラピ → title `インフラ統括`, `description` → `role`). There is **no** “may call” flag. Each member built from a profile also includes `id`: the seat directory name, which is the GrokBot agent id (`profile.json` itself has no id field). A GrokBot-targeted `call_send` resolves `member_name` to that id and includes it as `member_agent_id` on the `session.opened` ring. A remote directory passes `id` or `agentId` through. A `directory.json` entry without an id cannot be relayed to GrokBot.

Lookup order:

1. **`CALL_BRIDGE_DIRECTORY_UNIX`** (preferred in production). HTTP GET over an `AF_UNIX` socket. The HTTP path is `CALL_BRIDGE_DIRECTORY_UNIX_PATH` (default `/v0/directory`). On main-server the container mounts the socket's parent directory at `/run/dirlive`, and the socket path is `/run/dirlive/dirlive.sock`.
2. **`CALL_BRIDGE_DIRECTORY_URL`** — plain HTTP GET, only if the unix socket is unset or that GET fails.
3. **Local profiles**, only after every configured remote GET has failed (or none is set): `CALL_BRIDGE_AGENTS_ROOT` if set and that directory exists, otherwise `/home/box/agent-data/agents` when the env var is unset and the path exists. This is for running call-bridge on the Grok Bot box itself. A copied agents tree on main-server is not the primary path.
4. **`directory.json`** — last-resort snapshot when the remotes failed and no local profile tree is available.

A live read reports `source: agent-profiles` and `agents_root`. A unix read also includes `directory_unix`; a URL read includes `directory_url`. The snapshot reports `source: directory.json` and does not claim to be a live profile read. If a remote was configured and failed before the snapshot was used, the response includes `directory_unix_error` and/or `directory_url_error`.

Prod wiring (operated outside this repo): the Grok Bot box runs `scripts/directory_live_server.py` on `127.0.0.1:18765`. SSH reverse-forwards that port to main-server (`ssh -R 127.0.0.1:18765:127.0.0.1:18765`). A host `socat` listens on a unix socket and dials `127.0.0.1:18765`. Compose mounts that socket's parent directory (`./dirlive:/run/dirlive`) so a recreated socket is visible without recreating the container. Set `CALL_BRIDGE_DIRECTORY_UNIX=/run/dirlive/dirlive.sock`. If `CALL_BRIDGE_DIRECTORY_UNIX_HOST` is set, point it at the directory (the default is `./dirlive`), not at the socket file.

```bash
# On the Grok Bot box (profiles live here):
python scripts/directory_live_server.py
# {"ok": true, "listen": "http://127.0.0.1:18765/v0/directory", ...}

# MCP clients — this is the whole interface
call_directory()
call_directory(query="インフラ")

# REST
curl -sS -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:18910/v0/directory?q=ラピ"
```

`scripts/sync_directory_from_agents.py` only writes an optional `directory.json` fallback. It is not how the book stays fresh, and nothing needs to run it after a profile edit.

## Quick start

```bash
cp .env.example .env
# set CALL_BRIDGE_TOKEN to a long random string

docker compose up -d --build
curl -sS http://127.0.0.1:18910/health
```

MCP endpoint: `http://127.0.0.1:18910/mcp`  
Auth: `Authorization: Bearer <token>` on `/mcp` and `/v0/*` (`/health` is open). Without any token setting, `/mcp` and `/v0/*` are also open; use this only for local development.

### 発信者の本人確認

`CALL_BRIDGE_TOKENS_FILE` を設定すると、トークンごとに所属を結び付ける。ファイルにはトークン本体ではなく SHA-256 を書く（`printf %s "$TOKEN" | sha256sum`）。

```json
{"tokens": [
  {"name": "bellteam", "sha256": "<hex>", "system": "bellteam", "caller_id_header": true},
  {"name": "grokbot", "sha256": "<hex>", "system": "grokbot"},
  {"name": "macbook", "sha256": "<hex>", "system": "local"},
  {"name": "ops", "sha256": "<hex>", "ops": true}
]}
```

- `system` 付きのトークンは、その所属の当事者としてだけ動ける。`call_open` の `local_system` は所属と一致しなければならない。
- `call_send`・`call_poll`・`call_hangup` は、通話の `local`／`member` のうち、接続の所属に当たる側だけを受け付ける。`call_info`・`call_list` も当事者の通話だけを返す。違反は `error=forbidden`（REST は 403）。
- `call_open` で `local_system` を省くと、所属に結び付いた接続ではその所属を使う。旧トークンと開発モードでは今までどおり `local`。
- `caller_id_header: true` のトークンは、基盤が `X-Call-Bridge-Caller-Id` で発信者の ID を付けられる。付いた接続は、その ID の通話だけを扱える。BellTeam は Bot ごとに付ける。BellTeam トークンでヘッダーを付けない接続は、BellTeam 全体として扱う。GrokBot は1つの接続設定を共有するので所属単位。許可のないトークンにこのヘッダーがあると 403。旧トークンに付いたヘッダーは無視する。
- `system` のない項目は全権で、`ops: true` が必要。`call_hangup(by_party="ops")` は `ops` のトークンと旧トークンだけが使える。
- `ops`・`caller_id_header` は真偽値、`name`・`sha256`・`system`・`id` は文字列で書く。型の違い、同じ `sha256` の重複、項目0件、読めないファイル、壊れた JSON は、1行のエラーで起動を止める。
- 移行中は `CALL_BRIDGE_TOKEN` も全権の旧トークンとして受け付ける。対応ファイルに項目がある間は、旧トークンが使われると5分に1回警告を記録する。`CALL_BRIDGE_TOKENS_FILE` がなければ、今までと同じ動きになる。

#### 切り替え手順

1. 所属ごとにトークンを作り、配る先と方法を決める（GrokBot 側はマリアン、BellTeam はトロニーが受け持つ）。対応ファイルにはハッシュだけを書く。発行はブリッジのホストで、リポジトリの `scripts/issue_token.py` を使う（`src/call_bridge/auth.py` と同じ規則で結果を確かめるので、リポジトリの中から動かす）。
   - トークン本体は `--out` のファイル（権限600で作る）か、`--out -` の標準出力にだけ出す。
   - 名前と `--id` は前後の空白を落とし、空なら止まる。書き込む前に、できあがる対応ファイル全体をブリッジの読み込み規則で確かめる。
   - 同じ名前や既存の `--out` は、`--replace` がない限り止まる。
   - 失敗しても、それまでのトークンは使える。対応ファイルは新しいトークンを渡し終えてから置き換え、`--out` の置き換えに失敗したら元に戻す。`--out -` は標準出力へ書き終えてから登録する。
   - 対応ファイルはロックを取り、一時ファイルから置き換えて、ディレクトリも fsync する。止まった時でも、隣に `tokens.json.lock` は作られる。

   ```sh
   mkdir -p auth
   python3 scripts/issue_token.py --tokens-file auth/tokens.json --name bellteam --system bellteam \
       --caller-id-header --out /path/to/headers --format header
   ssh main-server 'cd /home/kite/call-bridge && python3 scripts/issue_token.py \
       --tokens-file auth/tokens.json --name grokbot --system grokbot --out -' > grokbot.token
   ```

   対応ファイルは、発行してからマウントする。ファイルそのものではなく `./auth` ディレクトリを読み取り専用でマウントする（`docker-compose.yml` の例）。ファイルがないまま compose でファイルをマウントすると、Docker がそこにディレクトリを作ってしまう。ファイルのマウントだと、スクリプトが置き換えた後も古い実体を見続ける。どちらにしてもブリッジは起動時にしか読まないので、発行や入れ替えのたびに再起動する。
2. 対応ファイルを読み取り専用でマウントし、`CALL_BRIDGE_TOKENS_FILE` を設定して再起動する。旧トークンは残す。この時点では誰も新しいトークンを使っていないので、動きは変わらない。
3. 各基盤の接続設定を新しいトークンへ切り替える。BellTeam は `X-Call-Bridge-Caller-Id` を付ける版を先に入れておく。
4. 切り替えた基盤から、`local_system` を省いた発信と返信を1往復ずつ試す。
5. 切り替え前に開いた通話は、発信者の所属が `local_system='local'` のまま残っていることがある。新しいトークンでは当事者と一致せず `forbidden` になる。旧トークンを止める前に、進行中の通話を確かめて終わらせる。

   ```sh
   sqlite3 data/calls.db "SELECT session_id, local_system, local_id, member_system, member_id, status FROM sessions WHERE status IN ('ringing','open')"
   ```

6. 旧トークンの警告が出なくなったら、`CALL_BRIDGE_TOKEN` を外して再起動する。この修正より前の版のローカル MCP は、返信の見張りを始めた時のトークンを通話が終わるまで使い続ける。その見張りは旧トークンを止めると 401 で `failed` になるので、`local.sqlite` の `subscriptions` で `active` に戻し、環境変数なしで `python -m call_bridge.exec_watcher <session_id>` を起動して付け直す。`after_seq` は残るので返信は取りこぼさない。

### Client examples

**Cursor** (`~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "grokbot-bridge": {
      "url": "https://your-host.example/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      }
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add --transport http grokbot-bridge https://your-host.example/mcp \
  --header "Authorization: Bearer YOUR_TOKEN"
```

**Codex**:

```bash
export CALL_BRIDGE_TOKEN=YOUR_TOKEN
codex mcp add grokbot-bridge --url https://your-host.example/mcp \
  --bearer-token-env-var CALL_BRIDGE_TOKEN
```

**Grok Bot**: add the remote MCP URL with the same Bearer header in the account connectors.

Put a reverse proxy (Caddy, nginx, Cloudflare Tunnel, …) in front for HTTPS.

## Config

| Env | Default | Meaning |
|-----|---------|---------|
| `CALL_BRIDGE_TOKEN` | _(unset)_ | Bearer token. Set it for production; if unset, MCP and REST are open for local development |
| `CALL_BRIDGE_TOKENS_FILE` | _(unset)_ | 所属に結び付けたトークンの対応ファイル（上記）。コンテナでは読み取り専用でマウントする |
| `CALL_BRIDGE_HOST` | `0.0.0.0` | Bind host |
| `CALL_BRIDGE_PORT` | `18910` | Bind port |
| `CALL_BRIDGE_DB` | `data/calls.db` | SQLite path |
| `CALL_BRIDGE_ALLOWED_HOSTS` | `127.0.0.1:*,localhost:*` | Host header allowlist |
| `CALL_BRIDGE_ALLOWED_ORIGINS` | `http://127.0.0.1:*,http://localhost:*` | Origin allowlist |
| `CALL_BRIDGE_DIRECTORY_UNIX` | _(unset)_ | On each `call_directory`, HTTP GET over this `AF_UNIX` socket. Preferred prod source (`/run/dirlive/dirlive.sock`) |
| `CALL_BRIDGE_DIRECTORY_UNIX_PATH` | `/v0/directory` | HTTP path on that socket |
| `CALL_BRIDGE_DIRECTORY_URL` | _(unset)_ | HTTP GET used when the unix socket is unset or fails |
| `CALL_BRIDGE_DIRECTORY_URL_AUTH` | _(unset)_ | Bearer token sent on the unix and URL GETs (raw token or `Bearer …`) |
| `CALL_BRIDGE_DIRECTORY_URL_TIMEOUT` | `2.5` | Seconds for each remote GET |
| `CALL_BRIDGE_AGENTS_ROOT` | `/home/box/agent-data/agents` if that directory exists and the env var is unset | Local `profile.json` tree, used only when configured remotes fail (or none are set). When set, only that path is used |
| `CALL_BRIDGE_DIRECTORY` | `./directory.json` | Last-resort GrokBot snapshot file |
| `CALL_BRIDGE_BELLTEAM_SOCKET_HOST` | `./bellteam` | Host directory mounted at `/run/bellteam` in Compose |
| `CALL_BRIDGE_BELLTEAM_UNIX` | _(unset)_ | BellTeam socket inside the container, e.g. `/run/bellteam/bellteam.sock` |
| `CALL_BRIDGE_WAKE_WEBHOOK_URL` | _(unset)_ | GrokBot switchboard webhook. GrokBot-targeted local `call_send` posts `session.opened` as a ring when the member has nothing unread (no message body; an empty URL fails the send and does not store). A down GrokBot directory UNIX socket posts `bridge.link_down` at most once per 60 seconds |
| `CALL_BRIDGE_WAKE_WEBHOOK_AUTH` | _(unset)_ | `Authorization` header for those POSTs. Never placed in the payload |
| `CALL_BRIDGE_RERING_SECONDS` | `600` | A local `call_send` rings again when the member's oldest unread message is at least this old |
| `CALL_BRIDGE_PUBLIC_MCP_URL` | `https://call.kitepon.dev/mcp` | MCP URL included in `session.opened` |

Compose sets `extra_hosts: ["host.docker.internal:host-gateway"]` so that hostname resolves inside the container. Recreate the container after changing `.env`. On the Grok Bot box, restart `scripts/directory_live_server.py` from this revision so live directory members include `id`.

`CALL_BRIDGE_SWITCHBOARD_WEBHOOK_URL` and `CALL_BRIDGE_SWITCHBOARD_WEBHOOK_AUTH` are aliases for the webhook URL and auth value (`session.opened` and `bridge.link_down`).

## Stack

- Python 3.12 in Docker (package requires Python 3.11+) / FastMCP streamable HTTP (`mcp>=1.2,<2`)
- SQLite for sessions + messages
- Docker Compose for deploy

## License

MIT © kitepon.dev
