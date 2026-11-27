"""Make the ``planner`` directory importable when pytest is started elsewhere."""

from __future__ import annotations

import sys
from pathlib import Path

PLANNER_DIR = Path(__file__).resolve().parents[1]
if str(PLANNER_DIR) not in sys.path:
    sys.path.insert(0, str(PLANNER_DIR))
