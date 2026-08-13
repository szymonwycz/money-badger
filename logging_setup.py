"""Shared logging for the MoneyPro pipeline scripts (master, sync, push_actuals, import).

Tees stdout/stderr to logs/{name}_{date}.log while leaving console output unchanged,
so the same call works for manual terminal runs and for cron/launchd runs. Each
script keeps its own dated log file, which is easier to grep/debug than one giant
combined file. Old logs are pruned automatically.
"""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

LOG_RETENTION_DAYS = 90


class _TimestampedFileTee:
    def __init__(self, console_stream, log_fh):
        self.console = console_stream
        self.log_fh = log_fh
        self._line_start = True

    def write(self, data):
        self.console.write(data)
        for chunk in data.splitlines(keepends=True):
            if self._line_start:
                self.log_fh.write(f"[{datetime.now():%H:%M:%S}] ")
            self.log_fh.write(chunk)
            self._line_start = chunk.endswith("\n")
        self.log_fh.flush()

    def flush(self):
        self.console.flush()
        self.log_fh.flush()


def _cleanup_old_logs(log_dir: Path):
    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    for pattern in ("*.log", ".success_*"):
        for f in log_dir.glob(pattern):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass


def start_logging(name: str, base_dir) -> Path:
    """Call once near the top of a script's entry point (main()/__main__ block).
    Returns the path of the log file being written to."""
    log_dir = Path(base_dir) / "logs"
    log_dir.mkdir(exist_ok=True)
    _cleanup_old_logs(log_dir)

    log_path = log_dir / f"{name}_{datetime.now():%Y-%m-%d}.log"
    log_fh = open(log_path, "a", encoding="utf-8")
    log_fh.write(f"\n{'─' * 70}\n[{datetime.now():%Y-%m-%d %H:%M:%S}] START {name} "
                 f"(pid={os.getpid()}, args={sys.argv[1:]})\n{'─' * 70}\n")
    log_fh.flush()

    sys.stdout = _TimestampedFileTee(sys.__stdout__, log_fh)
    sys.stderr = _TimestampedFileTee(sys.__stderr__, log_fh)
    return log_path


def mark_success(name: str, base_dir):
    """Call at the very end of a run that completed without error. Watchdog
    crons check for today's marker and re-run the script if it's missing —
    this is the signal they check, not the .log file (which exists from the
    moment the script starts, success or not)."""
    log_dir = Path(base_dir) / "logs"
    log_dir.mkdir(exist_ok=True)
    marker = log_dir / f".success_{name}_{datetime.now():%Y-%m-%d}"
    marker.write_text(datetime.now().isoformat())
