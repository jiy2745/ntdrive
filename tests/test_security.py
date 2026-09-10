"""Daemon auth, guest host key pinning, input validation and the policy gate."""

from pathlib import Path

import paramiko
import pytest
from aiohttp.test_utils import TestClient, TestServer

from ntdrive.config import PolicyConfig
from ntdrive.core.policy import Policy
from ntdrive.core.registry import load_builtin_tools
from ntdrive.core.service import NtDriveService
from ntdrive.daemon.app import create_app
from ntdrive.errors import INVALID_ARGS, POLICY_DENIED, NtDriveError
from ntdrive.term.ssh import HostKeyChanged, PinnedHostKeyPolicy


async def test_query_token_only_works_for_websockets(service: NtDriveService) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    opened = await service.call("term_open", {"vm": "win11-dev"})
    app = create_app(service, token="t")
    async with TestClient(TestServer(app)) as client:
        # API routes: header only. A token in the URL would end up in logs and history.
        denied = await client.post("/api/tools/sys_health?token=t", json={})
        assert denied.status == 401
        assert denied.headers["Referrer-Policy"] == "no-referrer"
        ok = await client.post("/api/tools/sys_health", json={}, headers={"X-NtDrive-Token": "t"})
        assert ok.status == 200 and ok.headers["Cache-Control"] == "no-store"
        wrong = await client.post(
            "/api/tools/sys_health", json={}, headers={"X-NtDrive-Token": "tt"}
        )
        assert wrong.status == 401
        # WebSockets cannot set headers from a browser, so the query form is allowed there.
        ws = await client.ws_connect(f"/ws/term/{opened['session_id']}?token=t&source=human")
        await ws.close()
        bad_source = await client.get(f"/ws/term/{opened['session_id']}?token=t&source=<script>")
        assert bad_source.status == 400
        no_token = await client.get(f"/ws/term/{opened['session_id']}")
        assert no_token.status == 401


def test_pinned_host_key_policy(tmp_path: Path) -> None:
    store = tmp_path / "keys" / "vm.json"
    policy = PinnedHostKeyPolicy(store)
    first = paramiko.RSAKey.generate(1024)
    policy.missing_host_key(None, "192.168.1.5", first)
    assert store.exists()
    # Same key again, even from another address (DHCP moved the guest): fine.
    policy.missing_host_key(None, "192.168.1.9", first)
    with pytest.raises(HostKeyChanged):
        policy.missing_host_key(None, "192.168.1.5", paramiko.RSAKey.generate(1024))


async def test_kd_inputs_are_validated(service: NtDriveService) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("kd_attach", {"vm": "win11-dev", "key": "1.2.3.4; calc.exe"})
    assert exc.value.code == INVALID_ARGS
    with pytest.raises(NtDriveError) as exc:
        await service.call("kd_setup_guest", {"vm": "win11-dev", "key": "x y"})
    assert exc.value.code == INVALID_ARGS
    service.config.vms["win11-dev"].kdnet_hostip = "192.168.1.1; whoami"
    with pytest.raises(NtDriveError) as exc:
        await service.call("kd_setup_guest", {"vm": "win11-dev"})
    assert exc.value.code == INVALID_ARGS and "IPv4" in exc.value.message


def test_policy_confirm_on_tool_without_flag_is_a_deny() -> None:
    registry = load_builtin_tools()
    spec = registry.get("kd_exec")
    assert spec is not None
    policy = Policy(PolicyConfig(tools={"kd_exec": "confirm"}))
    with pytest.raises(NtDriveError) as exc:
        policy.check(spec, spec.params(vm="x", cmd="r"))
    assert exc.value.code == POLICY_DENIED
