"""The fuzz suite must catch every hand-crafted violation."""
import shutil
from pathlib import Path

from planner.validate.fuzz import run_all_fuzz


def test_fuzz_catches_all(tmp_path: Path):
    r = run_all_fuzz(tmp_path)
    assert r, "no fuzz cases ran"
    assert all(r.values()), f"missed cases: {[k for k, v in r.items() if not v]}"
