"""Error type shared by every layer.

Every failure that reaches a client is a NtDriveError with a stable `code`, a human readable
`message` and a `hint` that tells the agent what to do next. The daemon serializes it as
`{"error": {"code": ..., "message": ..., "hint": ...}}` and the clients raise it again.
"""

from __future__ import annotations

from typing import Any

# Stable error codes. The CLI maps some of them to exit codes.
VM_NOT_FOUND = "vm_not_found"
VM_NOT_RUNNING = "vm_not_running"
GUEST_FROZEN_BY_DEBUGGER = "guest_frozen_by_debugger"
KD_NOT_ATTACHED = "kd_not_attached"
KD_ALREADY_ATTACHED = "kd_already_attached"
KD_NOT_BROKEN = "kd_not_broken"
SESSION_NOT_FOUND = "session_not_found"
SESSION_DISCONNECTED = "session_disconnected"
CONFIRM_REQUIRED = "confirm_required"
POLICY_DENIED = "policy_denied"
BACKEND_UNSUPPORTED = "backend_unsupported"
BACKEND_ERROR = "backend_error"
SNAPSHOT_NOT_FOUND = "snapshot_not_found"
INVALID_ARGS = "invalid_args"
TIMEOUT = "timeout"
DAEMON_UNAVAILABLE = "daemon_unavailable"
UNAUTHORIZED = "unauthorized"
VERSION_MISMATCH = "version_mismatch"
TOOL_NOT_FOUND = "tool_not_found"
INTERNAL = "internal"

# Backend error reasons. The hypervisor adapter classifies vmrun output once and tags the error
# with `reason=...` so tools never match English error text themselves.
REASON_ENCRYPTED_LIVE = "encrypted_live_snapshot"
REASON_PASSWORD_REQUIRED = "password_required"
REASON_CONFIG_UNREADABLE = "config_unreadable"
REASON_SNAPSHOT_MISSING = "snapshot_missing"

# HTTP status used by the daemon for each code. Anything not listed is 500.
HTTP_STATUS: dict[str, int] = {
    VM_NOT_FOUND: 404,
    VM_NOT_RUNNING: 409,
    GUEST_FROZEN_BY_DEBUGGER: 409,
    KD_NOT_ATTACHED: 409,
    KD_ALREADY_ATTACHED: 409,
    KD_NOT_BROKEN: 409,
    SESSION_NOT_FOUND: 404,
    SESSION_DISCONNECTED: 409,
    CONFIRM_REQUIRED: 428,
    POLICY_DENIED: 403,
    BACKEND_UNSUPPORTED: 400,
    BACKEND_ERROR: 502,
    SNAPSHOT_NOT_FOUND: 404,
    INVALID_ARGS: 400,
    TIMEOUT: 504,
    UNAUTHORIZED: 401,
    VERSION_MISMATCH: 409,
    TOOL_NOT_FOUND: 404,
}


class NtDriveError(Exception):
    """A failure with a stable code and an actionable hint."""

    def __init__(self, code: str, message: str, hint: str = "", **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.extra = extra

    @property
    def status(self) -> int:
        """HTTP status the daemon should answer with."""
        return HTTP_STATUS.get(self.code, 500)

    @property
    def reason(self) -> str:
        """Backend classification tag (one of the REASON_* constants), or empty."""
        return str(self.extra.get("reason", ""))

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the wire."""
        body: dict[str, Any] = {"code": self.code, "message": self.message, "hint": self.hint}
        body.update(self.extra)
        return {"error": body}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NtDriveError:
        """Rebuild from a wire payload produced by to_dict."""
        err = dict(data.get("error", data))
        code = str(err.pop("code", INTERNAL))
        message = str(err.pop("message", "unknown error"))
        hint = str(err.pop("hint", ""))
        return cls(code, message, hint, **err)

    def __str__(self) -> str:
        if self.hint:
            return f"[{self.code}] {self.message} (hint: {self.hint})"
        return f"[{self.code}] {self.message}"
