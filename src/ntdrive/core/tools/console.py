"""con_*: console screen (BSOD, login screen, boot hangs)."""

from __future__ import annotations

import base64
import time
from typing import Any

from pydantic import Field

from ntdrive.core.registry import tool
from ntdrive.core.service import NtDriveService
from ntdrive.core.tools.common import VmParams


class ScreenshotParams(VmParams):
    """con_screenshot."""

    base64: bool = Field(default=False, description="Also return the PNG as base64")


@tool(
    "con_screenshot",
    "Save a PNG of the VM console and return its path (base64 on request).",
    ScreenshotParams,
    touches_guest=True,
    effect="read",
)
async def con_screenshot(service: NtDriveService, p: ScreenshotParams) -> dict[str, Any]:
    """Screenshot through the hypervisor tools."""
    cfg = service.vm_cfg(p.vm)
    service.ensure_not_frozen(p.vm)
    await service.ensure_running(cfg)
    out_dir = service.log_dir / "screens"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{p.vm}-{time.strftime('%Y%m%d-%H%M%S')}.png"
    saved = await service.adapter_for(cfg).screenshot(cfg, str(path))
    result: dict[str, Any] = {"vm": p.vm, "png_path": saved}
    if p.base64:
        with open(saved, "rb") as fh:
            result["png_base64"] = base64.b64encode(fh.read()).decode("ascii")
    return result
