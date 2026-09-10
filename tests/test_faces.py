"""The three front doors (HTTP/MCP, CLI, SDK) must expose the same tools and results."""

import asyncio
import json

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from click.testing import CliRunner

import ntdrive.cli.main as cli_main
from ntdrive.core.registry import load_builtin_tools
from ntdrive.core.service import NtDriveService
from ntdrive.daemon.app import create_app
from ntdrive.mcp.server import build_server
from ntdrive.sdk import NtDrive

from .conftest import FakeTransport, settle


class _LocalClient:
    """Stand-in for DaemonClient that calls the service on a private loop (CLI tests)."""

    def __init__(self, service: NtDriveService, loop: asyncio.AbstractEventLoop) -> None:
        self.service = service
        self.loop = loop
        self.info = type("Info", (), {"token": "t", "base_url": "http://x", "ws_base": "ws://x"})()

    def call(self, name: str, args: dict) -> dict:  # type: ignore[type-arg]
        return asyncio.run_coroutine_threadsafe(
            self.service.call(name, args, caller="cli"), self.loop
        ).result(30)


async def test_http_and_sdk_and_cli_agree(service: NtDriveService, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    direct = await service.call("sys_health", {})

    app = create_app(service, token="t")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/tools/sys_health", json={}, headers={"X-NtDrive-Token": "t"})
        assert resp.status == 200
        via_http = await resp.json()
        denied = await client.post("/api/tools/sys_health", json={})
        assert denied.status == 401
        health = await client.get("/health")
        assert (await health.json())["ok"]
        tools = await (await client.get("/api/tools", headers={"X-NtDrive-Token": "t"})).json()
        assert {t["name"] for t in tools["tools"]} == set(service.registry.names())

    vt = NtDrive(service=service)
    via_sdk = vt.sys.health()

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(cli_main, "connect", lambda *a, **k: _LocalClient(service, loop))
    runner = CliRunner()
    result = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: runner.invoke(
            cli_main.build_cli(load_builtin_tools()), ["--json", "sys", "health"]
        ),
    )
    assert result.exit_code == 0, result.output
    via_cli = json.loads(result.output)

    for other in (via_http, via_sdk, via_cli):
        assert set(other) == set(direct)
        assert other["tools"] == direct["tools"]
        assert (
            other["backends"]["vmware"]["capabilities"]
            == direct["backends"]["vmware"]["capabilities"]
        )


async def test_mcp_server_lists_and_calls_tools(service: NtDriveService) -> None:
    class FakeDaemonClient:
        async def acall(self, name: str, args: dict) -> dict:  # type: ignore[type-arg]
            return await service.call(name, args, caller="mcp")

    server = build_server(service.registry, FakeDaemonClient())  # type: ignore[arg-type]
    list_handler = server.request_handlers[
        __import__("mcp.types", fromlist=["ListToolsRequest"]).ListToolsRequest
    ]
    from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

    listed = await list_handler(ListToolsRequest(method="tools/list"))
    names = {t.name for t in listed.root.tools}
    assert "kd_exec" in names and "term_read" in names
    kd_exec = next(t for t in listed.root.tools if t.name == "kd_exec")
    assert "cmds" in kd_exec.inputSchema["properties"]

    call_handler = server.request_handlers[CallToolRequest]
    request = CallToolRequest(
        method="tools/call", params=CallToolRequestParams(name="sys_health", arguments={})
    )
    answer = await call_handler(request)
    payload = json.loads(answer.root.content[0].text)
    assert payload["tools"] == len(service.registry)

    bad = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name="kd_exec", arguments={"vm": "nope", "cmd": "r"}),
    )
    answer = await call_handler(bad)
    payload = json.loads(answer.root.content[0].text)
    assert payload["error"]["code"] == "vm_not_found"


async def test_websocket_streams_session(
    service: NtDriveService, fake_transport: FakeTransport
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    opened = await service.call("term_open", {"vm": "win11-dev"})
    sid = opened["session_id"]
    app = create_app(service, token="t")
    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect(f"/ws/term/{sid}?token=t&source=human")
        first = await ws.receive()
        assert first.type == aiohttp.WSMsgType.BINARY and b"PS C:" in first.data
        await ws.send_bytes(b"echo hi\r")
        await settle()
        chan = fake_transport.channels[-1]
        assert chan.written[-1] == b"echo hi\r"
        echoed = await ws.receive()
        assert echoed.type == aiohttp.WSMsgType.BINARY and b"echo hi" in echoed.data
        events = (service.log_dir / "term").glob("*.events.jsonl")
        text = "".join(p.read_text() for p in events)
        assert '"source": "human"' in text
        await ws.close()


def test_cli_builds_commands_for_every_tool() -> None:
    cli = cli_main.build_cli(load_builtin_tools())
    groups = {name: cmd for name, cmd in cli.commands.items()}
    assert set(groups) == {"vm", "snap", "kd", "term", "con", "file", "sys", "daemon"}
    kd = groups["kd"]
    assert isinstance(kd, cli_main.click.Group)
    assert {"exec", "attach", "wait-event", "setup-guest"} <= set(kd.commands)
    exec_cmd = kd.commands["exec"]
    names = {p.name for p in exec_cmd.params}
    assert {"vm", "cmd", "cmds", "timeout", "file", "stdin"} <= names
    assert "attach" in groups["term"].commands
