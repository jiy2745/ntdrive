"""ntdrive: daemon plus MCP, CLI and SDK front doors for driving VMware guests."""

from ntdrive.errors import NtDriveError

__version__ = "0.1.0"

__all__ = ["NtDriveError", "__version__"]


def __getattr__(name: str) -> object:
    """Expose the SDK client lazily so importing the package stays cheap."""
    if name == "NtDrive":
        from ntdrive.sdk import NtDrive

        return NtDrive
    raise AttributeError(name)
