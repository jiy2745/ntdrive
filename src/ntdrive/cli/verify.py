"""`ntdrive verify`: prove that a VM is ready end to end, and say so plainly.

Runs the checks a person would otherwise do by hand after setup-host.cmd and setup-guest.cmd,
in order, and stops at the first failure with the fix: config and host (sys_health), the VM
running, an SSH login as the configured account, the debugger transport on the host (firewall
or serial pipe), then a real debugger round trip (attach, break in, resume, detach). A guest that
was just configured for KDNET and still needs a reboot is rebooted here. The last line is either
ALL SET or NOT READY with numbered next steps, and the exit code says the same.
"""

from __future__ import annotations

import contextlib
import json
import sys
from typing import Any

import click

from ntdrive.cli import log
from ntdrive.cli.log import Out
from ntdrive.daemon.client import DaemonClient, connect
from ntdrive.errors import NtDriveError

Report = list[dict[str, Any]]


def ssh_fix(message: str, vm: str, standard: str = "") -> str:
    """The fix for a failed SSH login, from the error text (`standard` names that account)."""
    lower = message.lower()
    if "authentication" in lower and standard:
        return (
            f"the password of {standard} is wrong: run ntdrive setup --name {vm} and, at the "
            "standard account prompt, type the name and password that setup-guest.cmd -Standard "
            "set in the guest, or run that there first"
        )
    if "authentication" in lower:
        return (
            f"the account or password is wrong: run ntdrive setup --name {vm} and type the "
            "account and password that setup-guest.cmd created in the guest (ntdrive by default), "
            "or run setup-guest.cmd there first"
        )
    if "guest ip" in lower or "vmware tools" in lower:
        return "VMware Tools are not running in the guest: install them (VM > Install VMware Tools)"
    return (
        "OpenSSH does not answer: in the guest run setup-guest.cmd (installs OpenSSH, one UAC "
        "click), then verify again"
    )


def verify_vm(client: DaemonClient, name: str, out: Out) -> Report:
    """Run the checks for one VM in order and stop at the first failure."""
    report: Report = []

    def passed(check: str, detail: str) -> None:
        report.append({"check": check, "ok": True, "detail": detail, "fix": ""})
        log.ok(check, detail, out)

    def failed(check: str, detail: str, fix: str) -> Report:
        report.append({"check": check, "ok": False, "detail": detail, "fix": fix})
        log.fail(check, detail, out)
        log.fix(fix, out)
        return report

    health = client.call("sys_health", {})
    if health.get("problems"):
        return failed(
            "host", str(health["problems"][0]), "install the missing piece, then verify again"
        )
    vm = next((v for v in health.get("vms", []) if v.get("name") == name), None)
    if vm is None:
        return failed(
            "config", f"{name} is not in vms.yaml", "run scripts\\setup-host.cmd or ntdrive setup"
        )
    # The key is read from the guest during the attach below, the SSH port and the firewall are
    # tested for real further down, so those live findings are not verdicts yet.
    later = ("kdnet key not set", "host firewall", "SSH port")
    blocking = [str(issue) for issue in vm.get("issues", []) if not str(issue).startswith(later)]
    if blocking:
        return failed("config", "vms.yaml or the VM settings", blocking[0])
    passed("config", f"{vm.get('kd_transport')} transport, {health.get('config_path')}")

    if vm.get("power") != "running":
        try:
            client.call("vm_start", {"vm": name})
        except NtDriveError as exc:
            return failed(
                "power", exc.message, exc.hint or "start the VM in VMware and look at its console"
            )
        passed("power", "was off, started it")
    else:
        passed("power", "running")

    guest = vm.get("guest") or {}
    try:
        opened = client.call("term_open", {"vm": name})
    except NtDriveError as exc:
        return failed("ssh", exc.message, ssh_fix(exc.message, name))
    with contextlib.suppress(NtDriveError):
        client.call("term_close", {"session_id": opened["session_id"]})
    passed("ssh", f"logged in as {guest.get('user') or 'the configured account'} and got a shell")
    standard = str(guest.get("standard_user") or "")
    if standard:
        # The optional second account, without administrator rights. Its password is a separate
        # secret, so a typo there would only show up on the first term_open account=standard.
        try:
            opened = client.call("term_open", {"vm": name, "account": "standard"})
        except NtDriveError as exc:
            return failed("ssh standard", exc.message, ssh_fix(exc.message, name, standard))
        with contextlib.suppress(NtDriveError):
            client.call("term_close", {"session_id": opened["session_id"]})
        passed("ssh standard", f"logged in as {standard}, a plain user, and got a shell")

    if vm.get("kd_transport") == "net":
        try:
            host_side = client.call("kd_setup_host", {"vm": name, "fix_firewall": False})
        except NtDriveError as exc:
            return failed("firewall", exc.message, exc.hint or "run scripts\\setup-host.cmd")
        firewall = host_side.get("firewall", {})
        if not firewall.get("ok"):
            rules = firewall.get("block_rules") or []
            detail = (
                "inbound Block rules for kd.exe: " + ", ".join(rules)
                if rules
                else "no inbound Allow rule for kd.exe UDP"
            )
            return failed(
                "firewall",
                detail,
                "run scripts\\setup-host.cmd (one UAC prompt), or scripts\\setup-host.cmd "
                "-FirewallOnly from an Administrator shell",
            )
        passed("firewall", "kd.exe may receive KDNET")
    else:
        pipe = vm.get("serial_pipe") or {}
        if pipe.get("open") is False:
            return failed(
                "serial pipe",
                f"{pipe.get('path')} has no server on the host",
                f"power the VM off, run ntdrive kd setup-host {name}, then start it again",
            )
        passed("serial pipe", str(pipe.get("path")))

    state = client.call("kd_state", {"vm": name})
    attached_here = False
    if state.get("state") == "detached":
        try:
            log.running("debugger", "attaching, this waits for the target", out)
            client.call("kd_attach", {"vm": name, "timeout": 120})
        except NtDriveError as exc:
            if "needs a reboot" not in exc.message:
                return failed("debugger", exc.message, exc.hint or "look at the guest console")
            # setup-guest.cmd (or the attach itself) configured KDNET a moment ago. The reboot is
            # part of the setup, so do it here rather than sending the person back and forth.
            log.running("debugger", "KDNET was configured in the guest just now, rebooting it", out)
            try:
                client.call(
                    "vm_reboot",
                    {
                        "vm": name,
                        "mode": "soft",
                        "confirm": True,
                        "reattach_kd": False,
                        "reopen_term": False,
                    },
                )
                client.call("kd_attach", {"vm": name, "timeout": 180})
            except NtDriveError as again:
                return failed("debugger", again.message, again.hint or "look at the guest console")
        attached_here = True
    elif state.get("state") == "broken":
        client.call("kd_go", {"vm": name})
    try:
        broke = client.call("kd_break", {"vm": name, "timeout": 30})
        client.call("kd_go", {"vm": name})
    except NtDriveError as exc:
        return failed(
            "debugger",
            exc.message,
            exc.hint or "reboot the guest so it boots with the debugger on, then verify again",
        )
    finally:
        if attached_here:
            with contextlib.suppress(NtDriveError):
                client.call("kd_detach", {"vm": name})
    target = str(broke.get("target_info") or "target connected")
    passed("debugger", f"attached, broke in and resumed ({target})")
    return report


def verify_command() -> click.Command:
    """The `ntdrive verify` click command."""

    @click.command(
        "verify",
        help=(
            "Prove that a VM is ready: config, power, SSH login, the debugger transport, then a "
            "real attach, break in and resume. Ends with ALL SET or the first thing to fix. "
            "Without a VM name every configured VM is checked."
        ),
    )
    @click.argument("vms", nargs=-1)
    @click.pass_context
    def verify(ctx: click.Context, vms: tuple[str, ...]) -> None:
        obj = ctx.ensure_object(dict)
        as_json = bool(obj.get("json"))
        out: Out = (lambda line: None) if as_json else click.echo
        report: dict[str, Report] = {}
        try:
            client = connect(obj.get("config"), autostart=True, caller="cli")
            names = list(vms) or [
                str(v.get("name")) for v in client.call("vm_list", {}).get("vms", [])
            ]
            for index, name in enumerate(names, start=1):
                log.section(index, len(names), name, out)
                report[name] = verify_vm(client, name, out)
        except NtDriveError as exc:
            # main imports this module at load, so the back-import waits until a call.
            from ntdrive.cli.main import fail

            fail(ctx, exc)
            return
        ready = bool(names) and all(all(c["ok"] for c in checks) for checks in report.values())
        if as_json:
            click.echo(json.dumps({"ready": ready, "vms": report}, indent=2))
        elif not names:
            log.verdict("NOT READY", "no VM is configured")
            log.next_steps(["run scripts\\setup-host.cmd (or ntdrive setup) and pick the VM"])
        elif ready:
            log.verdict(
                "ALL SET",
                ", ".join(names)
                + " ready. SSH login, debugger attach, break in and resume all worked.",
            )
        else:
            bad = {n: checks[-1] for n, checks in report.items() if not checks[-1]["ok"]}
            log.verdict("NOT READY", ", ".join(bad))
            log.next_steps(
                [f"{n} ({last['check']}): {last['fix']}" for n, last in bad.items()]
                + ["then run ntdrive verify again (or scripts\\setup-host.cmd -Verify)"]
            )
        if not ready:
            sys.exit(1)

    return verify
