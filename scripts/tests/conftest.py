"""Make ``scripts/`` importable: these tests run as ``pytest scripts/tests``."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = str(Path(__file__).resolve().parents[1])
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
