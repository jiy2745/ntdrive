"""con_*: console screen (BSOD, login screen, boot hangs)."""

from __future__ import annotations

import base64
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from ntdrive.core.registry import tool
from ntdrive.core.tools.common import VmParams
from ntdrive.errors import BACKEND_ERROR, INVALID_ARGS, NtDriveError
from ntdrive.screen import keymap, vnc

if TYPE_CHECKING:
    from ntdrive.core.service import NtDriveService


class ScreenshotParams(VmParams):
    """con_screenshot."""

    base64: bool = Field(default=False, description="Also return the PNG as base64")
    method: Literal["auto", "guest", "vnc"] = Field(
        default="auto",
        description=(
            "auto: vmrun captureScreen, falling back to VNC if guest login fails and VNC is on. "
            "guest: vmrun only (needs a working guest login). vnc: the console VNC framebuffer, "
            "which needs no guest login (con_enable_vnc turns it on)"
        ),
    )


class EnableVncParams(VmParams):
    """con_enable_vnc."""

    port: int | None = Field(
        default=None, ge=1, le=65535, description="VNC port; a per-VM default when omitted"
    )


class SendKeysParams(VmParams):
    """con_send_keys."""

    keys: list[str] = Field(
        default_factory=list,
        description=(
            "Keys to type over the console. Each item is text or one {token}: {enter} {tab} "
            "{esc} {backspace} {up} {ctrl+alt+delete} {win+r} {alt+f4}. {password} and "
            "{standard_password} as a whole item type that guest account's password from "
            "vms.yaml without it passing through the arguments or the log"
        ),
    )


class ClickParams(VmParams):
    """con_click."""

    x: int = Field(
        ge=0, le=65535, description="X in framebuffer pixels (con_screenshot method=vnc)"
    )
    y: int = Field(ge=0, le=65535, description="Y in framebuffer pixels")
    button: Literal["left", "right", "middle"] = Field(default="left", description="Which button")
    double: bool = Field(default=False, description="Double-click instead of a single click")


# RFB button mask bits: left 1, middle 2, right 4.
_BUTTON_MASK = {"left": 1, "middle": 2, "right": 4}


class AutologonParams(VmParams):
    """con_autologon."""

    enabled: bool = Field(default=True, description="true sets autologon, false clears it")
    account: Literal["admin", "standard"] = Field(
        default="standard",
        description=(
            "Which guest account logs in automatically at boot: standard (guest.standard_user, a "
            "plain Medium-IL desktop, the safer default) or admin (guest.user)"
        ),
    )


# The registry key that Winlogon reads at boot for automatic logon.
_WINLOGON = r"HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"


def _ps_literal(value: str) -> str:
    """A string as a PowerShell single-quoted literal body (single quotes are doubled)."""
    return value.replace("'", "''")


def _out_path(service: NtDriveService, vm: str) -> str:
    out_dir = service.log_dir / "screens"
    out_dir.mkdir(parents=True, exist_ok=True)
    return str(out_dir / f"{vm}-{time.strftime('%Y%m%d-%H%M%S')}.png")


@tool(
    "con_screenshot",
    "Save a PNG of the VM console and return its path (base64 on request). method=vnc reads the "
    "console framebuffer without any guest login (con_enable_vnc turns VNC on first).",
    ScreenshotParams,
    effect="read",
)
async def con_screenshot(service: NtDriveService, p: ScreenshotParams) -> dict[str, Any]:
    """Screenshot through vmrun (guest login) or the VNC framebuffer (no login)."""
    cfg = service.vm_cfg(p.vm)
    adapter = service.adapter_for(cfg)
    endpoint = adapter.vnc_endpoint(cfg)

    async def by_vnc() -> tuple[str, str]:
        if endpoint is None:
            raise NtDriveError(
                BACKEND_ERROR,
                f"VNC is not enabled for {p.vm}",
                "run con_enable_vnc with the VM off, then start it",
            )
        await service.ensure_running(cfg)
        path = await vnc.capture(endpoint[0], endpoint[1], "", _out_path(service, p.vm))
        return path, "vnc"

    if p.method == "vnc":
        saved, via = await by_vnc()
    else:
        # The guest (vmrun) path freezes-check and needs the guest running and reachable.
        service.ensure_not_frozen(p.vm)
        await service.ensure_running(cfg)
        try:
            saved = await adapter.screenshot(cfg, _out_path(service, p.vm))
            via = "guest"
        except NtDriveError:
            if p.method == "guest" or endpoint is None:
                raise
            saved, via = await by_vnc()  # auto: fall back to the login-free path
    result: dict[str, Any] = {"vm": p.vm, "png_path": saved, "via": via}
    if p.base64:
        with open(saved, "rb") as fh:
            result["png_base64"] = base64.b64encode(fh.read()).decode("ascii")
    return result


@tool(
    "con_send_keys",
    "Type keys into the VM console over VNC, no guest login needed (con_enable_vnc turns VNC on). "
    "For a lock or login screen or before the network is up: keys=['{password}', '{enter}'] logs "
    "in without the password crossing the wire.",
    SendKeysParams,
    positional=("vm",),
    touches_guest=True,
    effect="destructive",
)
async def con_send_keys(service: NtDriveService, p: SendKeysParams) -> dict[str, Any]:
    """Send console key input through the VNC framebuffer server.

    {password} and {standard_password} items are expanded from vms.yaml inside the daemon and
    typed character by character, so the secret never appears in the arguments, the result or the
    audit log. Every other item is text or a {token} in the same vocabulary as term_send.
    """
    cfg = service.vm_cfg(p.vm)
    if not p.keys:
        raise NtDriveError(
            INVALID_ARGS, "nothing to send", "give keys, for example ['hi', '{enter}']"
        )
    endpoint = service.adapter_for(cfg).vnc_endpoint(cfg)
    if endpoint is None:
        raise NtDriveError(
            BACKEND_ERROR,
            f"VNC is not enabled for {p.vm}",
            "run con_enable_vnc with the VM off, then start it",
        )
    await service.ensure_running(cfg)
    strokes: list[keymap.Stroke] = []
    for item in p.keys:
        if item == "{password}":
            strokes.extend(keymap.literal_strokes(cfg.guest.resolve_password()))
        elif item == "{standard_password}":
            strokes.extend(keymap.literal_strokes(cfg.guest.resolve_standard_password()))
        else:
            strokes.extend(keymap.key_strokes([item]))
    await vnc.send_keys(endpoint[0], endpoint[1], "", keymap.flatten(strokes))
    service.state.record_event(p.vm, "con_send_keys", keys=len(p.keys))
    return {"vm": p.vm, "sent": len(p.keys)}


@tool(
    "con_click",
    "Click the VM console at a framebuffer pixel over VNC, no guest login needed. Read the "
    "coordinate off con_screenshot method=vnc (same pixels), for example to pick a user tile on "
    "the lock screen, then con_send_keys for the password.",
    ClickParams,
    positional=("vm", "x", "y"),
    touches_guest=True,
    effect="destructive",
)
async def con_click(service: NtDriveService, p: ClickParams) -> dict[str, Any]:
    """Move the pointer to (x, y) and click, through the VNC framebuffer server."""
    cfg = service.vm_cfg(p.vm)
    endpoint = service.adapter_for(cfg).vnc_endpoint(cfg)
    if endpoint is None:
        raise NtDriveError(
            BACKEND_ERROR,
            f"VNC is not enabled for {p.vm}",
            "run con_enable_vnc with the VM off, then start it",
        )
    await service.ensure_running(cfg)
    mask = _BUTTON_MASK[p.button]
    # Move there with no button, press, release. A double-click presses and releases twice.
    events = [(0, p.x, p.y), (mask, p.x, p.y), (0, p.x, p.y)]
    if p.double:
        events += [(mask, p.x, p.y), (0, p.x, p.y)]
    await vnc.send_pointer(endpoint[0], endpoint[1], "", events)
    service.state.record_event(p.vm, "con_click", x=p.x, y=p.y, button=p.button)
    return {"vm": p.vm, "clicked": [p.x, p.y], "button": p.button, "double": p.double}


@tool(
    "con_autologon",
    "Configure Windows automatic logon in the guest so a reboot lands on an unlocked interactive "
    "desktop (session 1), which term_* over SSH (session 0) cannot open. enabled=false clears it. "
    "needs_reboot: run vm_reboot mode=soft next.",
    AutologonParams,
    positional=("vm",),
    touches_guest=True,
    effect="additive",
    idempotent=True,
)
async def con_autologon(service: NtDriveService, p: AutologonParams) -> dict[str, Any]:
    """Set or clear the Winlogon autologon keys over SSH.

    The password comes from vms.yaml inside the daemon and is written to the guest's
    DefaultPassword value, so it stays out of the tool arguments, the result and the audit log.
    That value is stored in the guest registry in cleartext, which is how Windows autologon
    works, so use this on a debugging VM and clear it (enabled=false) when done.
    """
    cfg = service.vm_cfg(p.vm)
    user, password = cfg.guest.credentials(p.account)
    if p.enabled and not user:
        field = "standard_user" if p.account == "standard" else "user"
        raise NtDriveError(
            INVALID_ARGS,
            f"{p.vm} has no {p.account} account (guest.{field} is empty)",
            "add it in vms.yaml (setup-guest.cmd -Standard creates ntdrive-user), or pass "
            "account=admin",
        )
    service.ensure_not_frozen(p.vm)
    await service.ensure_running(cfg)
    transport = await service.transport(cfg)
    if not hasattr(transport, "exec_once"):
        raise NtDriveError(BACKEND_ERROR, "the transport cannot run commands")
    if p.enabled:
        # DefaultPassword goes last so no secret sits in the first characters of the command.
        script = "; ".join(
            [
                f"$w = '{_WINLOGON}'",
                "Set-ItemProperty -Path $w -Name AutoAdminLogon -Value '1'",
                f"Set-ItemProperty -Path $w -Name DefaultUserName -Value '{_ps_literal(user)}'",
                "Set-ItemProperty -Path $w -Name DefaultDomainName -Value $env:COMPUTERNAME",
                "Remove-ItemProperty -Path $w -Name AutoLogonCount -ErrorAction SilentlyContinue",
                f"Set-ItemProperty -Path $w -Name DefaultPassword -Value '{_ps_literal(password)}'",
            ]
        )
    else:
        script = "; ".join(
            [
                f"$w = '{_WINLOGON}'",
                "Set-ItemProperty -Path $w -Name AutoAdminLogon -Value '0'",
                "Remove-ItemProperty -Path $w -Name DefaultPassword -ErrorAction SilentlyContinue",
            ]
        )
    code, out = await transport.exec_once(script, timeout=60)
    if code != 0:
        detail = out.strip()[:300]
        if password:
            detail = detail.replace(password, "***")
        raise NtDriveError(
            BACKEND_ERROR,
            f"could not set autologon in {p.vm}: {detail}",
            "the SSH account needs administrator rights",
        )
    service.state.record_event(p.vm, "con_autologon", enabled=p.enabled, account=p.account)
    return {
        "vm": p.vm,
        "enabled": p.enabled,
        "account": p.account,
        "user": user if p.enabled else None,
        "needs_reboot": True,
    }


@tool(
    "con_enable_vnc",
    "Turn on the console VNC server in the vmx so con_screenshot method=vnc can read the screen "
    "without a guest login. Run it with the VM off, then start the VM.",
    EnableVncParams,
    effect="additive",
    idempotent=True,
)
async def con_enable_vnc(service: NtDriveService, p: EnableVncParams) -> dict[str, Any]:
    """Enable RemoteDisplay.vnc in the vmx (VM off)."""
    from ntdrive.hypervisor.vmware import default_vnc_port

    cfg = service.vm_cfg(p.vm)
    port = p.port or default_vnc_port(p.vm)
    result = await service.adapter_for(cfg).ensure_vnc(cfg, port)
    service.state.record_event(p.vm, "enable_vnc", port=port)
    return {
        "vm": p.vm,
        **result,
        "note": (
            "VNC has no password and listens on the host, so keep it to a trusted host or add a "
            "firewall rule. Start the VM for the change to take effect, then con_screenshot "
            "method=vnc works with no guest login"
        ),
    }
