"""ntdrive CLI.

Every registry tool becomes `ntdrive <group> <verb>`. Positional parameters come from the
tool's `positional` tuple, everything else is an option. `--json` prints the raw result.
Extra commands that are not tools: `daemon start|stop|status|restart` and `term attach`.
"""

from __future__ import annotations

import json
import sys
import types
import typing
from typing import Any, get_args, get_origin

import click
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from ntdrive import __version__
from ntdrive.cli.attach import attach_session
from ntdrive.cli.setup import setup_command
from ntdrive.cli.verify import verify_command
from ntdrive.core.registry import ToolRegistry, ToolSpec, load_builtin_tools
from ntdrive.daemon.client import DaemonClient, connect
from ntdrive.daemon.lifecycle import ensure_daemon, read_info, restart_daemon, stop_daemon
from ntdrive.errors import (
    CONFIRM_REQUIRED,
    GUEST_FROZEN_BY_DEBUGGER,
    INVALID_ARGS,
    TIMEOUT,
    NtDriveError,
)
from ntdrive.paths import absolutize_local

EXIT_CODES = {INVALID_ARGS: 2, CONFIRM_REQUIRED: 3, GUEST_FROZEN_BY_DEBUGGER: 4, TIMEOUT: 5}


# -- output ----------------------------------------------------------------------------------


def _print_human(result: Any, indent: int = 0) -> None:
    pad = "  " * indent
    if isinstance(result, dict):
        for key, value in result.items():
            if key in ("text", "output") and isinstance(value, str):
                click.echo(f"{pad}{key}:")
                click.echo(value)
            elif isinstance(value, dict | list):
                click.echo(f"{pad}{key}:")
                _print_human(value, indent + 1)
            else:
                click.echo(f"{pad}{key}: {value}")
    elif isinstance(result, list):
        for item in result:
            if isinstance(item, dict | list):
                click.echo(f"{pad}-")
                _print_human(item, indent + 1)
            else:
                click.echo(f"{pad}- {item}")
    else:
        click.echo(f"{pad}{result}")


def emit(ctx: click.Context, result: Any) -> None:
    """Print a result as JSON or human text."""
    if ctx.obj.get("json"):
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        _print_human(result)


def fail(ctx: click.Context, exc: NtDriveError) -> None:
    """Print an error and exit with the mapped code."""
    if ctx.obj.get("json"):
        click.echo(json.dumps(exc.to_dict(), indent=2), err=True)
    else:
        click.echo(f"error [{exc.code}]: {exc.message}", err=True)
        if exc.hint:
            click.echo(f"hint: {exc.hint}", err=True)
    sys.exit(EXIT_CODES.get(exc.code, 1))


def client_for(ctx: click.Context) -> DaemonClient:
    """Connect (and auto-start) the daemon."""
    try:
        return connect(ctx.obj.get("config"), autostart=True, caller="cli")
    except NtDriveError as exc:
        fail(ctx, exc)
        raise AssertionError("unreachable") from exc


# -- command generation ---------------------------------------------------------------------


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    origin = get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _click_type(annotation: Any) -> tuple[Any, bool]:
    """(click type, multiple)."""
    origin = get_origin(annotation)
    if origin is typing.Literal:
        return click.Choice([str(a) for a in get_args(annotation)]), False
    if origin is list:
        inner = get_args(annotation)[0] if get_args(annotation) else str
        return _click_type(inner)[0], True
    if annotation is bool:
        return click.BOOL, False
    if annotation is int:
        return click.INT, False
    if annotation is float:
        return click.FLOAT, False
    return click.STRING, False


def _params_for(spec: ToolSpec) -> list[click.Parameter]:
    params: list[click.Parameter] = []
    fields: dict[str, FieldInfo] = spec.params.model_fields
    for name, field in fields.items():
        annotation, optional = _unwrap_optional(field.annotation)
        ctype, multiple = _click_type(annotation)
        required = field.is_required()
        help_text = field.description or ""
        if name in spec.positional:
            params.append(
                click.Argument(
                    [name],
                    required=required,
                    default=None if not required else None,
                    nargs=1,
                )
            )
            continue
        flag = "--" + name.replace("_", "-")
        if annotation is bool and not optional:
            params.append(
                click.Option(
                    [f"{flag}/--no-{name.replace('_', '-')}"],
                    default=None,
                    help=help_text,
                    show_default=str(field.default) if not required else None,
                )
            )
        else:
            params.append(
                click.Option(
                    [flag],
                    type=ctype,
                    multiple=multiple,
                    default=None,
                    required=required,
                    help=help_text,
                    show_default=str(field.default) if field.default is not None else None,
                )
            )
    return params


def _make_command(spec: ToolSpec) -> click.Command:
    params = _params_for(spec)
    extra: list[click.Parameter] = []
    if spec.name == "kd_exec":
        extra.append(
            click.Option(["--file"], type=click.Path(exists=True), help="Read commands from a file")
        )
        extra.append(click.Option(["--stdin"], is_flag=True, help="Read commands from stdin"))

    @click.pass_context
    def callback(ctx: click.Context, /, **kwargs: Any) -> None:
        args: dict[str, Any] = {}
        for key, value in kwargs.items():
            if key in ("file", "stdin"):
                continue
            if value is None or value == ():
                continue
            if isinstance(value, tuple):
                value = list(value)
            if key == "local" and isinstance(value, str):
                # The daemon runs elsewhere, so host paths must be absolute before they leave.
                value = absolutize_local(value)
            args[key] = value
        if spec.name == "kd_exec":
            lines: list[str] = []
            if kwargs.get("file"):
                with open(kwargs["file"], encoding="utf-8") as fh:
                    lines = [ln.strip() for ln in fh if ln.strip()]
            elif kwargs.get("stdin"):
                lines = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
            if lines:
                args.setdefault("cmds", [])
                args["cmds"] = list(args["cmds"]) + lines
        client = client_for(ctx)
        try:
            result = client.call(spec.name, args)
        except NtDriveError as exc:
            fail(ctx, exc)
            return
        emit(ctx, result)

    return click.Command(
        name=spec.verb.replace("_", "-"),
        params=params + extra,
        callback=callback,
        help=spec.description + (" (destructive, needs --confirm)" if spec.destructive else ""),
    )


def build_cli(registry: ToolRegistry) -> click.Group:
    """Assemble the click application."""

    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.option("--json", "as_json", is_flag=True, help="Print raw JSON results")
    @click.option("--config", type=click.Path(), help="Path to vms.yaml")
    @click.version_option(__version__, prog_name="ntdrive")
    @click.pass_context
    def cli(ctx: click.Context, as_json: bool, config: str | None) -> None:
        """Drive VMware guests: power, snapshots, KDNET debugging, real-time terminals."""
        ctx.obj = {"json": as_json, "config": config}

    for group_name, specs in registry.groups().items():
        group = click.Group(name=group_name, help=f"{group_name}_* tools")
        for spec in specs:
            group.add_command(_make_command(spec))
        if group_name == "term":
            group.add_command(_attach_command())
        cli.add_command(group)
    cli.add_command(_daemon_group())
    cli.add_command(setup_command())
    cli.add_command(verify_command())
    return cli


def _attach_command() -> click.Command:
    @click.command(
        "attach", help="Sit in a live session. Ctrl+] detaches. Input is logged as human."
    )
    @click.argument("session_id")
    @click.pass_context
    def attach(ctx: click.Context, session_id: str) -> None:
        client = client_for(ctx)
        try:
            attach_session(client, session_id)
        except NtDriveError as exc:
            fail(ctx, exc)

    return attach


def _daemon_group() -> click.Group:
    @click.group("daemon", help="Manage ntdrived")
    def daemon() -> None:
        pass

    @daemon.command("start")
    @click.pass_context
    def start(ctx: click.Context) -> None:
        try:
            info = ensure_daemon(ctx.obj.get("config"), autostart=True)
        except NtDriveError as exc:
            fail(ctx, exc)
            return
        emit(ctx, {"running": True, "pid": info.pid, "url": info.base_url, "version": info.version})

    @daemon.command("status")
    @click.pass_context
    def status(ctx: click.Context) -> None:
        info = read_info()
        if info is None:
            emit(ctx, {"running": False})
            return
        try:
            health = DaemonClient(info, caller="cli").health()
        except NtDriveError:
            emit(ctx, {"running": False, "stale_pid": info.pid})
            return
        emit(ctx, {"running": True, "pid": info.pid, "url": info.base_url, **health})

    @daemon.command("stop")
    @click.pass_context
    def stop(ctx: click.Context) -> None:
        info = read_info()
        if info is None:
            emit(ctx, {"running": False})
            return
        emit(ctx, {"stopped": stop_daemon(info)})

    @daemon.command("restart")
    @click.pass_context
    def restart(ctx: click.Context) -> None:
        try:
            info = restart_daemon(ctx.obj.get("config"))
        except NtDriveError as exc:
            fail(ctx, exc)
            return
        emit(ctx, {"running": True, "pid": info.pid, "url": info.base_url})

    return daemon


def main() -> None:
    """Entry point for `ntdrive`."""
    registry = load_builtin_tools()
    cli = build_cli(registry)
    cli(prog_name="ntdrive")


_model_check: type[BaseModel] = BaseModel  # keeps the import used for type checkers

if __name__ == "__main__":
    main()
