"""ntdrived HTTP + WebSocket application.

Routes:
- GET  /health                 version, pid, uptime (no auth)
- GET  /api/tools              registry summary
- POST /api/tools/{name}       run a tool, JSON body = arguments
- POST /api/shutdown           stop the daemon
- GET  /ws/term/{session_id}   raw PTY stream (binary frames both ways, JSON text for resize)
- GET  /coview                 the web terminal page
- GET  /coview/sessions        session list for the page

Every route except /health and /coview needs the token from daemon.json in the X-NtDrive-Token
header. WebSocket clients cannot set headers from a browser, so /ws/ routes (only) also accept
the `token` query parameter. /coview/sessions and /ws/term/ additionally accept the view token,
a second secret that opens terminal streams and nothing else. CoView URLs carry that one, so a
tool result never contains the daemon token.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from ntdrive import __version__
from ntdrive.config import load_config
from ntdrive.core.service import NtDriveService
from ntdrive.daemon.lifecycle import DaemonInfo, new_token, remove_info, write_info
from ntdrive.errors import SESSION_DISCONNECTED, UNAUTHORIZED, NtDriveError

log = logging.getLogger("ntdrived")
# Grace period for open connections to close during shutdown, before the socket is forced down.
SHUTDOWN_TIMEOUT = 1.0
STATIC_DIR = Path(__file__).with_name("static")
OPEN_PATHS = {"/health", "/coview", "/coview/"}
# Routes the view token may open: the CoView session list and the terminal streams.
VIEW_PATHS = {"/coview/sessions"}
VIEW_PREFIX = "/ws/term/"
INPUT_SOURCES = {"human", "agent"}
# The daemon is local-only, but a browser on the same machine can still be pointed at it by a
# hostile page. These headers keep the token out of referrers and responses out of caches.
SAFE_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


def error_response(exc: NtDriveError) -> web.Response:
    """Serialize a NtDriveError."""
    return web.json_response(exc.to_dict(), status=exc.status)


def _presented_token(request: web.Request) -> str:
    header = request.headers.get("X-NtDrive-Token")
    if header:
        return header
    if request.path.startswith("/ws/"):
        return request.query.get("token", "")
    return ""


def _accepted_tokens(request: web.Request) -> list[str]:
    """The daemon token everywhere, plus the view token on the terminal-stream routes."""
    tokens: list[str] = [request.app["token"]]
    view: str = request.app.get("view_token", "")
    if view and (request.path in VIEW_PATHS or request.path.startswith(VIEW_PREFIX)):
        tokens.append(view)
    return tokens


def _token_matches(presented: str, accepted: list[str]) -> bool:
    got = presented.encode("utf-8", errors="replace")
    return any(hmac.compare_digest(got, token.encode("utf-8")) for token in accepted)


@web.middleware
async def auth_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Token check for everything but /health and the CoView page."""
    if request.path in OPEN_PATHS:
        response = await handler(request)
    else:
        if not _token_matches(_presented_token(request), _accepted_tokens(request)):
            response = error_response(
                NtDriveError(UNAUTHORIZED, "missing or wrong daemon token", "read daemon.json")
            )
        else:
            response = await handler(request)
    for name, value in SAFE_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


async def health(request: web.Request) -> web.Response:
    """Liveness and version."""
    service: NtDriveService = request.app["service"]
    return web.json_response(
        {
            "ok": True,
            "version": __version__,
            "pid": os.getpid(),
            "started_at": service.state.started_at,
            "t_plus": service.state.t_plus(),
            "uptime_s": round(time.time() - service.state.started_at, 1),
            "tools": len(service.registry),
        }
    )


async def list_tools(request: web.Request) -> web.Response:
    """Registry summary."""
    service: NtDriveService = request.app["service"]
    return web.json_response({"tools": [spec.summary() for spec in service.registry]})


async def call_tool(request: web.Request) -> web.Response:
    """Run one tool."""
    service: NtDriveService = request.app["service"]
    name = request.match_info["name"]
    try:
        body: Any = await request.json() if request.can_read_body else {}
    except ValueError:
        return error_response(NtDriveError("invalid_args", "body must be JSON"))
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return error_response(NtDriveError("invalid_args", "body must be a JSON object"))
    caller = request.headers.get("X-NtDrive-Caller", "http")
    try:
        result = await service.call(name, body, caller=caller)
    except NtDriveError as exc:
        return error_response(exc)
    return web.json_response(result, dumps=lambda o: json.dumps(o, default=str))


async def shutdown(request: web.Request) -> web.Response:
    """Stop the daemon after answering."""
    request.app["stop"].set()
    return web.json_response({"ok": True, "stopping": True})


async def coview_page(request: web.Request) -> web.StreamResponse:
    """Static CoView page."""
    return web.FileResponse(STATIC_DIR / "coview.html")


async def coview_sessions(request: web.Request) -> web.Response:
    """Session list for the page."""
    service: NtDriveService = request.app["service"]
    return web.json_response({"sessions": service.term.sessions(None)})


async def term_ws(request: web.Request) -> web.StreamResponse:
    """Bidirectional PTY stream for CoView and `ntdrive term attach`."""
    service: NtDriveService = request.app["service"]
    session_id = request.match_info["session_id"]
    source = request.query.get("source", "human")
    if source not in INPUT_SOURCES:
        return error_response(
            NtDriveError("invalid_args", f"source must be one of {sorted(INPUT_SOURCES)}")
        )
    try:
        session = service.term.get(session_id)
    except NtDriveError as exc:
        return error_response(exc)
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    queue = session.subscribe()
    try:
        snapshot = session.snapshot_bytes()
        if snapshot:
            await ws.send_bytes(snapshot)
        if not session.connected:
            await ws.send_str(json.dumps({"type": "state", "state": "disconnected"}))

        async def pump() -> None:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    await ws.send_str(json.dumps({"type": "state", "state": "disconnected"}))
                    break
                await ws.send_bytes(chunk)

        pump_task = asyncio.create_task(pump())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    try:
                        session.send(msg.data, source=source)
                    except NtDriveError as exc:
                        await ws.send_str(json.dumps({"type": "error", **exc.to_dict()["error"]}))
                        if exc.code == SESSION_DISCONNECTED:
                            break
                elif msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        continue
                    if data.get("type") == "resize":
                        with contextlib.suppress(KeyError, ValueError, NtDriveError):
                            session.resize(int(data["cols"]), int(data["rows"]))
                    elif data.get("type") == "input":
                        with contextlib.suppress(NtDriveError):
                            session.send(str(data.get("data", "")).encode("utf-8"), source=source)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            pump_task.cancel()
    finally:
        session.unsubscribe(queue)
        if not ws.closed:
            await ws.close()
    return ws


def create_app(service: NtDriveService, token: str, view_token: str = "") -> web.Application:
    """Build the aiohttp application."""
    app = web.Application(middlewares=[auth_middleware], client_max_size=64 * 1024 * 1024)
    app["service"] = service
    app["token"] = token
    app["view_token"] = view_token
    app["stop"] = asyncio.Event()
    app.router.add_get("/health", health)
    app.router.add_get("/api/tools", list_tools)
    app.router.add_post("/api/tools/{name}", call_tool)
    app.router.add_post("/api/shutdown", shutdown)
    app.router.add_get("/ws/term/{session_id}", term_ws)
    app.router.add_get("/coview", coview_page)
    app.router.add_get("/coview/", coview_page)
    app.router.add_get("/coview/sessions", coview_sessions)
    return app


async def serve(config_path: str | None = None, bind: str | None = None) -> None:
    """Run the daemon until /api/shutdown or SIGINT."""
    config = load_config(config_path)
    if bind:
        config.host.daemon_bind = bind
    token = new_token()
    view_token = new_token()
    host, port = config.host.bind_host, config.host.bind_port
    coview_base = f"http://{host}:{port}/coview?token={view_token}"
    service = NtDriveService(config, coview_base=coview_base)
    app = create_app(service, token, view_token)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    # shutdown_timeout caps how long runner.cleanup() waits for open connections to close. The
    # default is 60 s, so a client holding an idle keep-alive connection (an MCP server, the CLI)
    # made `ntdrive daemon restart` take about 16 s. service.shutdown() already releases the guest
    # and closes SSH before cleanup, so idle HTTP and coview sockets can be dropped promptly.
    site = web.TCPSite(runner, host, port, shutdown_timeout=SHUTDOWN_TIMEOUT)
    try:
        await site.start()
    except OSError:
        # Port taken: move to the next one and record it in daemon.json.
        port += 1
        site = web.TCPSite(runner, host, port, shutdown_timeout=SHUTDOWN_TIMEOUT)
        await site.start()
        service.term.coview_base = f"http://{host}:{port}/coview?token={view_token}"
    info = DaemonInfo(
        host=host,
        port=port,
        pid=os.getpid(),
        token=token,
        version=__version__,
        started_at=service.state.started_at,
        config_path=config.path,
        view_token=view_token,
    )
    write_info(info)
    log.info("ntdrived %s listening on %s:%s (pid %s)", __version__, host, port, os.getpid())
    try:
        await app["stop"].wait()
    finally:
        log.info("ntdrived stopping")
        await service.shutdown()
        await runner.cleanup()
        remove_info()


def main(argv: list[str] | None = None) -> None:
    """Entry point for `ntdrived` and `python -m ntdrive.daemon.app`."""
    parser = argparse.ArgumentParser(prog="ntdrived", description="ntdrive daemon")
    parser.add_argument("--config", help="path to vms.yaml")
    parser.add_argument("--bind", help="host:port (default from vms.yaml or 127.0.0.1:8765)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(args.config, args.bind))


if __name__ == "__main__":
    main()
