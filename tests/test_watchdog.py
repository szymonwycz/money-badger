from types import SimpleNamespace

import pytest

import watchdog


@pytest.mark.parametrize(
    "returncode, expected_fragment",
    [
        (0, None),
        (1, "exited with status 1"),
        (75, "has been running since 2026-10-29T15:15:02+01:00"),
    ],
)
def test_watchdog_handles_master_exit_codes(
        monkeypatch, tmp_path, returncode, expected_fragment):
    marker = tmp_path / "missing-marker"
    lock_file = tmp_path / ".master_pi.lock"
    lock_file.write_text(
        '{"pid":1837340,"started_at":"2026-10-29T15:15:02+01:00"}'
    )
    messages = []

    monkeypatch.setattr(watchdog, "BASE_DIR", tmp_path)
    monkeypatch.setattr(watchdog, "MARKER", marker)
    monkeypatch.setattr(watchdog, "LOCK_FILE", lock_file)
    monkeypatch.setattr(watchdog, "start_logging", lambda *_args: tmp_path / "watchdog.log")
    monkeypatch.setattr(
        watchdog.subprocess,
        "run",
        lambda _command: SimpleNamespace(returncode=returncode),
    )
    monkeypatch.setattr(watchdog, "send_telegram", messages.append)

    watchdog.main()

    if expected_fragment is None:
        assert messages == []
    else:
        assert len(messages) == 1
        assert expected_fragment in messages[0]
