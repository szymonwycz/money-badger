#!/usr/bin/env python3
"""Self-check for _dump_raw_pull() — run directly, no framework/fixtures.

python3 sync/test_raw_pull_dump.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "banks"))
sys.path.insert(0, str(Path(__file__).parent))
from banks.enable_banking import RAW_PULL_RETENTION_DAYS, _dump_raw_pull


def test_dump_writes_readable_json_next_to_state_file():
    tmp_root = Path(tempfile.mkdtemp())
    state_file = tmp_root / "sync" / ".bank_sync_state.json"
    state_file.parent.mkdir()

    body = {"transactions": [{"transaction_amount": {"amount": "12.34"}}]}
    _dump_raw_pull(str(state_file), "1111111111", "2026-07-14", "2026-07-14", body)

    raw_dir = tmp_root / "raw_pulls"
    files = list(raw_dir.glob("*.json"))
    assert len(files) == 1, f"expected 1 dump file, got {len(files)}"
    assert json.loads(files[0].read_text()) == body


def test_old_dumps_get_pruned():
    tmp_root = Path(tempfile.mkdtemp())
    state_file = tmp_root / "sync" / ".bank_sync_state.json"
    state_file.parent.mkdir()
    raw_dir = tmp_root / "raw_pulls"
    raw_dir.mkdir()

    stale = raw_dir / "stale.json"
    stale.write_text("{}")
    old_time = time.time() - (RAW_PULL_RETENTION_DAYS + 1) * 86400
    import os
    os.utime(stale, (old_time, old_time))

    _dump_raw_pull(str(state_file), "1111111111", "2026-07-14", "2026-07-14", {"transactions": []})

    assert not stale.exists(), f"dump older than {RAW_PULL_RETENTION_DAYS} days should have been pruned"
    assert len(list(raw_dir.glob("*.json"))) == 1, "only the fresh dump should remain"


if __name__ == "__main__":
    test_dump_writes_readable_json_next_to_state_file()
    test_old_dumps_get_pruned()
    print("OK — all raw_pull_dump tests passed")
