"""sys_health: the live per-VM probe (power, guest SSH port, debugger transport on the host)."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from ntdrive.core.service import NtDriveService
from ntdrive.core.state import KdState
from ntdrive.core.tools import sys as sys_tools
from tests.conftest import FakeVmrun, never_reachable


def _calls(fake_vmrun: FakeVmrun, command: str) -> int:
    return sum(1 for call in fake_vmrun.calls if command in call)


async def _vm(service: NtDriveService) -> dict[str, Any]:
    health = await service.call("sys_health", {})
    assert "daemon_port_free_now" not in health  # always false inside the daemon, so dropped
    return dict(health["vms"][0])


def test_udp_port_free_sees_a_held_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as held:
        held.bind(("127.0.0.1", 0))
        port = held.getsockname()[1]
        assert sys_tools._udp_port_free(port) is False  # noqa: SLF001
    assert sys_tools._udp_port_free(port) is True  # noqa: SLF001


async def test_health_skips_guest_probe_while_vm_is_off(
    service: NtDriveService, fake_vmrun: FakeVmrun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys_tools, "_udp_port_free", lambda port: True)
    vm = await _vm(service)
    assert vm["power"] == "off" and vm["kd_state"] == "detached"
    assert vm["guest"] == {
        "ip": None,
        "user": "dev",
        "standard_user": None,
        "ssh_port": service.config.vms["win11-dev"].guest.ssh_port,
        "ssh_open": None,
        "skipped": "vm_not_running",
    }
    assert vm["serial_pipe"] is None
    assert vm["kdnet_port"] == {
        "port": 50000,
        "free": True,
        "held_by_ntdrive": False,
        "firewall_ok": True,
    }
    assert vm["issues"] == []
    assert _calls(fake_vmrun, "getGuestIPAddress") == 0


async def test_health_probes_a_running_guest(
    service: NtDriveService, fake_vmrun: FakeVmrun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys_tools, "_udp_port_free", lambda port: True)
    await service.call("vm_start", {"vm": "win11-dev"})
    vm = await _vm(service)
    assert vm["power"] == "running"
    assert vm["guest"]["ip"] == fake_vmrun.ip
    assert vm["guest"]["ssh_open"] is True and vm["guest"]["skipped"] is None
    assert vm["issues"] == []
    # The IP lookup is bounded, unlike the 60 s wait the terminal tools accept.
    ip_call = next(c for c in fake_vmrun.calls if "getGuestIPAddress" in c)
    assert "-wait" in ip_call


async def test_health_reports_a_closed_ssh_port(
    service: NtDriveService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys_tools, "_udp_port_free", lambda port: True)
    service.ssh_probe = never_reachable
    await service.call("vm_start", {"vm": "win11-dev"})
    vm = await _vm(service)
    assert vm["guest"]["ssh_open"] is False
    assert any("SSH port" in issue and "OpenSSH" in issue for issue in vm["issues"])


async def test_health_reports_a_missing_guest_ip(
    service: NtDriveService, fake_vmrun: FakeVmrun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys_tools, "_udp_port_free", lambda port: True)
    fake_vmrun.ip = ""  # VMware Tools not reporting an address yet
    await service.call("vm_start", {"vm": "win11-dev"})
    vm = await _vm(service)
    assert vm["guest"]["ip"] is None and vm["guest"]["ssh_open"] is None
    assert any("guest IP unknown" in issue for issue in vm["issues"])


async def test_health_does_not_touch_a_frozen_guest(
    service: NtDriveService, fake_vmrun: FakeVmrun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys_tools, "_udp_port_free", lambda port: True)
    await service.call("vm_start", {"vm": "win11-dev"})
    service.runtime("win11-dev").kd_state = KdState.BROKEN
    before = _calls(fake_vmrun, "getGuestIPAddress")
    vm = await _vm(service)
    assert vm["power"] == "running" and vm["kd_state"] == "broken"
    assert vm["guest"]["skipped"] == "guest_frozen_by_debugger"
    assert vm["guest"]["ssh_open"] is None
    assert _calls(fake_vmrun, "getGuestIPAddress") == before


async def test_health_checks_the_serial_pipe_server_on_the_host(service: NtDriveService) -> None:
    cfg = service.config.vms["win11-dev"]
    cfg.kd_transport = "serial"
    cfg.kdnet.key = ""
    await service.call("kd_setup_host", {"vm": "win11-dev"})
    vm = await _vm(service)
    assert vm["kdnet_port"] is None
    assert vm["serial_pipe"] == {"path": cfg.resolved_serial_pipe(), "open": None}
    assert vm["issues"] == []

    await service.call("vm_start", {"vm": "win11-dev"})
    vm = await _vm(service)
    assert vm["serial_pipe"]["open"] is True and vm["issues"] == []

    service._kd_pipe_check = lambda pipe: False  # noqa: SLF001
    vm = await _vm(service)
    assert vm["serial_pipe"]["open"] is False
    assert any("no server on the host" in issue for issue in vm["issues"])


async def test_health_reports_a_kdnet_port_conflict_unless_ntdrive_holds_it(
    service: NtDriveService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys_tools, "_udp_port_free", lambda port: False)
    vm = await _vm(service)
    assert vm["kdnet_port"] == {
        "port": 50000,
        "free": False,
        "held_by_ntdrive": False,
        "firewall_ok": True,
    }
    assert any("UDP port 50000" in issue for issue in vm["issues"])

    await service.call("vm_start", {"vm": "win11-dev"})
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 1})
    vm = await _vm(service)
    assert vm["kdnet_port"]["held_by_ntdrive"] is True
    assert not any("UDP port" in issue for issue in vm["issues"])


async def test_health_names_the_fix_for_common_vmx_and_secret_mistakes(
    service: NtDriveService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys_tools, "_udp_port_free", lambda port: True)
    monkeypatch.delenv("NTDRIVE_TEST_PW", raising=False)
    monkeypatch.setattr("ntdrive.config._user_environment", lambda name: "")
    cfg = service.config.vms["win11-dev"]
    Path(cfg.vmx).write_text(
        'displayName = "win11-dev"\n'
        'uefi.secureBoot.enabled = "TRUE"\n'
        'ethernet0.virtualDev = "vmxnet3"\n'
        'encryption.keySafe = "vmware:key/list/(pair/...)"\n'
    )
    cfg.guest.password = ""
    cfg.guest.password_env = "NTDRIVE_TEST_PW"
    cfg.encryption_password_env = "NTDRIVE_TEST_PW"
    issues = (await _vm(service))["issues"]
    assert any("Secure Boot is on" in i for i in issues)
    assert any("guest NIC is vmxnet3" in i for i in issues)
    # The same variable backs both passwords, so it is reported once, with the fix.
    assert [i for i in issues if "NTDRIVE_TEST_PW" in i] == [
        "environment variable NTDRIVE_TEST_PW is empty (set it at User scope: ntdrive reads it "
        "from the registry at once, no new terminal needed)"
    ]
    assert not any("names no encryption password" in i for i in issues)

    cfg.encryption_password_env = ""
    monkeypatch.setenv("NTDRIVE_TEST_PW", "pw")
    issues = (await _vm(service))["issues"]
    assert any("encrypted" in i and "encryption_password_env" in i for i in issues)
    assert not any("NTDRIVE_TEST_PW" in i for i in issues)

    cfg.kd_transport = "serial"
    Path(cfg.vmx).write_text('displayName = "win11-dev"\n')
    cfg.guest.user = ""
    issues = (await _vm(service))["issues"]
    assert any("guest.user missing" in i for i in issues)
    assert any("serial pipe not in the vmx" in i for i in issues)
    assert not any("Secure Boot" in i or "NIC" in i or "encrypted" in i for i in issues)


async def test_health_names_the_accounts_and_an_empty_standard_password_variable(
    service: NtDriveService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys_tools, "_udp_port_free", lambda port: True)
    monkeypatch.delenv("NTDRIVE_TEST_STDPW", raising=False)
    monkeypatch.setattr("ntdrive.config._user_environment", lambda name: "")
    cfg = service.config.vms["win11-dev"]
    vm = await _vm(service)
    assert vm["guest"]["user"] == "dev" and vm["guest"]["standard_user"] is None
    cfg.guest.standard_user = "ntdrive-user"
    cfg.guest.standard_password_env = "NTDRIVE_TEST_STDPW"
    vm = await _vm(service)
    assert vm["guest"]["standard_user"] == "ntdrive-user"
    assert any("NTDRIVE_TEST_STDPW is empty" in issue for issue in vm["issues"])
