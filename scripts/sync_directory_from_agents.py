#!/usr/bin/env python3
"""Build phone directory from Grok Bot agent profiles and optionally push to the bridge host.

Source of truth: each seat's profile.json (name, title, description) — unchanged.
No call-permission flags.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Allow running from repo without install
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from call_bridge.directory import build_directory_doc, write_directory_snapshot  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--agents-root",
        default="/home/box/agent-data/agents",
        help="Grok Bot agents directory",
    )
    ap.add_argument(
        "--out",
        default=str(ROOT / "directory.json"),
        help="Local snapshot path",
    )
    ap.add_argument(
        "--remote",
        default="",
        help="scp target, e.g. main-server:/home/kite/call-bridge/directory.json",
    )
    args = ap.parse_args()
    root = Path(args.agents_root)
    out = Path(args.out)
    doc = write_directory_snapshot(out, root)
    print(json.dumps({"ok": True, "count": len(doc["members"]), "out": str(out)}, ensure_ascii=False))
    # sanity: ラピ title
    for m in doc["members"]:
        if m["name"] == "ラピ":
            print(f"check ラピ title={m.get('title')!r}", file=sys.stderr)
            break
    if args.remote:
        subprocess.check_call(["scp", "-o", "BatchMode=yes", str(out), args.remote])
        print(json.dumps({"ok": True, "synced_to": args.remote}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
