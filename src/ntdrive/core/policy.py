"""Policy gate: allow, confirm or deny a tool call before it runs."""

from __future__ import annotations

from typing import Any

from ntdrive.config import PolicyConfig
from ntdrive.core.registry import ToolSpec
from ntdrive.errors import CONFIRM_REQUIRED, POLICY_DENIED, NtDriveError


class Policy:
    """Decides whether a call may proceed.

    Rules, in order:
    1. A tool listed as `deny` never runs.
    2. A tool listed as `confirm`, or a destructive tool not listed at all, needs `confirm=true`.
       Tools whose params expose `mode` only need confirmation when `mode == "hard"`.
    3. Everything else runs.
    """

    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config or PolicyConfig()

    def level(self, spec: ToolSpec) -> str:
        """Effective level for a tool."""
        if spec.name in self.config.tools:
            return self.config.tools[spec.name]
        if spec.destructive:
            return "confirm"
        return self.config.default

    def check(self, spec: ToolSpec, params: Any) -> None:
        """Raise policy_denied or confirm_required when the call must not proceed."""
        level = self.level(spec)
        if level == "deny":
            raise NtDriveError(
                POLICY_DENIED,
                f"{spec.name} is denied by policy",
                "edit policy.yaml to allow it",
            )
        if level != "confirm":
            return
        mode = getattr(params, "mode", None)
        if mode is not None and mode != "hard":
            return
        if not hasattr(params, "confirm"):
            # A tool without a confirm flag cannot be confirmed, so "confirm" means deny here.
            raise NtDriveError(
                POLICY_DENIED,
                f"{spec.name} is set to confirm in policy.yaml but has no confirm flag",
                "set it to allow or deny in policy.yaml",
            )
        if getattr(params, "confirm", False):
            return
        raise NtDriveError(
            CONFIRM_REQUIRED,
            f"{spec.name} is destructive and needs confirmation",
            "call again with confirm=true if you really mean it",
        )
