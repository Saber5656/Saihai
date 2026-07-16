#!/usr/bin/env python3
"""Verify the installed Codex A-prime deployment trust boundary."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from codex_main_agent_deployment import verify_main


if __name__ == "__main__":
    raise SystemExit(verify_main())
