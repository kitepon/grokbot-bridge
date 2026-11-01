#!/usr/bin/env python3
"""Run the Grok Bot box courier from a checkout of this repo.

The bridge cannot reach 127.0.0.1 on the box. This process connects outward,
claims new messages, and delivers them with sendPrompt. See README「箱の配達役」.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from call_bridge.box_courier import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
