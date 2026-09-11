"""ensure_daemon: config discovery on the client side and the restart of a config-less daemon."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from ntdrive.daemon import lifecycle
from ntdrive.daemon.lifecycle import DaemonInfo
from ntdrive.errors import INVALID_ARGS, NtDriveError


def _info(config_path: str) -> DaemonInfo:
    return DaemonInfo(
        host="127.0.0.1",
        port=1,
        pid=42,
        token="t",
        version=lifecycle.__version__,
        started_at=0.0,
        config_path=config_path,
    )


class Harness:
    """Fakes for the process side of lifecycle, so the decision logic runs without a daemon."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, running: DaemonInfo | None, config: Path | None
    ) -> None:
        self.running = running
        self.spawned: list[str | None] = []
        self.stopped: list[int] = []
        monkeypatch.setattr(lifecycle, "read_info", lambda: self.running)
        monkeypatch.setattr(lifecycle, "pid_alive", lambda pid: self.running is not None)
        monkeypatch.setattr(
            lifecycle, "probe_health", lambda info, timeout=2.0: {"version": info.version}
        )
        monkeypatch.setattr(
            lifecycle,
            "find_config_path",
            lambda explicit=None: Path(explicit) if explicit else config,
        )
        monkeypatch.setattr(lifecycle, "stop_daemon", self._stop)
        monkeypatch.setattr(lifecycle, "spawn_daemon", self._spawn)
        monkeypatch.setattr(lifecycle, "remove_info", lambda: None)
        monkeypatch.setattr(lifecycle.time, "sleep", lambda seconds: None)

    def _stop(self, info: DaemonInfo, timeout: float = 15.0) -> bool:
        self.stopped.append(info.pid)
        self.running = None
        return True

    def _spawn(self, config_path: str | None = None) -> Any:
        self.spawned.append(config_path)
        self.running = _info(config_path or "")
        return None


def test_a_running_daemon_with_a_config_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "vms.yaml"
    cfg.write_text("vms: {}\n")
    h = Harness(monkeypatch, _info(str(cfg)), cfg)
    info = lifecycle.ensure_daemon()
    assert info.pid == 42 and h.spawned == [] and h.stopped == []


def test_a_config_less_daemon_is_restarted_once_a_config_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Started before vms.yaml existed: it holds no VMs and no sessions, so a restart loses nothing.
    cfg = tmp_path / "vms.yaml"
    cfg.write_text("vms: {}\n")
    h = Harness(monkeypatch, _info(""), cfg)
    info = lifecycle.ensure_daemon()
    assert h.stopped == [42] and h.spawned == [str(cfg.resolve())]
    assert info.config_path == str(cfg.resolve())


def test_a_config_less_daemon_stays_while_there_is_still_no_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(monkeypatch, _info(""), None)
    info = lifecycle.ensure_daemon()
    assert info.pid == 42 and h.spawned == [] and h.stopped == []


def test_autostart_hands_the_resolved_config_to_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "vms.yaml"
    cfg.write_text("vms: {}\n")
    h = Harness(monkeypatch, None, cfg)
    info = lifecycle.ensure_daemon()
    assert h.spawned == [str(cfg.resolve())] and info.config_path == str(cfg.resolve())


def test_an_explicit_config_path_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other.yaml"
    other.write_text("vms: {}\n")
    h = Harness(monkeypatch, None, None)
    lifecycle.ensure_daemon(str(other))
    assert h.spawned == [str(other.resolve())]


def test_no_autostart_leaves_a_config_less_daemon_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "vms.yaml"
    cfg.write_text("vms: {}\n")
    h = Harness(monkeypatch, _info(""), cfg)
    info = lifecycle.ensure_daemon(autostart=False)
    assert info.pid == 42 and h.stopped == [] and h.spawned == []


def test_a_daemon_on_another_config_is_reported_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    theirs = tmp_path / "a.yaml"
    theirs.write_text("vms: {}\n")
    mine = tmp_path / "b.yaml"
    mine.write_text("vms: {}\n")
    h = Harness(monkeypatch, _info(str(theirs)), None)
    with pytest.raises(NtDriveError) as exc:
        lifecycle.ensure_daemon(str(mine))
    assert exc.value.code == INVALID_ARGS and "daemon restart" in exc.value.hint
    assert h.stopped == [] and h.spawned == []
    # The same file spelled differently is the same config.
    spelled = str(theirs).upper() if sys.platform == "win32" else str(theirs)
    assert lifecycle.ensure_daemon(spelled).pid == 42


def test_restart_daemon_stops_and_starts_on_the_resolved_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "vms.yaml"
    cfg.write_text("vms: {}\n")
    h = Harness(monkeypatch, _info(str(cfg)), cfg)
    info = lifecycle.restart_daemon()
    assert h.stopped == [42] and h.spawned == [str(cfg.resolve())]
    assert info.config_path == str(cfg.resolve())
