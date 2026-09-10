#!/usr/bin/env python3
"""Self-check for deploy/schedule.py — run directly, no framework.

python3 test_schedule.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "deploy"))
import schedule


def test_parse_sorts_dedupes_and_pads():
    assert schedule.parse_times("15:15, 9:05,11:15,09:05") == [(9, 5), (11, 15), (15, 15)]


def test_parse_rejects_nonsense():
    for bad in ("", "10.30", "24:00", "10:60", "morning", "10:"):
        try:
            schedule.parse_times(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected")


def test_one_pass_leaves_retries_across_the_day():
    times = schedule.parse_times("10:30")
    assert schedule.watchdog_hours(times, 4) == [11, 17, 23]


def test_three_passes_leave_one_late_retry():
    times = schedule.parse_times("11:15,15:15,17:45")
    assert schedule.watchdog_hours(times, 4) == [23]


def test_no_attempts_left_means_no_watchdog():
    """Every attempt spent on a scheduled pass — the watchdog timer is then not
    installed at all, so it must ask for no slots rather than a stray one."""
    times = schedule.parse_times("11:15,15:15,17:45")
    assert schedule.watchdog_hours(times, 3) == []
    assert schedule.watchdog_hours(times, 2) == []


def test_late_pass_still_gets_a_slot_before_midnight():
    times = schedule.parse_times("23:30")
    assert schedule.watchdog_hours(times, 2) == [23]


def test_rendered_lines_carry_the_last_minute():
    assert schedule.main(["", "sync", "11:15,17:45"]) == (
        "OnCalendar=*-*-* 11:15:00\nOnCalendar=*-*-* 17:45:00")
    assert schedule.main(["", "watchdog", "11:15,17:45", "3"]) == "OnCalendar=*-*-* 23:45:00"
    assert schedule.main(["", "normalize", " 9:05 ,11:15"]) == "09:05,11:15"


if __name__ == "__main__":
    test_parse_sorts_dedupes_and_pads()
    test_parse_rejects_nonsense()
    test_one_pass_leaves_retries_across_the_day()
    test_three_passes_leave_one_late_retry()
    test_no_attempts_left_means_no_watchdog()
    test_late_pass_still_gets_a_slot_before_midnight()
    test_rendered_lines_carry_the_last_minute()
    print("OK — schedule rendering tests passed")
