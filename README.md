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
- For KDNET, the default: a firewall rule that lets `kd.exe` receive UDP. `kd_setup_host` creates
  it through one UAC prompt. The `serial` transport needs no firewall and no prompt. See "Kernel
  debugging" below.

Guest (Windows 10 or 11 x64):

- UEFI Secure Boot turned off in the VM settings (needed for `bcdedit /debug on`)
- VMware Tools installed
- OpenSSH Server running, with PowerShell as the default shell (see "Guest setup" below)
- A local user account for SSH
- For KDNET, the default: the virtual NIC set to `e1000e` (the Intel 82574L, which KDNET supports
  on every Windows 10/11 build). `vmxnet3` works only on Windows 11 23H2 and later.

`scripts/setup-host.ps1`, `scripts/setup-guest.ps1` and `scripts/probe-guest.ps1` automate most of
this.

## Setup in order

`ntdrive sys health` names the fix for every problem it finds (a missing vmx, Secure Boot
on, the wrong NIC for KDNET, an encrypted VM without a password, an empty password variable, a
blocked firewall, a missing serial pipe), so run it after each host step until the VM has no
`issues` left.

1. Host tools: VMware Workstation Pro, the Debugging Tools for Windows and uv (see Requirements),
   then `git clone` and `cd ntdrive`.
2. Host, one command, no admin: `powershell -ExecutionPolicy Bypass -File scripts\setup-host.ps1`.
   It runs `uv sync`, then `ntdrive setup` for each VM you pick (guest account and passwords,
   hidden, stored as User environment variables and never in a file), restarts the daemon, runs
   `kd setup-host` for each VM (net: one UAC prompt for the firewall. serial: the pipe goes into
   the vmx, so the VM must be off) and ends with `sys health`. Run it again to add a VM.
3. VM settings, with the VM off: Secure Boot off (Options > Advanced), NAT networking, and the
   `e1000e` NIC for KDNET (the default).
4. Guest: install VMware Tools, create a local account with a password, copy
   `scripts/setup-guest.ps1` in (drag and drop works once Tools are in) and run it from any
   PowerShell: it asks for administrator rights itself, one UAC click. It installs OpenSSH, and
   `kd setup-guest` does the bcdedit part from the host afterwards (`-Serial` sets up the serial
   transport in the guest instead). Reboot when it says so.
5. Host: `vm start`, `term open`, `kd setup-guest` (unless the guest script already did it), a
   soft `vm reboot`, `kd attach`, `kd break`. The "Kernel debugging" section below has the exact
   commands for each transport.

## Guest setup

The terminal, file transfer and screenshot tools work as soon as the guest has VMware Tools, an
account, and OpenSSH Server. The recommended way to install OpenSSH:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic; Start-Service sshd
New-ItemProperty -Path HKLM:\SOFTWARE\OpenSSH -Name DefaultShell `
  -Value C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -PropertyType String -Force
```

`Add-WindowsCapability` fails on Insider builds and on some offline images, because Windows Update
publishes no Feature-on-Demand package for them. The build-independent alternative is the standalone
[Win32-OpenSSH](https://github.com/PowerShell/Win32-OpenSSH/releases) zip: expand `OpenSSH-Win64.zip`
to `C:\Program Files\OpenSSH`, run its `install-sshd.ps1`, then start the `sshd` service.

`scripts/setup-guest.ps1` does all of this in one run. It tries the capability first and falls back
to the zip on its own (downloaded from GitHub, or pass `-OpenSshZip` with a local copy for a guest
without internet), sets the default shell and the firewall rule, and with `-Serial` (or `-HostIp`
for KDNET) also runs the `bcdedit` step that `kd_setup_guest` would otherwise do over SSH. Copy it
into the guest (VMware drag and drop, or `file_push`, which falls back to VMware Tools while SSH is
not up yet) and run it from any PowerShell. It asks for administrator rights itself, so one UAC
click replaces opening an elevated shell:

```powershell
powershell -ExecutionPolicy Bypass -File setup-guest.ps1
```

`kd_setup_guest` does the `bcdedit` step over SSH afterwards for the configured transport, so the
flags are optional. Reboot the guest when it says so. Running it again on a guest that already has `sshd` is safe.

## Install

```powershell
git clone https://github.com/jiy2745/ntdrive
cd ntdrive
powershell -ExecutionPolicy Bypass -File scripts\setup-host.ps1   # uv tool install -e ., ntdrive setup, kd setup-host, sys health
```

That installs three commands on your PATH with `uv tool install`: `ntdrive` (the CLI), `ntdrive-mcp`
(the MCP server) and `ntdrived` (the daemon, started for you). The install is editable, so the
commands run the code in the clone and follow `git pull`. Without a clone:

```powershell
uv tool install git+https://github.com/jiy2745/ntdrive
ntdrive setup                    # pick the VM, enter the guest account and passwords
```

Later, `ntdrive setup` on its own adds or changes a VM, and `uv tool upgrade ntdrive` updates a
clone-less install.

`ntdrive setup` writes one VM entry into `%LOCALAPPDATA%\ntdrive\vms.yaml` (or the file that
`--config` or `NTDRIVE_CONFIG` names), stores the passwords as User environment variables,
restarts the daemon and prints what `sys health` still complains about. Run it again to add a VM or change one. `--inline-secrets` keeps the passwords
in the file instead, and `vms.example.yaml` documents every field for editing by hand.

`ntdrive` looks for `vms.yaml` in `NTDRIVE_CONFIG`, then `%LOCALAPPDATA%\ntdrive`. It never looks
in the working directory: the config belongs to the user, not to a checkout of this repository.
The CLI and the MCP server hand the file they find to the daemon they start, and a daemon that
came up before the file existed is restarted on it by the next command.

Register the MCP server with an MCP client. The command is the installed `ntdrive-mcp`, with no
path and no `uv run`, so it does not matter which directory the client starts it from. Claude Code
picks up the bundled `.mcp.json` in a clone, or add the server to your own configuration:

```json
{ "mcpServers": { "ntdrive": { "command": "ntdrive-mcp" } } }
```

```powershell
claude mcp add --scope user ntdrive ntdrive-mcp
```

Allow the tools with one permission rule: `mcp__ntdrive__*`. Because the wait tools long-poll, set the
MCP tool-call timeout above the server cap (600 s by default).

## Kernel debugging: pick a transport

ntdrive can attach the kernel debugger two ways. `kd_transport` in `vms.yaml` chooses per VM, and
`ntdrive setup` writes `net` unless told otherwise.

**net (KDNET, the default).** kd.exe receives UDP from the guest over VMnet8, so the host firewall
must let it through. On many machines Windows has a leftover inbound Block rule for kd.exe that
silently drops KDNET (a Block rule beats an Allow rule). Windows creates those "Query User" rules
itself when kd.exe first listens, the firewall prompt appears and nobody clicks Allow.
`kd_setup_host` reads the rules, and when they block kd.exe it removes the Block rules and adds an
Allow rule through a single UAC prompt:

```powershell
ntdrive kd setup-host win11     # checks the firewall, repairs it once you approve the UAC prompt
ntdrive kd setup-guest win11    # generates the KDNET key and runs bcdedit /dbgsettings net over SSH
ntdrive vm reboot win11 --mode soft --confirm
ntdrive kd attach win11         # waiting until the guest boots with the debugger on
```

`sys health` runs the same firewall check and lists the offending rules. Pass `--no-fix-firewall`
to only look. `scripts\setup-host.ps1 -FirewallOnly` from an Administrator PowerShell does the same
repair by hand. The guest needs a KDNET-capable NIC (`e1000e`).

**serial (no network, no prompt).** kd.exe talks to the guest over a VMware serial port exposed as
a host named pipe (`\\.\pipe\ntdrive-<vm>`). A named pipe is local IPC, so there is no firewall
and nothing to approve, which makes it the choice for a host where nobody can answer a UAC prompt.
It is a little slower than net. Set `kd_transport: serial` (or `ntdrive setup --transport serial`),
then, with the VM powered off:

```powershell
ntdrive kd setup-host win11     # VM off: adds the named-pipe serial port to the vmx (idempotent)
ntdrive vm start win11
ntdrive kd setup-guest win11    # runs bcdedit /dbgsettings serial in the guest over SSH
ntdrive vm reboot win11 --mode soft --confirm
ntdrive kd attach win11         # running at once; kd break syncs with the target
```

`sys health` tells you when the vmx still lacks the pipe entry, and `kd attach` refuses with a
clear hint when the pipe is not open on the host (the VM is off or was started before the vmx
edit).

## Quick start (CLI)

The CLI has the same tools as subcommands. The first call auto-starts the daemon. This assumes the
guest is already set up for your chosen transport (see above).

The daemon and the vmrun and kd.exe processes it starts run without console windows, so nothing
pops up on the desktop. `ntdrive daemon status` says whether it is up, and its own output
goes to `%LOCALAPPDATA%\ntdrive\logs\daemon.out.log`.

```powershell
ntdrive sys health                 # host binaries and config, then each VM live: power, SSH, debugger transport
ntdrive vm start win11
ntdrive term open win11            # prints a session id and a CoView URL
ntdrive kd attach win11            # serial: attaches at once; net: connects as the guest boots
ntdrive kd break win11             # freezes the guest at a kd> prompt
ntdrive kd exec win11 "!process 0 0"
ntdrive kd go win11                # resume the guest
```

Add `--json` to any command for the raw tool result. Exit codes: 0 ok, 1 error, 2 bad arguments,
3 confirmation required, 4 guest frozen by the debugger, 5 timeout.

A person can sit down in a session the agent opened:

```powershell
ntdrive term attach <session-id>   # Ctrl+] to detach, your keystrokes are logged as human
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
