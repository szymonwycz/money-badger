#!/usr/bin/env python3
"""Turn the schedule answers install.sh collects into systemd OnCalendar lines.

    schedule.py normalize "11:15, 15:15,17:45"   -> 11:15,15:15,17:45
    schedule.py sync      "11:15,15:15,17:45"    -> three OnCalendar lines
    schedule.py watchdog  "11:15,15:15,17:45" 4  -> the retry lines (here: one)

Lives outside install.sh so it can be tested — see tests/test_schedule.py.
Bad input exits 1 with the reason on stderr; install.sh turns that into a die().
"""
import sys


def parse_times(raw: str):
    """"11:15, 9:05" -> [(9, 5), (11, 15)], sorted and de-duplicated."""
    times = []
    for chunk in raw.split(","):
        t = chunk.strip()
        if not t:
            continue
        hh, sep, mm = t.partition(":")
        if not sep or not hh.isdigit() or not mm.isdigit():
            raise ValueError(f"Use HH:MM, e.g. 10:30 — not {t!r}.")
        h, m = int(hh), int(mm)
        if h > 23 or m > 59:
            raise ValueError(f"{t!r} is not a time of day.")
        times.append((h, m))
    if not times:
        raise ValueError("Give at least one time, e.g. 10:30.")
    return sorted(set(times))


def watchdog_hours(times, total_attempts: int):
    """Hours for the retry timer: spread between an hour after the last pass and
    the last slot of the day, both included, so there is always one more attempt
    before midnight. Empty when the passes already use up the attempts."""
    retries = total_attempts - len(times)
    if retries <= 0:
        return []
    first, last = min(times[-1][0] + 1, 23), 23
    if retries == 1:
        hours = [last]
    else:
        hours = [round(first + i * (last - first) / (retries - 1)) for i in range(retries)]
    return sorted(set(hours))


def _lines(pairs):
    return "\n".join(f"OnCalendar=*-*-* {h:02d}:{m:02d}:00" for h, m in pairs)


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    try:
        times = parse_times(argv[2])
        if mode == "normalize":
            return ",".join(f"{h:02d}:{m:02d}" for h, m in times)
        if mode == "sync":
            return _lines(times)
        if mode == "watchdog":
            minute = times[-1][1]
            return _lines((h, minute) for h in watchdog_hours(times, int(argv[3])))
    except (IndexError, ValueError) as e:
        print(e if isinstance(e, ValueError) else "usage: schedule.py MODE TIMES [ATTEMPTS]",
              file=sys.stderr)
        return None
    print(f"unknown mode {mode!r}", file=sys.stderr)
    return None


if __name__ == "__main__":
    out = main(sys.argv)
    if out is None:
        sys.exit(1)
    print(out)
