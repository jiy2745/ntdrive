"""The generated CLI: a free-text last positional takes the rest of the line."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

import ntdrive.cli.main as main_mod
from ntdrive.cli.main import build_cli
from ntdrive.core.registry import load_builtin_tools


def test_last_free_text_positional_is_one_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
            calls.append((name, args))
            return {"ok": True}

    monkeypatch.setattr(main_mod, "client_for", lambda ctx: FakeClient())
    cli = build_cli(load_builtin_tools())
    runner = CliRunner()
    # A debugger command typed without quotes used to be "unexpected extra argument".
    result = runner.invoke(
        cli, ["--json", "kd", "exec", "win11-dev", "!process", "0", "0", "p.exe"]
    )
    assert result.exit_code == 0, result.output
    assert calls[-1] == ("kd_exec", {"vm": "win11-dev", "cmd": "!process 0 0 p.exe"})
    # Options still work after the greedy positional, and the VM filter is positional now.
    result = runner.invoke(cli, ["--json", "term", "exec", "t-1", "Get-Date", "--timeout", "5"])
    assert result.exit_code == 0, result.output
    assert calls[-1] == ("term_exec", {"session_id": "t-1", "cmd": "Get-Date", "timeout": 5.0})
    result = runner.invoke(cli, ["--json", "term", "list", "win11-dev"])
    assert result.exit_code == 0, result.output
    assert calls[-1] == ("term_list", {"vm": "win11-dev"})
    # Single-positional tools stay strict: a typo is still an error, not a mangled VM name.
    result = runner.invoke(cli, ["--json", "vm", "state", "win11-dev", "extra"])
    assert result.exit_code != 0 and "extra" in result.output.lower()
    # A path positional (file push REMOTE) is strict too: a flag written as a stray positional
    # (grant_users_rx=true instead of --grant-users-rx) errors instead of being swallowed as
    # another remote path. This misparse happened live.
    result = runner.invoke(
        cli,
        ["--json", "file", "push", "win11-dev", "C:\\a.exe", "C:\\b.exe", "grant_users_rx=true"],
    )
    assert result.exit_code != 0 and "grant_users_rx=true" in result.output
    # Spelled as the flag, it is accepted.
    result = runner.invoke(
        cli, ["--json", "file", "push", "win11-dev", "C:\\a.exe", "C:\\b.exe", "--grant-users-rx"]
    )
    assert result.exit_code == 0, result.output
    assert calls[-1] == (
        "file_push",
        {"vm": "win11-dev", "local": "C:\\a.exe", "remote": "C:\\b.exe", "grant_users_rx": True},
    )


def test_status_is_an_alias_of_sys_state() -> None:
    cli = build_cli(load_builtin_tools())
    status = cli.commands["status"]
    assert status.help == cli.commands["sys"].commands["state"].help


def test_daemon_logs_prints_the_tail(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    log = tmp_path / "daemon.out.log"
    log.write_text("first\ncli con_send_keys win11 ok (12 ms)\n", encoding="utf-8")
    monkeypatch.setattr(main_mod, "daemon_log_path", lambda: log)
    cli = build_cli(load_builtin_tools())
    result = CliRunner().invoke(cli, ["daemon", "logs", "--lines", "1"])
    assert result.exit_code == 0, result.output
    assert "con_send_keys win11 ok" in result.output
    assert "first" not in result.output  # only the last line was asked for


def test_daemon_logs_without_a_file_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setattr(main_mod, "daemon_log_path", lambda: tmp_path / "nope.log")
    cli = build_cli(load_builtin_tools())
    result = CliRunner().invoke(cli, ["daemon", "logs"])
    assert result.exit_code == 0
    assert "no daemon log yet" in result.output
