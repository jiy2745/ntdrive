"""`ntdrive setup`: write the vms.yaml entry for one VM without editing YAML by hand.

Picks a VM from the VMware Workstation inventory (or takes --vmx), reads what the vmx already
says (display name, encryption), asks for the guest account and the passwords with hidden input,
stores the passwords as User environment variables (or inline in vms.yaml with
--inline-secrets), writes the entry, restarts the daemon so it sees the new VM, and prints the
sys_health issues that are left. Run it again to add another VM or to change one. Passwords are
never taken from the command line.
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any

import click
import psutil

from ntdrive.config import (
    find_config_path,
    load_config,
    read_raw_config,
    state_dir,
    write_raw_config,
)
from ntdrive.core.tools.sys import config_issues
from ntdrive.daemon.client import connect
from ntdrive.daemon.lifecycle import restart_daemon
from ntdrive.errors import INVALID_ARGS, NtDriveError
from ntdrive.hypervisor.vmx import vmx_settings

if sys.platform == "win32":
    import ctypes
    import winreg

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
FIRST_KDNET_PORT = 50000
BACKENDS = {"vmware"}


# -- host facts, replaced by fakes in tests ---------------------------------------------------


def inventory_vmx_paths() -> list[str]:
    """The vmx files VMware Workstation lists in its inventory, existing ones only."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    try:
        text = (Path(appdata) / "VMware" / "inventory.vmls").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return []
    paths: list[str] = []
    for m in re.finditer(r'(?m)^vmlist\d+\.config\s*=\s*"(.+)"\s*$', text):
        raw = m.group(1)
        if Path(raw).is_file() and raw not in paths:
            paths.append(raw)
    return paths


def vmnet8_ip() -> str:
    """IPv4 of the host's VMware Network Adapter VMnet8, or empty when there is none."""
    for name, addrs in psutil.net_if_addrs().items():
        if "vmnet8" not in name.lower():
            continue
        for addr in addrs:
            if addr.family == socket.AF_INET:
                return str(addr.address)
    return ""


def store_secret(name: str, value: str) -> str:
    """Save a User-scope environment variable and expose it to this process.

    On Windows the value goes to the user's Environment registry key, which the daemon reads as
    a fallback (config.secret_from_env), so the secret is visible at once. Explorer is told too,
    so new terminals see it.
    """
    os.environ[name] = value
    if sys.platform != "win32":
        return f"{name} is set for this process only (persist it in your shell profile)"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    hwnd_broadcast, wm_settingchange, smto_abortifhung = 0xFFFF, 0x001A, 0x0002
    with contextlib.suppress(Exception):
        ctypes.windll.user32.SendMessageTimeoutW(
            hwnd_broadcast, wm_settingchange, 0, "Environment", smto_abortifhung, 2000, None
        )
    return f"{name} saved as a User environment variable"


def restart_and_check(config_path: Path, name: str) -> list[str]:
    """Restart the daemon on this config and return the sys_health issues of one VM."""
    restart_daemon(str(config_path))
    health = connect(str(config_path), caller="cli").call("sys_health", {})
    for vm in health.get("vms", []):
        if vm.get("name") == name:
            return [str(issue) for issue in vm.get("issues", [])]
    return []


# -- pure helpers ------------------------------------------------------------------------------


def slug(text: str) -> str:
    """A vms.yaml name from a display name: lower case, dashes, nothing else."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "vm"


def env_name(vm: str, suffix: str) -> str:
    """Environment variable name for a secret of one VM, for example NTDRIVE_WIN11_DEV_PW."""
    core = re.sub(r"[^A-Z0-9]+", "_", vm.upper()).strip("_")
    return f"NTDRIVE_{core}_{suffix}"


def _same_file(a: str, b: str) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def _name_for_vmx(vms: dict[str, Any], vmx: str) -> str | None:
    """The configured name whose entry points at this vmx, or None."""
    for name, body in vms.items():
        if isinstance(body, dict) and _same_file(str(body.get("vmx", "")), vmx):
            return name
    return None


def _other_vm_using(vms: dict[str, Any], var: str, name: str) -> str | None:
    """Name of another VM whose passwords already live in environment variable `var`."""
    for other, body in vms.items():
        if other == name or not isinstance(body, dict):
            continue
        guest = body.get("guest") or {}
        if var in (guest.get("password_env"), body.get("encryption_password_env")):
            return other
    return None


def _next_kdnet_port(vms: dict[str, Any]) -> int:
    """The first KDNET port from 50000 that no configured VM uses."""
    used: set[int] = set()
    for body in vms.values():
        kdnet = (body.get("kdnet") if isinstance(body, dict) else None) or {}
        with contextlib.suppress(TypeError, ValueError):
            used.add(int(kdnet.get("port") or 0))
    port = FIRST_KDNET_PORT
    while port in used:
        port += 1
    return port


# -- the command -------------------------------------------------------------------------------


def _pick_vmx(vms: dict[str, Any]) -> str:
    paths = inventory_vmx_paths()
    if not paths:
        return str(click.prompt("Path of the .vmx file", type=click.Path(exists=True)))
    click.echo("VMs known to VMware Workstation:")
    for i, path in enumerate(paths, start=1):
        display = vmx_settings(path).get("displayname", Path(path).stem)
        configured = _name_for_vmx(vms, path)
        note = f"  (configured as {configured})" if configured else ""
        click.echo(f"  {i}. {display}{note}\n     {path}")
    answer = str(click.prompt("Which VM? (number, or the path of a .vmx file)", default="1"))
    if answer.isdigit() and 1 <= int(answer) <= len(paths):
        return paths[int(answer) - 1]
    if Path(answer).is_file():
        return answer
    raise NtDriveError(INVALID_ARGS, f"{answer!r} is neither a number in the list nor a vmx file")


def _config_target(explicit: str | None) -> Path:
    """Where the entry goes.

    --config, then NTDRIVE_CONFIG (even before the file exists), then the vms.yaml ntdrive
    would find, then the state directory.
    """
    if explicit:
        return Path(explicit)
    env = os.environ.get("NTDRIVE_CONFIG")
    if env:
        return Path(env)
    found = find_config_path()
    return found if found is not None else state_dir() / "vms.yaml"


def run_setup(
    config_opt: str | None,
    vmx_opt: str | None,
    name_opt: str | None,
    user_opt: str | None,
    transport_opt: str | None,
    inline_secrets: bool,
    restart: bool,
) -> None:
    """The interactive flow. Every prompt has a default where one is knowable."""
    config_path = _config_target(config_opt)
    data = read_raw_config(config_path)
    vms = data.get("vms")
    if not isinstance(vms, dict):
        vms = {}
    data["vms"] = vms

    vmx = vmx_opt or _pick_vmx(vms)
    if not Path(vmx).is_file():
        raise NtDriveError(INVALID_ARGS, f"vmx not found: {vmx}")
    # The daemon runs elsewhere, so a relative path would point at the wrong place there.
    vmx = str(Path(vmx).resolve())
    facts = vmx_settings(vmx)
    display = facts.get("displayname", Path(vmx).stem)
    encrypted = "encryption.keysafe" in facts
    click.echo(f"VM: {display}" + ("  (encrypted, a vTPM does this)" if encrypted else ""))

    existing_name = _name_for_vmx(vms, vmx)
    name = name_opt or str(
        click.prompt("Name for this VM in ntdrive", default=existing_name or slug(display))
    )
    if not NAME_RE.match(name):
        raise NtDriveError(INVALID_ARGS, "the name may use letters, digits, dash and underscore")
    if not inline_secrets:
        for suffix in ("PW", "VMPW"):
            var = env_name(name, suffix)
            other = _other_vm_using(vms, var, name)
            if other:
                raise NtDriveError(
                    INVALID_ARGS,
                    f"{name} would keep its password in {var}, which VM {other} already uses",
                    "pick a name that differs in more than punctuation, or use --inline-secrets",
                )
    entry: dict[str, Any] = dict(vms.get(name) or {}) if isinstance(vms.get(name), dict) else {}
    guest: dict[str, Any] = dict(entry.get("guest") or {})
    if entry and not _same_file(str(entry.get("vmx", vmx)), vmx):
        click.echo(f"  {name} now points at this vmx instead of {entry.get('vmx')}")

    user = user_opt or str(
        click.prompt(
            "Guest account for SSH (a local account with a password)",
            default=guest.get("user") or None,
        )
    )
    had_secret = bool(guest.get("password") or guest.get("password_env"))
    if had_secret:
        password = str(
            click.prompt(
                "Guest password (Enter keeps the current one)",
                hide_input=True,
                default="",
                show_default=False,
            )
        )
    else:
        password = str(click.prompt("Guest password", hide_input=True, confirmation_prompt=True))
    vm_password = ""
    had_vm_secret = bool(entry.get("encryption_password") or entry.get("encryption_password_env"))
    if encrypted:
        hint = (
            "Enter keeps the current one" if had_vm_secret else "Enter: same as the guest password"
        )
        vm_password = str(
            click.prompt(
                f"VM encryption password ({hint})",
                hide_input=True,
                default="",
                show_default=False,
            )
        )

    transport = transport_opt or str(entry.get("kd_transport") or "net")
    hostip = str(entry.get("kdnet_hostip") or vmnet8_ip())
    if transport == "net" and not hostip:
        hostip = str(click.prompt("IPv4 of the host's VMware Network Adapter VMnet8"))

    notes: list[str] = []
    guest["user"] = user
    if password:
        if inline_secrets:
            guest["password"] = password
            guest.pop("password_env", None)
        else:
            var = env_name(name, "PW")
            notes.append(store_secret(var, password))
            guest["password_env"] = var
            guest.pop("password", None)
    if encrypted:
        if vm_password and inline_secrets:
            entry["encryption_password"] = vm_password
            entry.pop("encryption_password_env", None)
        elif vm_password and vm_password != password:
            var = env_name(name, "VMPW")
            notes.append(store_secret(var, vm_password))
            entry["encryption_password_env"] = var
            entry.pop("encryption_password", None)
        elif vm_password or not had_vm_secret:
            # Same as the guest password, typed or implied by Enter: share the guest's secret,
            # whether it was stored just now or kept from an earlier run.
            if guest.get("password_env"):
                entry["encryption_password_env"] = guest["password_env"]
                entry.pop("encryption_password", None)
            elif guest.get("password"):
                entry["encryption_password"] = guest["password"]
                entry.pop("encryption_password_env", None)
        # Enter with a stored encryption password keeps it.
    guest.setdefault("ssh_port", 22)
    guest.setdefault("shell", "powershell")

    kdnet = dict(entry.get("kdnet") or {})
    if "port" not in kdnet:
        kdnet["port"] = _next_kdnet_port(vms)
    entry.update(
        {
            "backend": "vmware",
            "vmx": vmx,
            "kd_transport": transport,
            "kdnet_hostip": hostip,
            "guest": guest,
            "kdnet": kdnet,
        }
    )
    vms[name] = entry
    write_raw_config(config_path, data)

    click.echo(f"wrote {name} to {config_path}")
    for note in notes:
        click.echo(f"  {note}")
    if inline_secrets:
        click.echo("  passwords stored inline in vms.yaml (git-ignored, keep it private)")

    if restart:
        issues = restart_and_check(config_path, name)
        source = "sys_health"
    else:
        issues = config_issues(load_config(config_path).vm(name), BACKENDS)
        source = "the config check"
    if issues:
        click.echo(f"{source} still reports:")
        for issue in issues:
            click.echo(f"  - {issue}")
    else:
        click.echo(f"{source} reports no issues for this VM")
    click.echo(
        "next: in the guest run scripts/setup-guest.ps1 as Administrator (installs OpenSSH), "
        f"then on the host: ntdrive kd setup-host {name}"
    )


def setup_command() -> click.Command:
    """The `ntdrive setup` click command."""

    @click.command(
        "setup",
        help=(
            "Write the vms.yaml entry for one VM: pick it, enter the guest account and the "
            "passwords, done. Run again to add another VM or to change one."
        ),
    )
    @click.option("--vmx", "vmx_opt", type=click.Path(), help="The .vmx file (skips the list)")
    @click.option("--name", "name_opt", help="Name for ntdrive (default: from the display name)")
    @click.option("--user", "user_opt", help="Guest account for SSH")
    @click.option(
        "--transport",
        "transport_opt",
        type=click.Choice(["net", "serial"]),
        help="Kernel debug transport (default: net, or the current value)",
    )
    @click.option(
        "--inline-secrets",
        is_flag=True,
        help="Store passwords inside vms.yaml instead of User environment variables",
    )
    @click.option("--no-restart", is_flag=True, help="Skip the daemon restart and sys_health")
    @click.pass_context
    def setup(
        ctx: click.Context,
        vmx_opt: str | None,
        name_opt: str | None,
        user_opt: str | None,
        transport_opt: str | None,
        inline_secrets: bool,
        no_restart: bool,
    ) -> None:
        config_opt = ctx.ensure_object(dict).get("config")
        try:
            run_setup(
                config_opt,
                vmx_opt,
                name_opt,
                user_opt,
                transport_opt,
                inline_secrets,
                restart=not no_restart,
            )
        except NtDriveError as exc:
            # main imports this module at load, so the back-import waits until a call.
            from ntdrive.cli.main import fail

            fail(ctx, exc)

    return setup
