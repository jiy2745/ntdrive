"""kd_setup_host on the net transport: read the host firewall, repair it through one UAC prompt."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from ntdrive.core.service import NtDriveService
from ntdrive.errors import BACKEND_ERROR, NtDriveError
from ntdrive.kd import firewall
from ntdrive.kd.firewall import (
    ALLOW_RULE,
    MANUAL_FIREWALL_HINT,
    FirewallStatus,
    check_script,
    firewall_status,
    fix_script,
    parse_check_output,
)

from .conftest import FakeFirewall


async def test_kd_setup_host_repairs_the_kdnet_firewall(
    service: NtDriveService, fake_firewall: FakeFirewall, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ntdrive.core.tools.sys._udp_port_free", lambda port: True)
    cfg = service.config.vms["win11-dev"]
    cfg.kd_transport = "net"
    fake_firewall.allow = False
    fake_firewall.block_rules = ["Windows Kernel Debugger"]

    health = await service.call("sys_health", {})
    assert health["kdnet_firewall"]["ok"] is False
    assert health["kdnet_firewall"]["block_rules"] == ["Windows Kernel Debugger"]
    vm = health["vms"][0]
    assert vm["kdnet_port"]["firewall_ok"] is False
    assert any("host firewall blocks KDNET" in i and "kd_setup_host" in i for i in vm["issues"])
    assert fake_firewall.checks == 1  # one read per sys_health, not one per VM probe

    # Look only: nothing changes and no prompt is shown.
    done = await service.call("kd_setup_host", {"vm": "win11-dev", "fix_firewall": False})
    assert done["changed"] is False and done["firewall"]["ok"] is False
    assert fake_firewall.fixes == 0 and "setup-host.ps1" in done["next"]

    done = await service.call("kd_setup_host", {"vm": "win11-dev"})
    assert done["changed"] is True and done["firewall"]["ok"] is True
    assert done["firewall"]["block_rules"] == [] and fake_firewall.fixes == 1
    assert "kd_setup_guest" in done["next"]

    health = await service.call("sys_health", {})
    assert health["kdnet_firewall"]["ok"] is True
    assert health["vms"][0]["issues"] == []

    # Already fine: nothing to repair, no prompt.
    done = await service.call("kd_setup_host", {"vm": "win11-dev"})
    assert done["changed"] is False and fake_firewall.fixes == 1


async def test_firewall_status_is_cached_between_calls(
    service: NtDriveService, fake_firewall: FakeFirewall, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ntdrive.core.tools.sys._udp_port_free", lambda port: True)
    service.config.vms["win11-dev"].kd_transport = "net"
    await service.call("sys_health", {})
    await service.call("sys_health", {})
    await service.call("kd_setup_host", {"vm": "win11-dev", "fix_firewall": False})
    assert fake_firewall.checks == 1

    # An unreadable answer is not kept, so the next call reads again.
    service._firewall_cache = None  # noqa: SLF001
    fake_firewall.unreadable = "boom"
    await service.call("sys_health", {})
    await service.call("sys_health", {})
    assert fake_firewall.checks == 3

    # A repair replaces whatever was cached.
    service._firewall_cache = None  # noqa: SLF001
    fake_firewall.unreadable = ""
    fake_firewall.allow = False
    await service.call("kd_setup_host", {"vm": "win11-dev"})
    health = await service.call("sys_health", {})
    assert health["kdnet_firewall"]["ok"] is True and fake_firewall.checks == 5


async def test_kd_setup_host_reports_a_refused_uac_prompt(
    service: NtDriveService, fake_firewall: FakeFirewall
) -> None:
    service.config.vms["win11-dev"].kd_transport = "net"
    fake_firewall.allow = False
    fake_firewall.refuse = True
    with pytest.raises(NtDriveError) as exc:
        await service.call("kd_setup_host", {"vm": "win11-dev"})
    assert exc.value.code == BACKEND_ERROR and "setup-host.ps1" in exc.value.hint


async def test_kd_setup_host_does_not_guess_when_the_firewall_is_unreadable(
    service: NtDriveService, fake_firewall: FakeFirewall, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ntdrive.core.tools.sys._udp_port_free", lambda port: True)
    service.config.vms["win11-dev"].kd_transport = "net"
    fake_firewall.unreadable = "powershell exit 1"
    health = await service.call("sys_health", {})
    assert health["kdnet_firewall"]["checked"] is False
    assert health["vms"][0]["kdnet_port"]["firewall_ok"] is None
    assert any("could not be read" in i for i in health["vms"][0]["issues"])
    # Looking only: the hint must name the manual route, not a repair that cannot run.
    done = await service.call("kd_setup_host", {"vm": "win11-dev", "fix_firewall": False})
    assert done["next"] == MANUAL_FIREWALL_HINT
    with pytest.raises(NtDriveError) as exc:
        await service.call("kd_setup_host", {"vm": "win11-dev"})
    assert exc.value.code == BACKEND_ERROR and fake_firewall.fixes == 0


async def test_serial_transport_never_reads_the_firewall(
    service: NtDriveService, fake_firewall: FakeFirewall
) -> None:
    cfg = service.config.vms["win11-dev"]
    cfg.kd_transport = "serial"
    cfg.kdnet.key = ""
    health = await service.call("sys_health", {})
    assert health["kdnet_firewall"] is None and fake_firewall.checks == 0
    await service.call("kd_setup_host", {"vm": "win11-dev"})
    assert fake_firewall.checks == 0 and fake_firewall.fixes == 0


def test_firewall_scripts_name_the_debugger_and_the_allow_rule(tmp_path: Path) -> None:
    kd = str(tmp_path / "it's here" / "kd.exe")
    check = check_script(kd)
    fix = fix_script(kd)
    # A single quote in the path is doubled, the PowerShell way, so the literal stays intact.
    assert "it''s here" in check and "it''s here" in fix
    assert "ConvertTo-Json" in check and "New-NetFirewallRule" not in check
    # The fix recreates the Allow rule instead of enabling one by name, which could point at
    # another kd.exe.
    assert f"Remove-NetFirewallRule -DisplayName '{ALLOW_RULE}'" in fix
    assert f"New-NetFirewallRule -DisplayName '{ALLOW_RULE}'" in fix and "-Program $kd" in fix
    assert "Enable-NetFirewallRule" not in fix


def test_parse_check_output_handles_every_shape() -> None:
    ok = parse_check_output(0, 'WARNING: noise\n{"allow":true,"block":[]}\n')
    assert ok.ok and ok.checked
    one = parse_check_output(0, '{"allow":false,"block":"Windows Kernel Debugger"}')
    assert one.block_rules == ["Windows Kernel Debugger"] and not one.ok
    two = parse_check_output(0, '{"allow":true,"block":["a","b"]}')
    assert two.block_rules == ["a", "b"] and not two.ok
    bad = parse_check_output(1, "Get-NetFirewallApplicationFilter : access denied")
    assert not bad.checked and "access denied" in bad.error
    junk = parse_check_output(0, "{not json")
    assert not junk.checked and "unexpected output" in junk.error
    assert bad.hint == MANUAL_FIREWALL_HINT and one.hint != MANUAL_FIREWALL_HINT
    assert one.issue() == f"{one.problem()} ({one.hint})"


def test_status_semantics() -> None:
    assert FirewallStatus(allow=True).ok is True
    assert FirewallStatus(allow=True, block_rules=["x"]).ok is False
    assert FirewallStatus(allow=False).ok is False
    unread = FirewallStatus(checked=False, error="boom")
    assert unread.ok is False and "could not be read" in unread.problem()
    assert "Block rules" in FirewallStatus(allow=True, block_rules=["x"]).problem()
    assert "no inbound Allow" in FirewallStatus(allow=False).problem()


@pytest.mark.skipif(sys.platform != "win32", reason="firewall_status short-circuits off Windows")
async def test_firewall_status_runs_the_check_script(monkeypatch: pytest.MonkeyPatch) -> None:
    scripts: list[str] = []

    async def fake_run(script: str, timeout: float) -> tuple[int, str]:
        scripts.append(script)
        return 0, '{"allow":true,"block":[]}'

    monkeypatch.setattr(firewall, "_run_powershell", fake_run)
    status = await firewall_status("C:/x/kd.exe")
    assert status.ok and "Get-KdRules" in scripts[0] and "c:\\x\\kd.exe" in scripts[0].lower()


@pytest.mark.skipif(
    not os.environ.get("NTDRIVE_LIVE_TESTS"),
    reason="reads the real Windows firewall, set NTDRIVE_LIVE_TESTS=1",
)
async def test_firewall_status_reads_real_rules(tmp_path: Path) -> None:
    # A kd.exe path no rule names: the read itself must work and report no Allow rule.
    status = await firewall_status(str(tmp_path / "kd.exe"))
    assert status.checked is True, status.error
    assert status.allow is False and status.block_rules == []
