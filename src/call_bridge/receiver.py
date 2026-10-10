"""常駐の受け取り係。Codex が1つも動いていない時も、この端末で開いた通話の返信を受け取る。

ログイン中だけ動く。macOS は LaunchAgent、Linux は systemd のユーザー単位、Windows はログオン時のタスク。
画面のある session で動かすのは、寝ている Codex の会話を起こす口と、席を立てる Aiterm がそこでしか使えないから。
"""

from __future__ import annotations

import asyncio
import logging
import os
import plistlib
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .codex_delivery import DeliveryError, state_root

log = logging.getLogger("call_bridge.receiver")
LABEL = "dev.kitepon.call-bridge.receiver"
UNIT = "call-bridge-receiver.service"
TASK = "call-bridge-receiver"
# 受け取り係が動き出した後に開かれた通話を見つける間隔。
_SCAN_SECONDS = 5.0
# 登録して起こした後、動き出したかを見る長さ。
_START_WAIT_SECONDS = 10.0
# 受け取り係が開けるファイルの数。通話1本につき鍵1つと接続1つ、席を立てる時は子の process の管が加わる。
_FILE_LIMIT = 4096


def _claim() -> int | None:
    """受け取り係は端末に1つ。鍵を取れなければ、もう動いている。"""
    fd = os.open(state_root() / "receiver.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def raise_file_limit() -> None:
    """開けるファイルの数の上限を上げる。macOS の常駐は 256 で始まり、通話の数と席を立てる時の子で足りなくなる。

    2026-10-10 に Mac で、通話 48 本を見ながら席を4つ同時に立てた時に、控えを開けず落ちた。
    """
    try:
        import resource
    except ImportError:  # Windows にはこの上限が無い
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = _FILE_LIMIT if hard == resource.RLIM_INFINITY else min(_FILE_LIMIT, hard)
    if soft < wanted:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
        except (ValueError, OSError) as exc:
            log.warning("file limit stays at %s: %s", soft, exc)


async def run(scans: int | None = None) -> None:
    """生きている通話を見張り続ける。scans は試験用（見回る回数）。"""
    from . import local

    store = local.LocalStore(state_root())
    watchers = local.Watchers(store, receiver=True)
    try:
        while scans is None or scans > 0:
            try:
                for session_id in store.active():
                    if store.subscription(session_id)["delivery_mode"] != "exec":
                        watchers.start(session_id)
            except sqlite3.Error as exc:
                # 控えを一時的に読めない時に、見張っている通話ごと落ちない。次の見回りで読み直す。
                log.warning("subscriptions were not read this round: %s", exc)
            if scans is not None:
                scans -= 1
            await asyncio.sleep(_SCAN_SECONDS)
    finally:
        await watchers.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    fd = _claim()
    if fd is None:
        log.info("receiver is already running")
        return
    raise_file_limit()
    try:
        asyncio.run(run())
    finally:
        os.close(fd)


# ---------- 登録 ----------


def _program(windows: bool = os.name == "nt") -> list[str]:
    """受け取り係を起こす命令。Windows は窓を出さない pythonw を使う。"""
    python = sys.executable
    if windows:
        windowless = os.path.join(os.path.dirname(python), "pythonw.exe")
        python = windowless if os.path.isfile(windowless) else python
    return [python, "-m", "call_bridge.receiver"]


def _environment() -> dict[str, str]:
    """登録した時の PATH を渡す。常駐の環境の PATH は細く、node・tmux・各 CLI が見つからない。"""
    env = {"PATH": os.environ.get("PATH", "")}
    for key in ("CALL_BRIDGE_STATE", "CODEX_HOME", "CALL_BRIDGE_MCP_URL"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def launch_agent() -> tuple[Path, bytes]:
    file = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    log_file = str(state_root() / "receiver.log")
    return file, plistlib.dumps({
        "Label": LABEL, "ProgramArguments": _program(), "EnvironmentVariables": _environment(),
        "RunAtLoad": True, "KeepAlive": True, "ProcessType": "Background",
        # 画面のある session でだけ動かす。ssh の session では、会話を開く口が人の画面へ届かない。
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": log_file, "StandardErrorPath": log_file,
    })


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def systemd_unit() -> tuple[Path, str]:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    lines = ["[Unit]", "Description=call-bridge receiver (replies for local seats)", "",
             "[Service]", "ExecStart=" + " ".join(_systemd_quote(part) for part in _program()),
             *[f"Environment={_systemd_quote(f'{key}={value}')}" for key, value in _environment().items()],
             "Restart=on-failure", "RestartSec=30", "",
             "[Install]", "WantedBy=default.target", ""]
    return base / "systemd" / "user" / UNIT, "\n".join(lines)


def windows_task(user: str) -> str:
    """ログオン時に、その利用者の画面のある session で起こすタスク（タスク スケジューラの XML）。"""
    program = _program(windows=True)
    arguments = subprocess.list2cmdline(program[1:])
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>call-bridge receiver (replies for local seats)</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
    <Hidden>true</Hidden>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(program[0])}</Command><Arguments>{escape(arguments)}</Arguments></Exec></Actions>
</Task>
"""


def _run(command: list[str], accept: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeliveryError("RECEIVER_SETUP_FAILED", f"{command[0]} を実行できません: {exc}") from exc
    if result.returncode not in accept:
        detail = (result.stderr or result.stdout).strip()[-300:]
        raise DeliveryError("RECEIVER_SETUP_FAILED", f"{' '.join(command[:3])}: {detail}")
    return result


def _windows_user() -> str:
    domain, name = os.environ.get("USERDOMAIN"), os.environ.get("USERNAME")
    if not name:
        raise DeliveryError("RECEIVER_SETUP_FAILED", "利用者名を確認できません")
    return f"{domain}\\{name}" if domain else name


def install(platform: str = sys.platform) -> dict[str, Any]:
    """登録して、今すぐ起こす。もう登録があれば、今の命令と PATH で登録し直す。"""
    if platform == "darwin":
        file, body = launch_agent()
        file.parent.mkdir(parents=True, exist_ok=True)
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", f"{domain}/{LABEL}"], accept=(0, 3, 5, 36, 113))
        # bootout は、止め終わる前に返る。残っている間の bootstrap は「5: Input/output error」で断られる
        # （2026-10-10 に Mac で、登録し直した時に起きた）。居なくなるのを待ってから登録する。
        deadline = time.monotonic() + _START_WAIT_SECONDS
        while _run(["launchctl", "print", f"{domain}/{LABEL}"], accept=(0, 113)).returncode == 0:
            if time.monotonic() >= deadline:
                raise DeliveryError("RECEIVER_SETUP_FAILED", "前の受け取り係が止まり終わりません")
            time.sleep(0.5)
        file.write_bytes(body)
        _run(["launchctl", "bootstrap", domain, str(file)])
    elif platform == "win32":
        task = state_root() / "receiver-task.xml"
        task.write_text(windows_task(_windows_user()), encoding="utf-16")
        _run(["schtasks", "/Create", "/TN", TASK, "/XML", str(task), "/F"])
        _run(["schtasks", "/Run", "/TN", TASK])
    elif platform.startswith("linux"):
        file, body = systemd_unit()
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(body, encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", UNIT])
        _run(["systemctl", "--user", "restart", UNIT])
    else:
        raise DeliveryError("RECEIVER_UNSUPPORTED", f"{platform} の常駐の登録には対応していません")
    # 起こした直後は、まだ鍵を取っていない。動き出すのを少し待ってから答える。
    deadline = time.monotonic() + _START_WAIT_SECONDS
    while not (current := state(platform))["running"] and time.monotonic() < deadline:
        time.sleep(0.5)
    return current


def uninstall(platform: str = sys.platform) -> dict[str, Any]:
    if platform == "darwin":
        file, _body = launch_agent()
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], accept=(0, 3, 5, 36, 113))
        file.unlink(missing_ok=True)
    elif platform == "win32":
        _run(["schtasks", "/End", "/TN", TASK], accept=(0, 1))
        _run(["schtasks", "/Delete", "/TN", TASK, "/F"], accept=(0, 1))
        (state_root() / "receiver-task.xml").unlink(missing_ok=True)
    elif platform.startswith("linux"):
        file, _body = systemd_unit()
        if file.is_file():
            _run(["systemctl", "--user", "disable", "--now", UNIT], accept=(0, 1, 5))
            file.unlink()
            _run(["systemctl", "--user", "daemon-reload"])
    else:
        raise DeliveryError("RECEIVER_UNSUPPORTED", f"{platform} の常駐の登録には対応していません")
    return state(platform)


def state(platform: str = sys.platform) -> dict[str, Any]:
    """registered＝登録があるか、running＝今動いているか（鍵を持つ process が居るか）。"""
    if platform == "darwin":
        registered = launch_agent()[0].is_file()
    elif platform == "win32":
        registered = _run(["schtasks", "/Query", "/TN", TASK], accept=(0, 1)).returncode == 0
    elif platform.startswith("linux"):
        registered = systemd_unit()[0].is_file()
    else:
        return {"registered": False, "running": False, "detail": f"{platform} は対象外です"}
    fd = _claim()
    if fd is not None:
        os.close(fd)
    return {"registered": registered, "running": fd is None}


if __name__ == "__main__":
    main()
