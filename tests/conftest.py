from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALPHAMACHINE_ROOT = ROOT / "AlphaMachine"
ALPHAMACHINE_MISSING_REASON = "AlphaMachine sources are not present in this checkout"


def pytest_ignore_collect(collection_path, config) -> bool:  # type: ignore[no-untyped-def]
    if ALPHAMACHINE_ROOT.exists():
        return False
    return collection_path.name.startswith("test_alphamachine_")
