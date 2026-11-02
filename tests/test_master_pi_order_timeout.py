import datetime
import subprocess
import sys

import master_pi


def test_order_timeout_still_marks_core_sync_and_alerts(monkeypatch, tmp_path):
    order_script = tmp_path / "order_match.py"
    order_script.touch()
    summary = tmp_path / ".last_fetch_summary.json"
    summary.write_text(
        '{"date":"%s","failed_accounts":[],"accounts":[]}'
        % datetime.date.today().isoformat()
    )

    monkeypatch.setattr(master_pi, "ORDER_MATCH_SCRIPT", order_script)
    monkeypatch.setattr(master_pi, "SUMMARY_FILE", summary)
    monkeypatch.setattr(master_pi, "start_logging", lambda *_args: tmp_path / "master.log")
    monkeypatch.setattr(master_pi, "run_fetch", lambda **_kwargs: True)
    monkeypatch.setattr(master_pi, "run_learn_from_budget", lambda **_kwargs: None)
    monkeypatch.setattr(master_pi, "run_push_actuals", lambda: None)
    monkeypatch.setattr(sys, "argv", ["master_pi.py"])

    def timeout_run(command, *, cwd, timeout):
        assert command[-1] == str(order_script)
        assert timeout == master_pi.ORDER_MATCH_TIMEOUT_SECONDS == 300
        raise subprocess.TimeoutExpired(command, timeout)

    marked = []
    messages = []
    monkeypatch.setattr(master_pi.subprocess, "run", timeout_run)
    monkeypatch.setattr(
        master_pi, "mark_success", lambda name, base: marked.append((name, base)))
    monkeypatch.setattr(master_pi, "send_telegram", messages.append)

    master_pi.main()

    assert marked == [("master_pi", master_pi.BASE_DIR)]
    assert "bank sync OK, order matching timed out" in messages
