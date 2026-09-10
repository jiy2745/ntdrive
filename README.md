# ntdrive

[![CI](https://github.com/jiy2745/ntdrive/actions/workflows/ci.yml/badge.svg)](https://github.com/jiy2745/ntdrive/actions/workflows/ci.yml)

Drive a Windows guest on VMware Workstation the way a person sitting at the machine would, but from
an LLM agent. ntdrive gives an agent power and snapshot control, KDNET kernel debugging, and a
real-time terminal on the guest, all behind one local daemon and one set of tools.

It is built for the kernel driver and Windows security loop: build, deploy to the guest, load, hit a
crash or breakpoint, analyze in the debugger, revert a snapshot, repeat. Each of those steps is a
tool call, and the daemon keeps the pieces consistent so the agent does not have to.

Windows only. The name is the point: `NT` is the Windows kernel, and ntdrive drives it.

## Why it exists

An agent that wants to debug a driver needs three windows at once: the VMware controls, WinDbg, and
an SSH or RDP session into the guest. ntdrive exposes all three as tools and, more importantly, keeps
their state in sync. Revert a snapshot and the debugger reattaches and the terminal reconnects on its
own. Break into the debugger and the terminal tools refuse to hang, because the guest is frozen. That
cross-tool consistency is the part existing debugger-only or VM-only tools leave to you.

## How it fits together

```
 Claude Code (agent)      Human / CI (shell)      pytest / automation
        | stdio                  | argv                  | import
        v                        v                       v
   ntdrive-mcp              ntdrive CLI             Python SDK (NtDrive)
        |                        |                       |
        +------------------------+-----------------------+
                                 | HTTP + WebSocket, 127.0.0.1, token
                                 v
   +------------- ntdrived (daemon, holds every session) -------------+
   |  ToolRegistry -> StateStore -> Orchestrator -> AuditLog          |
   |  VmwareAdapter (vmrun)   KdSession (kd.exe)   TermManager (SSH)   |
   +---------+------------------------+-----------------------+--------+
             | vmrun.exe              | UDP KDNET             | TCP 22 (SSH PTY)
             v                        v                       v
   +------------------------ VM (Windows guest) -----------------------+
   |  KDNET (boot-time)   OpenSSH -> PowerShell PTY   VMware Tools      |
   +-------------------------------------------------------------------+
```

The daemon owns the sessions. The MCP server, the CLI and the SDK are thin, stateless clients of it,
so an agent, a person at a shell, and a test script all see the same live sessions. Every tool is
declared once in a single registry (`ntdrive.core.registry`) and the three front doors are generated
from it, so they never drift apart.

## What an agent can do

- **VM power and snapshots**: start, stop, suspend, three reboot modes, and live snapshots with a
  tree listing, revert, and delete.
- **KDNET kernel debugging**: set up KDNET in the guest, attach `kd.exe`, break in, run debugger
  commands, wait for a bugcheck or breakpoint, and detach.
- **Real-time terminal**: open SSH PTY sessions, stream output, render the screen, wait on a regex,
  and send keys including `{ctrl+c}`.
- **Console and files**: capture a console screenshot (for a BSOD or login screen) and copy files
  both ways with checksum verification.
- **Unified state**: one `sys_state` call returns VM power, debugger state and terminal sessions,
  and compound actions like snapshot revert run as a single orchestrated step.

## Requirements

Host:

- Windows 11
- VMware Workstation Pro 17.6 or newer (provides `vmrun.exe`)
- Debugging Tools for Windows (`kd.exe`, `kdnet.exe`) from the Windows SDK or WDK
- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- For `net` kernel debugging only: a firewall rule that lets `kd.exe` receive UDP (admin, once).
  The `serial` transport needs no firewall and no admin. See "Kernel debugging" below.

Guest (Windows 10 or 11 x64):

- UEFI Secure Boot turned off in the VM settings (needed for `bcdedit /debug on`)
- VMware Tools installed
- OpenSSH Server running, with PowerShell as the default shell (see "Guest setup" below)
- A local user account for SSH
- For `net` kernel debugging only: the virtual NIC set to `e1000e` (the Intel 82574L, which KDNET
  supports on every Windows 10/11 build). `vmxnet3` works only on Windows 11 23H2 and later.

`scripts/setup-host.ps1`, `scripts/setup-guest.ps1` and `scripts/probe-guest.ps1` automate most of
this.

## Guest setup

The terminal, file transfer and screenshot tools work as soon as the guest has VMware Tools, an
account, and OpenSSH Server. The recommended way to install OpenSSH:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic; Start-Service sshd
New-ItemProperty -Path HKLM:\SOFTWARE\OpenSSH -Name DefaultShell `
  -Value C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -PropertyType String -Force
```

If `Add-WindowsCapability` fails (some offline images and Insider builds have no Feature-on-Demand
source), install the standalone build instead: download `OpenSSH-Win64.zip` from the
[Win32-OpenSSH releases](https://github.com/PowerShell/Win32-OpenSSH/releases), expand it to
`C:\Program Files\OpenSSH`, and run its `install-sshd.ps1`, then start the `sshd` service.
`scripts/setup-guest.ps1` does the Add-WindowsCapability path for you, and with `-Serial` (or
`-HostIp` for KDNET) it also runs the `bcdedit` step that `kd_setup_guest` would otherwise do over
SSH.

## Install

```powershell
git clone https://github.com/jiy2745/ntdrive
cd ntdrive
uv sync
uv run pre-commit install
copy vms.example.yaml vms.yaml
# edit vms.yaml: vmx path, guest user and password env var, VMnet8 host IP,
# and encryption_password_env if the VM is encrypted
```

Register the MCP server with an MCP client. Claude Code picks up the bundled `.mcp.json`, or add the
same entry to your own config:

```json
{ "mcpServers": { "ntdrive": { "command": "uv", "args": ["run", "ntdrive-mcp"] } } }
```

Allow the tools with one permission rule: `mcp__ntdrive__*`. Because the wait tools long-poll, set the
MCP tool-call timeout above the server cap (600 s by default).

## Kernel debugging: pick a transport

ntdrive can attach the kernel debugger two ways. Set `kd_transport` per VM in `vms.yaml`.

**serial (recommended, no admin).** kd.exe talks to the guest over a VMware serial port exposed as
a host named pipe (`\\.\pipe\ntdrive-<vm>`). A named pipe is local IPC, so there is no network, no
host firewall, and no administrator step. It is a little slower than net, which rarely matters.

One-time setup (the VM must be powered off to add the serial port):

```powershell
uv run ntdrive kd setup-host win11     # VM off: adds the named-pipe serial port to the vmx (idempotent)
uv run ntdrive vm start win11
uv run ntdrive kd setup-guest win11    # runs bcdedit /dbgsettings serial in the guest over SSH
uv run ntdrive vm reboot win11 --mode soft --confirm
uv run ntdrive kd attach win11         # running at once; kd break syncs with the target
```

`sys health` tells you when the vmx still lacks the pipe entry, and `kd attach` refuses with a
clear hint when the pipe is not open on the host (the VM is off or was started before the vmx
edit).

**net (KDNET, faster, needs admin once).** kd.exe receives UDP from the guest, so the host firewall
must allow it. On many machines Windows has a leftover inbound Block rule for kd.exe that silently
drops KDNET (a Block rule beats an Allow rule). Run the host setup as Administrator once. It removes
any such Block rule and adds an Allow rule:

```powershell
# Administrator PowerShell, once:
powershell -ExecutionPolicy Bypass -File scripts\setup-host.ps1
```

The guest also needs a KDNET-capable NIC (`e1000e`) and `kd_transport: net` in `vms.yaml`.

## Quick start (CLI)

The CLI has the same tools as subcommands. The first call auto-starts the daemon. This assumes the
guest is already set up for your chosen transport (see above).

The daemon and the vmrun and kd.exe processes it starts run without console windows, so nothing
pops up on the desktop. `uv run ntdrive daemon status` says whether it is up, and its own output
goes to `%LOCALAPPDATA%\ntdrive\logs\daemon.out.log`.

```powershell
uv run ntdrive sys health                 # host binaries and config, then each VM live: power, SSH, debugger transport
uv run ntdrive vm start win11
uv run ntdrive term open win11            # prints a session id and a CoView URL
uv run ntdrive kd attach win11            # serial: attaches at once; net: connects as the guest boots
uv run ntdrive kd break win11             # freezes the guest at a kd> prompt
uv run ntdrive kd exec win11 "!process 0 0"
uv run ntdrive kd go win11                # resume the guest
```

Add `--json` to any command for the raw tool result. Exit codes: 0 ok, 1 error, 2 bad arguments,
3 confirmation required, 4 guest frozen by the debugger, 5 timeout.

A person can sit down in a session the agent opened:

```powershell
uv run ntdrive term attach <session-id>   # Ctrl+] to detach, your keystrokes are logged as human
```

## The state model to respect

- While the debugger is broken in (`kd_state == broken`) the whole guest is frozen. Terminal, file
  and screenshot tools return `guest_frozen_by_debugger` at once instead of hanging. Run `kd go`
  first.
- `snap revert` and `vm reboot` detach the debugger and drop terminal sessions, then reattach and
  reopen them unless you pass `--no-reattach-kd` or `--no-reopen-term`.
- After a reconnect the old terminal session id is kept and points to its successor.

## Encrypted VMs

Set `encryption_password_env` (the name of an environment variable, preferred) or
`encryption_password` (inline, acceptable because `vms.yaml` is git-ignored) and every operation
that opens the vmx passes `-vp` to vmrun. Power, snapshot list, revert, delete, screenshot, guest IP and
file copy all work on an encrypted VM, including the partial encryption that a Windows 11 vTPM
requires.

The one rough edge is a **live snapshot of a running encrypted VM**. vmrun refuses to encrypt the
running memory directly and returns an authentication error, even though the password is correct
(verified: `deleteSnapshot` and every other op accept the same password, and a snapshot of the
same VM while powered off succeeds). Two ways to get a live-state snapshot anyway:

- `snap_take <vm> <name> --allow-suspend`. ntdrive suspends the VM (its memory is written to the
  encrypted `.vmss`), snapshots the saved state, then resumes. The snapshot includes the running
  state and it is fully headless with just the encryption password. The guest pauses for a few
  seconds during the suspend and resume. Like `vm_suspend` this refuses while the debugger is
  broken in, drops terminal sessions (reopen them with `term_open`) and reattaches the debugger
  afterwards. The result lists `terms_dropped` and the `kd` reattach status.
- Or take the snapshot from the VMware UI, which snapshots the running VM in place.

A plain powered-off `snap_take` always works. `snap_take` without `--allow-suspend` on a running
encrypted VM returns a specific hint instead of a raw error. Deleting a memory snapshot of a
running encrypted VM has the same limitation, and `snap_delete --allow-suspend` handles it the
same way (suspend, delete, resume).

## Safety

- The daemon binds only to `127.0.0.1` and checks a per-daemon token on every request.
- Secrets (guest password, KDNET key, VM encryption password, daemon token) live only in `vms.yaml`,
  environment variables, and the daemon state file. They never appear in tool arguments, results or
  logs, and the audit log masks them.
- The guest's SSH host key is pinned per VM on first use under `%LOCALAPPDATA%\ntdrive\hostkeys`.
  A different key later is refused before the password is sent. Delete that file after
  reinstalling a guest.
- The CoView URL carries a view token, a second secret that only lists terminal sessions and opens
  their streams. That is still a shell in the guest, so treat it like a password and do not paste
  it into chat or tickets. Terminal transcripts under the log directory record everything typed into a
  session, by the agent or by a person, so passwords typed interactively land there too.
- `kd_exec` executes any debugger command, and kd's `.shell` runs commands on the host. The
  policy file can set `kd_exec: deny` for agents that should not have that.
- Destructive actions (`snap_delete`, hard `vm_stop`, hard `vm_reboot`) require `confirm=true` and go
  through a policy gate you can tune in `policy.yaml`.

## Documentation map

| File | Read it if you are |
|---|---|
| `README.md` | a human setting up or using ntdrive |
| `SKILL.md` | an agent using the tools. State model, standard procedures, and moves to avoid |
| `AGENTS.md` | any coding agent changing this repo. Conventions, commands and where things live |
| `CLAUDE.md` | Claude Code. Points at `AGENTS.md` and adds the Claude-specific bits |
| `PRD.md` | anyone who wants the full requirements and design rationale |

`SKILL.md` is written for the agent. MCP clients that read skill files pick it up, and any agent can
be pointed at it. It covers the state model, the setup and debug-loop and BSOD-recovery procedures,
and the forbidden moves (such as touching the terminal while the debugger is broken in).

## Development

```powershell
uv sync
uv run pytest                     # 47 unit tests, all fakes, no real VM needed
uv run pre-commit run --all-files # ruff format + lint, mypy, prettier, ASCII/style check
```

Conventions live in `AGENTS.md` and `pyproject.toml`. Ruff is the only Python formatter and linter,
mypy is strict for `ntdrive.core`, and Prettier runs on the CoView web assets only. A pre-commit hook
rejects non-ASCII characters and semicolons in Markdown prose, which keeps every document English and
plain. Tests that need a real VM are marked `@pytest.mark.vm` and skip when none is configured.

Only the VMware backend is implemented. Hyper-V and VirtualBox sit behind the same
`HypervisorAdapter` interface and are planned for a later version.

## Layout

```
src/ntdrive/
  config.py       vms.yaml and policy.yaml models
  errors.py       NtDriveError with code, hint and backend reason tags
  paths.py        host path helpers shared by the CLI, SDK and file tools
  core/           registry, state store, policy gate, audit log, orchestrator, service
  core/tools/     tool handlers: vm, snap, kd, term, console, file, sys
  hypervisor/     HypervisorAdapter interface and the VMware (vmrun) adapter
  kd/             KdSession around kd.exe
  term/           terminal transports, sessions, key tokens
  daemon/         ntdrived HTTP and WebSocket app, lifecycle, client, CoView page
  mcp/ cli/ sdk/  the three generated front doors
scripts/          host and guest setup, the ASCII/style check hook
tests/            unit tests with fake vmrun, fake kd.exe and fake terminal channels
```

## License

MIT. See `LICENSE`.
