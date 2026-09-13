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
