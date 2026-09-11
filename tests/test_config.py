"""vms.yaml lives in the user profile, never in a checkout or the working directory."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ntdrive import config as config_mod
from ntdrive.config import GuestConfig, find_config_path, load_config


def test_config_is_found_in_the_state_dir_not_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("NTDRIVE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vms.yaml").write_text("vms: {}\n")  # a stray file where ntdrive happens to run
    assert find_config_path() is None
    assert load_config().path == ""

    state = tmp_path / "local" / "ntdrive"
    state.mkdir(parents=True, exist_ok=True)  # state_dir() may have created it already
    (state / "vms.yaml").write_text("vms: {}\n")
    assert find_config_path() == state / "vms.yaml"

    env_file = tmp_path / "elsewhere.yaml"
    env_file.write_text("vms: {}\n")
    monkeypatch.setenv("NTDRIVE_CONFIG", str(env_file))
    assert find_config_path() == env_file

    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("vms: {}\n")
    assert find_config_path(str(explicit)) == explicit
    assert load_config(str(explicit)).path == str(explicit)


def test_secret_from_env_falls_back_to_the_user_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NTDRIVE_X_PW", raising=False)
    monkeypatch.setattr(
        config_mod,
        "_user_environment",
        lambda name: "from-registry" if name == "NTDRIVE_X_PW" else "",
    )
    if sys.platform == "win32":
        assert config_mod.secret_from_env("NTDRIVE_X_PW") == "from-registry"
        assert (
            GuestConfig(user="u", password_env="NTDRIVE_X_PW").resolve_password() == "from-registry"
        )
    monkeypatch.setenv("NTDRIVE_X_PW", "from-process")
    assert config_mod.secret_from_env("NTDRIVE_X_PW") == "from-process"
    assert GuestConfig(user="u", password_env="NTDRIVE_X_PW").resolve_password() == "from-process"
