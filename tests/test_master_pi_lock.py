#!/usr/bin/env python3
"""Self-check for master_pi.acquire_lock() — run directly, no framework.

    python3 tests/test_master_pi_lock.py

Both sync.timer and sync-watchdog.timer carry Persistent=true, so a machine
that was off at the scheduled time replays both catch-ups in the same second
and the watchdog starts a second pipeline while the first is still running:
a double bank fetch against one daily quota, and two writers on the
order-matching state.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import master_pi


def _use_tmp_lock():
    master_pi.LOCK_FILE = Path(tempfile.mkdtemp()) / ".master_pi.lock"


def test_second_run_is_refused_while_first_holds_the_lock():
    _use_tmp_lock()
    first = master_pi.acquire_lock()
    assert first is not None, "the first run did not get the lock"
    assert master_pi.acquire_lock() is None, "a second run started during the first"
    first.close()


def test_lock_is_free_again_after_the_run_ends():
    _use_tmp_lock()
    first = master_pi.acquire_lock()
    assert first is not None
    first.close()
    again = master_pi.acquire_lock()
    assert again is not None, "the lock was not released when the handle closed"
    again.close()


if __name__ == "__main__":
    test_second_run_is_refused_while_first_holds_the_lock()
    test_lock_is_free_again_after_the_run_ends()
    print("OK — master_pi lock tests passed")
