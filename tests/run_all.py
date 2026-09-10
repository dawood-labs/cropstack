"""Run every test module. `python tests/run_all.py` from anywhere.

Offline: nothing here touches the catalogue or downloads imagery. The real-data
verification lives in `harness/` and is run separately, because it needs the test AOIs
and network.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from checks import run_modules  # noqa: E402

MODULES = [
    "test_qc_retention",
    "test_acquisition_reporting",
    "test_pool_recycle",
]

if __name__ == "__main__":
    raise SystemExit(run_modules(MODULES))
