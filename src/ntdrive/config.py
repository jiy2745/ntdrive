"""Configuration models for vms.yaml and policy.yaml.

Search order for vms.yaml: an explicit path (--config), the NTDRIVE_CONFIG environment variable,
then %LOCALAPPDATA%/ntdrive/vms.yaml. The working directory is never searched: the config
belongs to the user, not to a checkout. policy.yaml is looked up next to vms.yaml, then in
the state directory. Secrets named by the *_env fields come from the environment or, on
Windows, from the user's Environment registry key, so a value saved by `ntdrive setup` is
visible to a running daemon at once.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from ntdrive.errors import INVALID_ARGS, VM_NOT_FOUND, NtDriveError

if sys.platform == "win32":
    import winreg

DEFAULT_VMRUN = "C:/Program Files (x86)/VMware/VMware Workstation/vmrun.exe"
DEFAULT_KD = "C:/Program Files (x86)/Windows Kits/10/Debuggers/x64/kd.exe"
DEFAULT_KDNET = "C:/Program Files (x86)/Windows Kits/10/Debuggers/x64/kdnet.exe"
DEFAULT_SYMBOL_PATH = "srv*C:/symbols*https://msdl.microsoft.com/download/symbols"


def state_dir() -> Path:
    """Directory for daemon.json, logs and other per-user state."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    path = Path(base) / "ntdrive"
    path.mkdir(parents=True, exist_ok=True)
    return path


class HostConfig(BaseModel):
    """Host side paths and daemon settings."""

    vmrun: str = DEFAULT_VMRUN
    kd: str = DEFAULT_KD
    kdnet: str = DEFAULT_KDNET
    symbol_path: str = DEFAULT_SYMBOL_PATH
    daemon_bind: str = "127.0.0.1:8765"
    log_dir: str = ""
    tool_timeout_max: int = 600
    ssh_connect_timeout: float = 10.0

    @property
    def bind_host(self) -> str:
        """Host part of daemon_bind."""
        return self.daemon_bind.rsplit(":", 1)[0]

    @property
    def bind_port(self) -> int:
        """Port part of daemon_bind."""
        return int(self.daemon_bind.rsplit(":", 1)[1])

    def resolved_log_dir(self) -> Path:
        """Log directory, created on demand."""
        path = Path(self.log_dir) if self.log_dir else state_dir() / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path


def secret_from_env(name: str) -> str:
    """The value of an environment variable, or of the User-scope variable of that name.

    The daemon only inherits the environment it was started with, and `ntdrive setup` stores
    passwords at User scope, so on Windows the registry is read as a fallback: a secret is
    visible the moment it is saved, with no new terminal and no daemon restart.
    """
    value = os.environ.get(name, "")
    if value or sys.platform != "win32":
        return value
    return _user_environment(name)


def _user_environment(name: str) -> str:
    """One value of the user's Environment registry key, or empty. Faked in tests."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value)


class GuestConfig(BaseModel):
    """Credentials and shell for the guest OS."""

    user: str
    password_env: str = ""
    password: str = ""
    ssh_port: int = 22
    shell: Literal["powershell", "cmd", "pwsh"] = "powershell"

    def resolve_password(self) -> str:
        """Password from the environment variable, falling back to the inline value."""
        if self.password_env:
            value = secret_from_env(self.password_env)
            if value:
                return value
        return self.password


class KdnetConfig(BaseModel):
    """KDNET port and key for one VM."""

    port: int = 50000
    key: str = ""


class VmConfig(BaseModel):
    """One registered VM."""

    name: str = ""
    backend: str = "vmware"
    vmx: str = ""
    kdnet_hostip: str = ""
    encryption_password_env: str = ""
    encryption_password: str = ""
    kd_transport: Literal["net", "serial"] = "net"
    serial_pipe: str = ""
    guest: GuestConfig
    kdnet: KdnetConfig = Field(default_factory=KdnetConfig)

    def resolved_serial_pipe(self) -> str:
        r"""Host named pipe for serial KD, defaulting to \\.\pipe\ntdrive-<name>."""
        return self.serial_pipe or rf"\\.\pipe\ntdrive-{self.name}"

    def resolve_encryption_password(self) -> str:
        """VM encryption password from the environment variable, else the inline value."""
        if self.encryption_password_env:
            value = secret_from_env(self.encryption_password_env)
            if value:
                return value
        return self.encryption_password

    @field_validator("kdnet", mode="before")
    @classmethod
    def _kdnet_empty_is_default(cls, value: Any) -> Any:
        # A hand-edited `kdnet:` with nothing under it means the defaults, not an error.
        return {} if value is None else value

    @field_validator("backend")
    @classmethod
    def _backend_known(cls, value: str) -> str:
        return value.lower()


class Config(BaseModel):
    """Whole vms.yaml."""

    host: HostConfig = Field(default_factory=HostConfig)
    vms: dict[str, VmConfig] = Field(default_factory=dict)
    path: str = ""

    def vm(self, name: str) -> VmConfig:
        """Look up a VM or raise vm_not_found."""
        try:
            return self.vms[name]
        except KeyError:
            known = ", ".join(sorted(self.vms)) or "(none)"
            raise NtDriveError(
                VM_NOT_FOUND,
                f"VM '{name}' is not registered",
                f"use one of: {known}, or add it to {self.path or 'vms.yaml'}",
            ) from None


def find_config_path(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Locate vms.yaml following the documented search order."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("NTDRIVE_CONFIG")
    if env:
        candidates.append(Path(env))
    candidates.append(state_dir() / "vms.yaml")
    for path in candidates:
        if path.is_file():
            return path
    return None


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load vms.yaml. A missing file yields an empty config so sys_health can still report."""
    found = find_config_path(path)
    if found is None:
        return Config(path="")
    raw = read_raw_config(found)
    vms_raw = raw.get("vms") or {}
    for name, body in vms_raw.items():
        if isinstance(body, dict):
            body.setdefault("name", name)
    config = Config.model_validate({"host": raw.get("host") or {}, "vms": vms_raw})
    config.path = str(found)
    return config


CONFIG_HEADER = (
    "# Written by ntdrive (ntdrive setup, kd_setup_guest). git-ignored: it names VM paths,\n"
    "# accounts and the variables that hold passwords. `ntdrive setup` adds or changes a VM.\n"
)


def read_raw_config(path: str | os.PathLike[str]) -> dict[str, Any]:
    """vms.yaml as a plain dict, {} when the file does not exist yet."""
    target = Path(path)
    if not target.is_file():
        return {}
    try:
        raw: Any = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise NtDriveError(INVALID_ARGS, f"cannot parse {target}: {exc}") from exc
    if not isinstance(raw, dict):
        raise NtDriveError(INVALID_ARGS, f"{target} must be a mapping at the top level")
    return raw


def write_raw_config(path: str | os.PathLike[str], data: dict[str, Any]) -> None:
    """Write vms.yaml with the standard header. The one place the file is written.

    PyYAML drops comments, so anything a person added by hand is not kept.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=False, default_flow_style=False)
    target.write_text(CONFIG_HEADER + body, encoding="utf-8", newline="\n")


def save_kdnet_settings(config: Config, vm_name: str, port: int, key: str) -> None:
    """Persist the KDNET port and key for a VM back into vms.yaml."""
    if not config.path:
        raise NtDriveError(INVALID_ARGS, "no vms.yaml to save into", "create vms.yaml first")
    raw = read_raw_config(config.path)
    vms = raw.setdefault("vms", {})
    entry = vms.setdefault(vm_name, {})
    kdnet = entry.get("kdnet") or {}
    kdnet["port"] = port
    kdnet["key"] = key
    entry["kdnet"] = kdnet
    write_raw_config(config.path, raw)
    config.vms[vm_name].kdnet = KdnetConfig(port=port, key=key)


class PolicyConfig(BaseModel):
    """policy.yaml: per-tool allow / confirm / deny levels."""

    default: Literal["allow", "confirm", "deny"] = "allow"
    tools: dict[str, Literal["allow", "confirm", "deny"]] = Field(default_factory=dict)


def load_policy(config: Config) -> PolicyConfig:
    """Load policy.yaml next to vms.yaml, or the built-in defaults."""
    candidates: list[Path] = []
    if config.path:
        candidates.append(Path(config.path).with_name("policy.yaml"))
    candidates.append(state_dir() / "policy.yaml")
    for path in candidates:
        if path.is_file():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return PolicyConfig.model_validate(raw)
    return PolicyConfig()
