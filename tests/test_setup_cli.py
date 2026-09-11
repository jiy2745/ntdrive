"""`ntdrive setup` writes vms.yaml from prompts, keeps secrets out of the file, and is re-runnable."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner, Result

import ntdrive.cli.setup as setup_mod
from ntdrive.cli.main import build_cli
from ntdrive.config import load_config
from ntdrive.core.registry import load_builtin_tools


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    store: dict[str, str] = {}
    vmx = tmp_path / "Win11 Dev" / "Win11 Dev.vmx"
    vmx.parent.mkdir()
    vmx.write_text(
        'displayName = "Win11 Dev"\nencryption.keySafe = "x"\nethernet0.virtualDev = "e1000e"\n'
    )

    def remember(name: str, value: str) -> str:
        store[name] = value
        return f"{name} stored"

    monkeypatch.delenv("NTDRIVE_CONFIG", raising=False)
    monkeypatch.setattr(setup_mod, "inventory_vmx_paths", lambda: [str(vmx)])
    monkeypatch.setattr(setup_mod, "vmnet8_ip", lambda: "192.168.99.1")
    monkeypatch.setattr(setup_mod, "store_secret", remember)
    monkeypatch.setattr(
        setup_mod,
        "restart_and_check",
        lambda path, name: ["kdnet key not set (run kd_setup_guest)"],
    )
    return {"store": store, "vmx": vmx, "config": tmp_path / "vms.yaml"}


def _run(env: dict[str, Any], args: list[str], input_text: str) -> Result:
    cli = build_cli(load_builtin_tools())
    return CliRunner().invoke(
        cli, ["--config", str(env["config"]), "setup", *args], input=input_text
    )


def test_setup_writes_the_entry_and_keeps_secrets_out_of_the_file(env: dict[str, Any]) -> None:
    # Pick VM 1, accept the suggested name, type the account, the password twice, then Enter for
    # "encryption password = guest password".
    result = _run(env, [], "1\n\nalice\npw1\npw1\n\n")
    assert result.exit_code == 0, result.output
    vm = load_config(env["config"]).vms["win11-dev"]
    assert Path(vm.vmx) == env["vmx"].resolve() and vm.kd_transport == "net"
    assert vm.guest.user == "alice" and vm.guest.password == ""
    assert vm.guest.password_env == "NTDRIVE_WIN11_DEV_PW"
    assert vm.encryption_password_env == "NTDRIVE_WIN11_DEV_PW"
    assert vm.kdnet_hostip == "192.168.99.1" and vm.kdnet.port == 50000
    assert env["store"] == {"NTDRIVE_WIN11_DEV_PW": "pw1"}
    text = env["config"].read_text()
    assert "pw1" not in text and text.startswith("# Written by ntdrive")
    assert "kdnet key not set" in result.output and "kd setup-host win11-dev" in result.output


def test_setup_adds_a_second_vm_and_updates_an_existing_one(
    env: dict[str, Any], tmp_path: Path
) -> None:
    first = _run(
        env, ["--name", "one", "--user", "u1", "--transport", "serial"], "1\npw\npw\nvmpw\n"
    )
    assert first.exit_code == 0, first.output
    other = tmp_path / "other.vmx"
    other.write_text('displayName = "Other"\n')
    second = _run(env, ["--vmx", str(other), "--name", "two", "--user", "u2"], "pw2\npw2\n")
    assert second.exit_code == 0, second.output
    cfg = load_config(env["config"])
    assert set(cfg.vms) == {"one", "two"}
    assert cfg.vms["one"].kd_transport == "serial" and cfg.vms["two"].kd_transport == "net"
    assert cfg.vms["one"].kdnet_hostip == "192.168.99.1"
    assert cfg.vms["one"].encryption_password_env == "NTDRIVE_ONE_VMPW"
    assert cfg.vms["two"].encryption_password_env == ""
    assert cfg.vms["one"].kdnet.port == 50000 and cfg.vms["two"].kdnet.port == 50001
    assert env["store"] == {
        "NTDRIVE_ONE_PW": "pw",
        "NTDRIVE_ONE_VMPW": "vmpw",
        "NTDRIVE_TWO_PW": "pw2",
    }

    # Running again for "one": Enter keeps both stored passwords, the account can change.
    again = _run(env, ["--name", "one", "--user", "u9"], "1\n\n\n")
    assert again.exit_code == 0, again.output
    cfg = load_config(env["config"])
    one = cfg.vms["one"]
    assert one.guest.user == "u9" and one.guest.password_env == "NTDRIVE_ONE_PW"
    assert one.encryption_password_env == "NTDRIVE_ONE_VMPW" and one.kd_transport == "serial"
    assert set(cfg.vms) == {"one", "two"} and len(env["store"]) == 3


def test_enter_at_the_vm_password_shares_a_kept_guest_password(env: dict[str, Any]) -> None:
    first = _run(env, ["--name", "dev", "--user", "u"], "1\npw\npw\n\n")
    assert first.exit_code == 0, first.output
    # The VM was encrypted after the first run, or the entry was written by hand.
    raw = yaml.safe_load(env["config"].read_text())
    del raw["vms"]["dev"]["encryption_password_env"]
    env["config"].write_text(yaml.safe_dump(raw, sort_keys=False))
    assert load_config(env["config"]).vms["dev"].encryption_password_env == ""

    again = _run(env, ["--name", "dev", "--user", "u"], "1\n\n\n")
    assert again.exit_code == 0, again.output
    vm = load_config(env["config"]).vms["dev"]
    assert (
        vm.encryption_password_env == "NTDRIVE_DEV_PW" and vm.guest.password_env == "NTDRIVE_DEV_PW"
    )
    assert env["store"] == {"NTDRIVE_DEV_PW": "pw"}


def test_setup_can_store_secrets_inline(env: dict[str, Any]) -> None:
    result = _run(env, ["--inline-secrets", "--name", "dev", "--user", "u"], "1\npw\npw\n\n")
    assert result.exit_code == 0, result.output
    vm = load_config(env["config"]).vms["dev"]
    assert vm.guest.password == "pw" and vm.encryption_password == "pw"
    assert vm.guest.password_env == "" and env["store"] == {}


def test_setup_refuses_a_name_that_would_share_another_vms_variable(
    env: dict[str, Any],
) -> None:
    first = _run(env, ["--name", "win11-dev", "--user", "u"], "1\npw\npw\n\n")
    assert first.exit_code == 0, first.output
    clash = _run(env, ["--name", "win11_dev", "--user", "v", "--no-restart"], "1\npw\npw\n\n")
    assert clash.exit_code == 2, clash.output
    assert "NTDRIVE_WIN11_DEV_PW" in clash.output and "win11-dev" in clash.output
    assert set(load_config(env["config"]).vms) == {"win11-dev"}


def test_setup_survives_a_null_kdnet_in_another_entry(env: dict[str, Any]) -> None:
    env["config"].write_text(
        "vms:\n  old:\n    vmx: D:/x/x.vmx\n    guest:\n      user: u\n    kdnet:\n"
    )
    result = _run(env, ["--name", "dev", "--user", "u", "--no-restart"], "1\npw\npw\n\n")
    assert result.exit_code == 0, result.output
    cfg = load_config(env["config"])
    assert set(cfg.vms) == {"old", "dev"} and cfg.vms["dev"].kdnet.port == 50000


def test_setup_without_restart_prints_the_config_checks(
    env: dict[str, Any], tmp_path: Path
) -> None:
    vmx = tmp_path / "sb.vmx"
    vmx.write_text('displayName = "sb"\nuefi.secureBoot.enabled = "TRUE"\n')
    result = _run(
        env, ["--vmx", str(vmx), "--name", "sb", "--user", "u", "--no-restart"], "pw\npw\n"
    )
    assert result.exit_code == 0, result.output
    assert "Secure Boot is on" in result.output and "kdnet key not set" in result.output


def test_setup_honours_ntdrive_config_before_the_file_exists(
    env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "lab" / "vms.yaml"
    monkeypatch.setenv("NTDRIVE_CONFIG", str(target))
    cli = build_cli(load_builtin_tools())
    result = CliRunner().invoke(
        cli, ["setup", "--name", "dev", "--user", "u", "--no-restart"], input="1\npw\npw\n\n"
    )
    assert result.exit_code == 0, result.output
    assert target.is_file() and "dev" in load_config(target).vms


def test_setup_rejects_a_bad_name_and_a_missing_vmx(env: dict[str, Any], tmp_path: Path) -> None:
    bad = _run(env, ["--name", "no spaces", "--user", "u", "--no-restart"], "1\npw\npw\n\n")
    assert bad.exit_code == 2 and "letters, digits" in bad.output
    missing = _run(env, ["--vmx", str(tmp_path / "nope.vmx"), "--no-restart"], "")
    assert missing.exit_code == 2 and "vmx not found" in missing.output
    as_json = CliRunner().invoke(
        build_cli(load_builtin_tools()),
        ["--json", "--config", str(env["config"]), "setup", "--vmx", str(tmp_path / "nope.vmx")],
    )
    assert as_json.exit_code == 2 and '"code": "invalid_args"' in as_json.output
    assert not env["config"].exists()


def test_helpers() -> None:
    assert setup_mod.slug("Insider Preview Windows 11 x64") == "insider-preview-windows-11-x64"
    assert setup_mod.env_name("win11-dev", "PW") == "NTDRIVE_WIN11_DEV_PW"
    assert setup_mod.env_name("a.b", "VMPW") == "NTDRIVE_A_B_VMPW"
    assert setup_mod._next_kdnet_port({"a": {"kdnet": {"port": 50000}}, "b": None}) == 50001  # noqa: SLF001
    assert setup_mod._next_kdnet_port({"a": {"kdnet": {"port": "abc"}}}) == 50000  # noqa: SLF001


def test_mask_shows_enough_to_recognize_a_password() -> None:
    assert setup_mod.mask("") == "(empty)"
    assert setup_mod.mask("ab") == "a* (2 chars)"
    assert setup_mod.mask("abcd") == "ab*d (4 chars)"
    assert setup_mod.mask("hunter2!") == "hu*****! (8 chars)"


def test_setup_explains_the_passwords_before_asking(env: dict[str, Any]) -> None:
    result = _run(env, ["--name", "dev", "--user", "u", "--no-restart"], "1\npw12\npw12\n\n")
    assert result.exit_code == 0, result.output
    assert "INFO  guest password: the Windows password of that account" in result.output
    assert "INFO  VM encryption password: the one VMware asked for" in result.output
    assert "INFO  entered: pw*2 (4 chars)" in result.output and "pw12" not in result.output
    assert (
        "== 3/3 Write and check" in result.output
        and "DONE: dev is configured on the host" in result.output
    )
