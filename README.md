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

## Setup

Host: Windows 11, VMware Workstation Pro 17.6 or newer, the Debugging Tools for Windows (`kd.exe`
and `kdnet.exe` from the SDK or WDK), Python 3.12 and [uv](https://docs.astral.sh/uv/). No
administrator shell is needed. Guest: Windows 10 or 11 x64 with VMware Tools, Secure Boot off in
the VM settings (`bcdedit /debug on` needs that) and, for KDNET, the `e1000e` NIC. The guest script
creates the account SSH logs in with. `ntdrive sys health` names the fix for anything that is
missing, so run it whenever in doubt. Guest first, then host: the host script ends with the
end-to-end check and prints ALL SET, and it reboots the guest itself when the debugger needs it.

**1. Guest.** Copy `scripts\setup-guest.cmd` and `scripts\setup-guest.ps1` into the guest (drag and
drop works once VMware Tools are in) and run the `.cmd` from any shell or by double click. It asks
for administrator rights itself (one UAC click), creates a local administrator `ntdrive` and asks
for its password (type the same one in `ntdrive setup` on the host, `-NoAccount` uses your own
account instead), installs OpenSSH Server with PowerShell as the default shell, opens port 22 and
turns on KDNET (the host IP comes from the NAT gateway, the key
is generated in the guest and never needs copying). On Insider builds, where
`Add-WindowsCapability` has no package, it falls back to the Win32-OpenSSH zip (`-OpenSshZip
<file>` for a guest without internet). Running it again is safe. Reboot the guest when it says so.

Several VMs: run `setup-host.cmd` again (or `ntdrive setup`) for each VM and `setup-guest.cmd` in
each guest. Every guest picks its own KDNET port from its machine id, and the host moves a guest
whose port collides with another VM's.

**2. Host.** One command from a clone:

```powershell
git clone https://github.com/jiy2745/ntdrive
cd ntdrive
scripts\setup-host.cmd
```

It runs `uv sync`, puts the `ntdrive`, `ntdrive-mcp` and `ntdrived` commands on your PATH (an
editable `uv tool install`, so they follow the clone), then for each VM you pick from the VMware
library asks for the guest account and passwords (masked while you type, echoed partly masked so a typo shows, stored as User
environment variables, never in a file), restarts the daemon, repairs the host firewall for KDNET through one UAC prompt
and ends with `sys health`. Run it again to add a VM, or run `ntdrive setup` on its own. The config
lands in `%LOCALAPPDATA%\ntdrive\vms.yaml`, and `vms.example.yaml` documents every field. Without a
clone: `uv tool install git+https://github.com/jiy2745/ntdrive`, then `ntdrive setup`.

**3. Verify.** The host script already ran this at its end. Run it again after any change, or
whenever the guest was set up after the host:

```powershell
ntdrive verify                  # config, power, SSH login, firewall, then attach, break in, resume
```

It ends with `ALL SET` or the first thing to fix and how. `setup-host.cmd` runs it at the end,
and `setup-host.cmd -Verify` repeats only this part. KDNET is the default transport: the first
attach reads the key the guest script set over SSH, so nothing is copied by hand, and when the
guest was configured a moment ago verify reboots it itself. On a guest set up by hand,
`ntdrive kd setup-guest win11` writes the settings instead.

`sys health` shows the firewall state, and `scripts\setup-host.cmd -FirewallOnly` from an
Administrator shell is the manual repair. `kd_transport: serial` (a VMware named pipe: no firewall,
no prompt, a little slower) is the alternative for a host where nobody can approve a UAC prompt:
`ntdrive setup --transport serial`, `setup-guest.cmd -Serial` in the guest, then
`ntdrive kd setup-host win11` with the VM off, then the same commands.

**4. Claude Code.** A clone carries `.mcp.json`. Elsewhere register the installed command, and in
either case allow its tools with the permission rule `mcp__ntdrive__*` and set the MCP tool-call
timeout above 600 s, because the wait tools long-poll:

```json
{ "mcpServers": { "ntdrive": { "command": "ntdrive-mcp" } } }
```

## Quick start (CLI)

The CLI has the same tools as subcommands. The first call auto-starts the daemon.

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
