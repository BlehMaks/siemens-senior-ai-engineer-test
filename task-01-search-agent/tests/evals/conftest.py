from __future__ import annotations

import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[2] / "evals"
sys.path.insert(0, str(EVAL_DIR))
