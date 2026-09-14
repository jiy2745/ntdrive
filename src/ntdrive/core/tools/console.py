"""con_*: console screen (BSOD, login screen, boot hangs)."""

from __future__ import annotations

import base64
import time
from typing import Any, Literal

from pydantic import Field

from ntdrive.core.registry import tool
from ntdrive.core.service import NtDriveService
from ntdrive.core.tools.common import VmParams
from ntdrive.errors import BACKEND_ERROR, NtDriveError
from ntdrive.screen import vnc


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
