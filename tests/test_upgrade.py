"""vm_upgrade_builds, vm_upgrade and vm_upgrade_status against the fakes and a fake UUP dump API."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from ntdrive.core.service import NtDriveService
from ntdrive.core.state import KdState
from ntdrive.core.tools import upgrade
from ntdrive.errors import BACKEND_ERROR, INVALID_ARGS, KD_ALREADY_ATTACHED, NtDriveError
from tests.conftest import FakeTransport

UUID = "aab2b64c-3521-4716-9060-632df5659bfd"
CDN = "http://tlu.dl.delivery.mp.microsoft.com/filestreamingservice/files/"


def _file(name: str, size: int = 1000, host: str = CDN) -> tuple[str, dict[str, Any]]:
    return name, {"url": host + name, "sha1": "a" * 40, "size": str(size)}


FILES = dict(
    [
        _file("MetadataESD_professional_en-us.esd", 605_000_000),
        _file("MetadataESD_core_en-us.esd", 600_000_000),
        _file("Microsoft-Windows-Client-Desktop-Required-Package.esd", 1_035_000_000),
        _file(
            "cabs_Microsoft-Windows-Wifi-Client-Realtek-Rtl8192se-FOD-Package-amd64.cab", 300_000
        ),
        _file("Windows11.0-KB5127719-x64-NDP481.cab", 46_000_000),
        _file("Wim_Edge.wim", 201_000_000),
    ]
)


def _api(files: dict[str, Any] | None = None) -> Any:
    def fetch(url: str) -> Any:
        parsed = urlparse(url)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        endpoint = parsed.path.rsplit("/", 1)[1]
        if endpoint == "listid.php":
            return {
                "response": {
                    "builds": {
                        "0": {
                            "uuid": UUID,
                            "build": "29698.1000",
                            "arch": "amd64",
                            "title": "Windows 11 Insider Preview 29698.1000 (rs_prerelease)",
                            "created": 1793379616,
                        },
                        "1": {
                            "uuid": "older",
                            "build": "29695.1000",
                            "arch": "amd64",
                            "title": "Windows 11 Insider Preview 29695.1000 (rs_prerelease)",
                        },
                        "2": {
                            "uuid": "arm",
                            "build": "29698.1000",
                            "arch": "arm64",
                            "title": "Windows 11 Insider Preview 29698.1000 (rs_prerelease)",
                        },
                        "3": {
                            "uuid": "cu",
                            "build": "29698.1001",
                            "arch": "amd64",
                            "title": "Cumulative Update for Windows 11 Insider Preview (29698.1001)",
                        },
                    }
                }
            }
        if endpoint == "listeditions.php":
            assert query["id"] == UUID
            return {"response": {"editionList": ["CORE", "PROFESSIONAL"]}}
        if endpoint == "get.php":
            assert query["edition"] == "professional" and query["lang"] == "en-us"
            return {"response": {"files": files if files is not None else FILES}}
        raise AssertionError(url)

    return fetch


def _probe_output(**overrides: str) -> str:
    values = {
        "BUILD": "29695",
        "UBR": "1001",
        "EDITION": "Professional",
        "LANG": "en-us",
        "FREE_GB": "41.3",
        "CPUS": "4",
        "TASK": "none",
        "SETUP_RUNNING": "False",
        "STATE": "",
        "JOB": "",
        "SETUPERR": "",
        "DEBUG": "Yes",
    }
    values.update(overrides)
    return "".join(f"{k}={v}\r\n" for k, v in values.items())


@pytest.fixture
def uup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(upgrade, "_fetch_json", _api())
    zip_path = tmp_path / upgrade.WIMLIB_ZIP
    zip_path.write_bytes(b"PK")

    async def fake_zip() -> str:
        return str(zip_path)

    monkeypatch.setattr(upgrade, "_wimlib_zip", fake_zip)


def _decoded(command: str) -> str:
    return base64.b64decode(command.rsplit(" ", 1)[1]).decode("utf-16-le")


async def test_builds_lists_amd64_insider_builds_newest_first(
    service: NtDriveService, uup: None
) -> None:
    listed = await service.call("vm_upgrade_builds", {})
    assert [b["build"] for b in listed["builds"]] == ["29698.1000", "29695.1000"]
    assert listed["builds"][0]["uuid"] == UUID


async def test_upgrade_stages_the_pipeline_and_starts_it(
    service: NtDriveService, fake_transport: FakeTransport, uup: None
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    fake_transport.exec_responses[upgrade._encoded(upgrade.PROBE_PS)] = _probe_output()
    fake_transport.exec_responses[upgrade._encoded(upgrade.START_PS)] = "STARTED=1\r\n"
    started = await service.call(
        "vm_upgrade", {"vm": "win11-dev", "build": "29698", "confirm": True}
    )
    assert started["target_build"] == "29698.1000"
    assert started["from_build"] == "29695.1001"
    assert started["edition"] == "Professional" and started["lang"] == "en-us"
    root = upgrade.GUEST_ROOT
    job = json.loads(fake_transport.files[f"{root}\\job.json"])
    assert job["target_build"] == "29698" and job["uuid"] == UUID
    assert job["metadata"] == "MetadataESD_professional_en-us.esd"
    # Only this edition's metadata, the package ESDs and the FoD CABs: no other edition, no
    # .NET or Edge payload the install image does not reference.
    names = [
        line.split(",", 1)[0]
        for line in fake_transport.files[f"{root}\\files.csv"].decode().splitlines()[1:]
    ]
    assert sorted(names) == sorted(
        [
            "MetadataESD_professional_en-us.esd",
            "Microsoft-Windows-Client-Desktop-Required-Package.esd",
            "cabs_Microsoft-Windows-Wifi-Client-Realtek-Rtl8192se-FOD-Package-amd64.cab",
        ]
    )
    assert started["files"] == 3
    pipeline = fake_transport.files[f"{root}\\pipeline.ps1"].decode()
    assert "--ref=" in pipeline and "bcdedit.exe" in pipeline and "/auto upgrade" in pipeline
    assert f"{root}\\wimlib.zip" in fake_transport.files
    scripts = [_decoded(c) for c in fake_transport.exec_log if "EncodedCommand" in c]
    assert any("Register-ScheduledTask" in s and "SYSTEM" in s for s in scripts)
    # The work folder is wiped before the new files land, so a stale state.json cannot leak in.
    prepare = next(i for i, s in enumerate(scripts) if "rd /s /q C:\\ntdrive-upgrade" in s)
    start = next(i for i, s in enumerate(scripts) if "Start-ScheduledTask" in s)
    assert prepare < start


async def test_upgrade_needs_confirm(service: NtDriveService, uup: None) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as err:
        await service.call("vm_upgrade", {"vm": "win11-dev"})
    assert err.value.code == "confirm_required"


async def test_upgrade_refuses_while_kd_is_attached(service: NtDriveService, uup: None) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    service.runtime("win11-dev").kd_state = KdState.RUNNING
    with pytest.raises(NtDriveError) as err:
        await service.call("vm_upgrade", {"vm": "win11-dev", "confirm": True})
    assert err.value.code == KD_ALREADY_ATTACHED


async def test_upgrade_refuses_a_downgrade_and_a_full_disk(
    service: NtDriveService, fake_transport: FakeTransport, uup: None
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    probe = upgrade._encoded(upgrade.PROBE_PS)
    fake_transport.exec_responses[probe] = _probe_output(BUILD="29698")
    with pytest.raises(NtDriveError) as err:
        await service.call("vm_upgrade", {"vm": "win11-dev", "confirm": True})
    assert err.value.code == INVALID_ARGS and "already runs build 29698" in err.value.message
    fake_transport.exec_responses[probe] = _probe_output(FREE_GB="12.5")
    with pytest.raises(NtDriveError) as err:
        await service.call("vm_upgrade", {"vm": "win11-dev", "confirm": True})
    assert "12.5 GB free" in err.value.message


async def test_upgrade_refuses_a_file_that_is_not_on_a_microsoft_host(
    service: NtDriveService,
    fake_transport: FakeTransport,
    uup: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = dict(FILES)
    files.update([_file("Microsoft-Evil-Package.esd", host="http://cdn.example.net/")])
    monkeypatch.setattr(upgrade, "_fetch_json", _api(files))
    await service.call("vm_start", {"vm": "win11-dev"})
    fake_transport.exec_responses[upgrade._encoded(upgrade.PROBE_PS)] = _probe_output()
    with pytest.raises(NtDriveError) as err:
        await service.call("vm_upgrade", {"vm": "win11-dev", "confirm": True})
    assert err.value.code == BACKEND_ERROR and "cdn.example.net" in err.value.message
    assert not [k for k in fake_transport.files if k.startswith(upgrade.GUEST_ROOT)]


def _status(**probe: str) -> dict[str, Any]:
    values = dict(line.split("=", 1) for line in _probe_output(**probe).splitlines())
    return upgrade._status(
        "vm",
        values,
        upgrade._json_or_empty(values["JOB"]),
        upgrade._json_or_empty(values["STATE"]),
    )


JOB = json.dumps({"target_build": "29698", "target_full": "29698.1000"})


def test_status_reads_each_stage() -> None:
    assert _status()["status"] == "none"
    building = _status(
        JOB=JOB, TASK="Running", STATE=json.dumps({"phase": "refs", "detail": "3 of 67"})
    )
    assert building["status"] == "building" and building["detail"] == "3 of 67"
    assert _status(JOB=JOB, STATE=json.dumps({"phase": "refs"}))["status"] == "interrupted"
    assert (
        _status(JOB=JOB, SETUP_RUNNING="True", STATE=json.dumps({"phase": "setup"}))["status"]
        == "upgrading"
    )
    done = _status(JOB=JOB, BUILD="29698", UBR="1000", STATE=json.dumps({"phase": "setup"}))
    assert done["status"] == "done" and any("kd_setup_guest" in step for step in done["next"])
    expired = _status(
        JOB=JOB, STATE=json.dumps({"phase": "failed", "detail": "curl.exe exited 22: "})
    )
    assert expired["status"] == "failed" and "expire" in expired["note"]
    rolled_back = _status(
        JOB=JOB,
        SETUPERR="Error 0xC1900101 | rollback",
        STATE=json.dumps({"phase": "setup_exited", "setup_exit": "0xC1900101"}),
    )
    assert rolled_back["status"] == "failed" and "0xC1900101" in rolled_back["setup_errors"]
    # A healthy setup also writes errors to setuperr.log, so they alone are not a failure.
    noisy = _status(
        JOB=JOB,
        SETUPERR="Error [0x080831] MIG CSIAgent: Invalid xml format",
        STATE=json.dumps({"phase": "setup"}),
    )
    assert noisy["status"] == "stalled"
    assert (
        _status(JOB=JOB, STATE=json.dumps({"phase": "setup_exited", "setup_exit": "0x00000000"}))[
            "status"
        ]
        == "stalled"
    )


async def test_status_says_offline_when_ssh_is_down(service: NtDriveService) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})

    async def down(ip: str, port: int) -> bool:
        return False

    service.ssh_probe = down
    status = await service.call("vm_upgrade_status", {"vm": "win11-dev"})
    assert status["status"] == "offline"
