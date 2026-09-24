#!/usr/bin/env python3
"""Optional helper: write a directory.json fallback snapshot from local profiles.

call_directory does not use this script. Freshness is a live read on each
request (CALL_BRIDGE_DIRECTORY_UNIX, else CALL_BRIDGE_DIRECTORY_URL, else profile.json on this host). The file
this writes is only the last-resort snapshot when those sources are unavailable.
There is no periodic sync and no required post-edit push.
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

from call_bridge.directory import write_directory_snapshot  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Write an optional directory.json fallback snapshot.")
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
        help=(
            "optional scp of the fallback snapshot only "
            "(does not keep the live phone book fresh)"
        ),
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
        print(
            "note: this copies the fallback snapshot only; "
            "live directory freshness does not depend on this push",
            file=sys.stderr,
        )
        subprocess.check_call(["scp", "-o", "BatchMode=yes", str(out), args.remote])
        print(json.dumps({"ok": True, "synced_to": args.remote}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
