"""`ntdrive verify` proves a VM end to end and says ALL SET, or names the first thing to fix."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

import ntdrive.cli.verify as verify_mod
from ntdrive.cli.main import build_cli
from ntdrive.core.registry import load_builtin_tools
from ntdrive.core.service import NtDriveService

from .conftest import FakeFirewall


class _LocalClient:
    """Stand-in for DaemonClient that calls the service on the test loop (the CLI runs in a thread)."""

    def __init__(self, service: NtDriveService, loop: asyncio.AbstractEventLoop) -> None:
        self.service = service
        self.loop = loop

    def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        return asyncio.run_coroutine_threadsafe(
            self.service.call(name, args or {}, caller="cli"), self.loop
        ).result(30)


async def _verify(
    service: NtDriveService, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> Result:
    loop = asyncio.get_running_loop()
    # sys_health checks that the host binaries exist. The fakes never run them.
    for binary in (service.config.host.kd, service.config.host.kdnet, service.config.host.vmrun):
        Path(binary).touch()
    monkeypatch.setattr("ntdrive.core.tools.sys._udp_port_free", lambda port: True)
    monkeypatch.setattr(verify_mod, "connect", lambda *a, **k: _LocalClient(service, loop))
    runner = CliRunner()
    return await loop.run_in_executor(
        None, lambda: runner.invoke(build_cli(load_builtin_tools()), args)
    )


async def test_verify_says_all_set_and_leaves_nothing_attached(
    service: NtDriveService, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _verify(service, monkeypatch, ["verify", "win11-dev"])
    assert result.exit_code == 0, result.output
    for line in ("ok    config", "ok    power", "ok    ssh", "ok    firewall", "ok    debugger"):
        assert line in result.output
    assert result.output.strip().endswith("all worked.") and "ALL SET: win11-dev" in result.output
    # The debugger verify attached is detached again, so the guest is not left frozen.
    assert (await service.call("kd_state", {"vm": "win11-dev"}))["state"] == "detached"

    as_json = await _verify(service, monkeypatch, ["--json", "verify"])
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert payload["ready"] is True and [c["check"] for c in payload["vms"]["win11-dev"]] == [
        "config",
        "power",
        "ssh",
        "firewall",
        "debugger",
    ]


async def test_verify_stops_at_the_first_failure_with_the_fix(
    service: NtDriveService, fake_firewall: FakeFirewall, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_firewall.allow = False
    fake_firewall.block_rules = ["Windows Kernel Debugger"]
    result = await _verify(service, monkeypatch, ["verify", "win11-dev"])
    assert result.exit_code == 1
    assert (
        "FAIL  firewall: inbound Block rules for kd.exe: Windows Kernel Debugger" in result.output
    )
    assert "fix: run scripts\\setup-host.cmd" in result.output
    assert "ok    debugger" not in result.output and "NOT READY: win11-dev" in result.output

    service.config.vms.clear()
    empty = await _verify(service, monkeypatch, ["verify"])
    assert empty.exit_code == 1 and "no VM is configured" in empty.output
