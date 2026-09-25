# Codex 親プロセスの識別

- 出典: [psutil API リファレンス](https://psutil.readthedocs.io/en/latest/#psutil.Process.create_time)
- 取得日: 2026-09-25
- 確度: 一次資料と macOS での実測

`psutil.Process.create_time()` はプロセスの生成時刻を返す。PID だけでは再利用を区別できないため、`stale_processes` には PID と生成時刻の組を記録する。`Process.parents()` で呼び出し元から Codex 親までたどり、導入前に起動した親か確認する。
