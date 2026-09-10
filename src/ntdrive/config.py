"""Configuration models for vms.yaml and policy.yaml.

Search order for vms.yaml: the NTDRIVE_CONFIG environment variable, ./vms.yaml in the current
directory, then %LOCALAPPDATA%/ntdrive/vms.yaml. policy.yaml is looked up next to vms.yaml.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from ntdrive.errors import INVALID_ARGS, VM_NOT_FOUND, NtDriveError

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
            value = os.environ.get(self.password_env, "")
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
            value = os.environ.get(self.encryption_password_env, "")
            if value:
                return value
        return self.encryption_password

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
    candidates.append(Path.cwd() / "vms.yaml")
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
    try:
        raw: Any = yaml.safe_load(found.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise NtDriveError(INVALID_ARGS, f"cannot parse {found}: {exc}") from exc
    if not isinstance(raw, dict):
        raise NtDriveError(INVALID_ARGS, f"{found} must be a mapping at the top level")
    vms_raw = raw.get("vms") or {}
    for name, body in vms_raw.items():
        if isinstance(body, dict):
            body.setdefault("name", name)
    config = Config.model_validate({"host": raw.get("host") or {}, "vms": vms_raw})
    config.path = str(found)
    return config


def save_kdnet_settings(config: Config, vm_name: str, port: int, key: str) -> None:
    """Persist the KDNET port and key for a VM back into vms.yaml.

    PyYAML drops comments, so the file is rewritten without them. This is the one place the
    daemon writes the config file.
    """
    if not config.path:
        raise NtDriveError(INVALID_ARGS, "no vms.yaml to save into", "create vms.yaml first")
    path = Path(config.path)
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    vms = raw.setdefault("vms", {})
    entry = vms.setdefault(vm_name, {})
    kdnet = entry.setdefault("kdnet", {})
    kdnet["port"] = port
    kdnet["key"] = key
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
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
    candidates.append(Path.cwd() / "policy.yaml")
    candidates.append(state_dir() / "policy.yaml")
    for path in candidates:
        if path.is_file():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return PolicyConfig.model_validate(raw)
    return PolicyConfig()
