#!/usr/bin/env python3
"""Stable source-checkout entry for Bridge-owned connection export."""

import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from call_bridge.connection import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
