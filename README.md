# ntdrive

[![CI](https://github.com/jiy2745/ntdrive/actions/workflows/ci.yml/badge.svg)](https://github.com/jiy2745/ntdrive/actions/workflows/ci.yml)

Drive a Windows guest on VMware Workstation from an LLM agent: power and snapshots, kernel
debugging with kd.exe over KDNET or a serial pipe, and a real-time SSH terminal, behind one local
daemon and one set of tools exposed as an MCP server, a CLI and a Python SDK. It is built for the
kernel driver and Windows security loop: build, deploy to the guest, load, hit a crash or
breakpoint, analyze in the debugger, revert a snapshot, repeat. Each step is a tool call, and the
daemon keeps the pieces consistent so the agent does not have to. Windows only. The name is the
point: `NT` is the Windows kernel, and ntdrive drives it.

Needs: a Windows 11 host with VMware Workstation Pro 17.6 or newer, the Debugging Tools for
Windows (`kd.exe` and `kdnet.exe` from the Windows SDK or WDK), Python 3.12 and uv. Guest: Windows
10 or 11 x64 with VMware Tools, Secure Boot off and, for KDNET, the `e1000e` NIC. No administrator
shell on the host.

Fastest path in (guest first, then host):

1. In the guest, copy `scripts\setup-guest.cmd` and `scripts\setup-guest.ps1` in and run the `.cmd`.
2. On the host:

   ```powershell
   git clone https://github.com/jiy2745/ntdrive
   cd ntdrive
   scripts\setup-host.cmd
   ```

   It ends with `ntdrive verify` and prints ALL SET.
3. Point an MCP client at the installed `ntdrive-mcp` command (a clone carries `.mcp.json`).

Read next: `SKILL.md` if you are an agent using the tools, `AGENTS.md` if you are changing this
code, `PRD.md` for the requirements. The full map is under Documentation map below.

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

## Setup

Host: Windows 11, VMware Workstation Pro 17.6 or newer, the Debugging Tools for Windows (`kd.exe`
and `kdnet.exe` from the Windows SDK or WDK), Python 3.12 and [uv](https://docs.astral.sh/uv/). No
administrator shell is needed. Guest: Windows 10 or 11 x64 with VMware Tools, Secure Boot off in
the VM settings (`bcdedit /debug on` needs that) and, for KDNET, the `e1000e` NIC. Guest first,
then host: the host script ends with the end-to-end check and reboots the guest itself when the
debugger needs it. `ntdrive sys health` names the fix for anything that is missing.

**1. Guest.** Copy `scripts\setup-guest.cmd` and `scripts\setup-guest.ps1` into the guest (drag and
drop works once VMware Tools are in) and run the `.cmd` from any shell or by double click. It asks
for administrator rights itself (one UAC click), creates the local administrator `ntdrive` and
asks for its password (type the same one in `ntdrive setup` on the host), installs OpenSSH Server
with PowerShell as the default shell, opens port 22 and turns on KDNET. The host IP comes from the
NAT gateway and the key is generated in the guest, so nothing is copied by hand. Running it again
is safe. Reboot the guest when it says so.

**2. Host.** One command from a clone:

```powershell
git clone https://github.com/jiy2745/ntdrive
cd ntdrive
scripts\setup-host.cmd
```

It runs `uv sync`, puts the `ntdrive`, `ntdrive-mcp` and `ntdrived` commands on your PATH (an
editable `uv tool install`, so they follow the clone), then for each VM you pick from the VMware
library asks for the guest account and passwords (masked while you type, stored as User
environment variables, never in a file), restarts the daemon, repairs the host firewall for KDNET
through one UAC prompt and ends with `ntdrive verify`. The config lands in
`%LOCALAPPDATA%\ntdrive\vms.yaml`, and `vms.example.yaml` documents every field. Without a clone:
`uv tool install git+https://github.com/jiy2745/ntdrive`, then `ntdrive setup`.

**3. Verify.** The host script already ran this. Run it again after any change, or whenever the
guest was set up after the host:

```powershell
ntdrive verify                  # config, power, SSH login, firewall, then attach, break in, resume
```

It ends with `ALL SET` or the first thing to fix and how. When the guest was configured a moment
ago it reboots the guest itself so the debugger connects.

Options, all optional:

| Where | Switch | What it does |
|---|---|---|
| guest | `setup-guest.cmd -NoAccount` | use your own Windows account for SSH instead of creating `ntdrive` |
| guest | `setup-guest.cmd -Standard` | also create `ntdrive-user`, a plain account, for `term_open account=standard` |
| guest | `setup-guest.cmd -OpenSshZip <file or URL>` | OpenSSH from the Win32-OpenSSH zip, for a guest without internet (Insider builds have no capability package, and the script falls back to a download on its own) |
| guest | `setup-guest.cmd -Serial` | serial named-pipe transport instead of KDNET |
| host | `setup-host.cmd -Verify` | only the end-to-end check |
| host | `setup-host.cmd -FirewallOnly` | manual repair of the KDNET firewall rules, from an Administrator shell |
| host | `ntdrive setup --transport serial` | serial transport for a host where nobody can approve a UAC prompt, then `ntdrive kd setup-host <vm>` with the VM off |

Several VMs: run `setup-host.cmd` again (or `ntdrive setup`) for each VM and `setup-guest.cmd` in
each guest. Every guest picks its own KDNET port from its machine id, and the host moves a guest
whose port collides with another VM's. A guest set up by hand gets its KDNET settings from
`ntdrive kd setup-guest <vm>`. `scripts\probe-guest.ps1` prints what a guest has (build, Secure
Boot, NIC model, OpenSSH state) before you run the setup script.

**4. MCP clients.** The server is the installed `ntdrive-mcp` command: stdio, no arguments, no
environment variables (the config lives in `%LOCALAPPDATA%\ntdrive`), and it starts the daemon
itself. Set the client's MCP tool-call timeout above 600 s, because the wait tools long-poll.

Claude Code: a clone carries `.mcp.json`, elsewhere `claude mcp add ntdrive -- ntdrive-mcp`. Allow
the tools with the permission rule `mcp__ntdrive__*`.

```json
{ "mcpServers": { "ntdrive": { "command": "ntdrive-mcp" } } }
```

Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json`) does not search PATH, so give the
full path. `uv tool dir --bin` prints the directory, `%USERPROFILE%\.local\bin` by default:

```json
{ "mcpServers": { "ntdrive": { "command": "C:\\Users\\you\\.local\\bin\\ntdrive-mcp.exe" } } }
```

VS Code (`.vscode/mcp.json`), and Cursor takes the Claude Desktop shape in `.cursor/mcp.json`:

```json
{ "servers": { "ntdrive": { "type": "stdio", "command": "ntdrive-mcp" } } }
```

## Quick start (CLI)

The CLI has the same tools as subcommands. The first call auto-starts the daemon.

The daemon runs windowless (started with `pythonw.exe`, detached), and the vmrun and kd.exe
processes it starts are hidden too, so nothing pops up on the desktop. `ntdrive daemon status`
says whether it is up. `ntdrive daemon logs -f` follows what it is doing: one line per tool call
(name, caller, outcome, never the arguments) plus the daemon's own messages, from
`%LOCALAPPDATA%\ntdrive\logs\daemon.out.log`.

```powershell
ntdrive sys health                 # host binaries and config, then each VM live: power, SSH, debugger transport
ntdrive vm start win11
ntdrive term open win11            # prints a session id and a CoView URL, a browser page that mirrors the session
ntdrive kd attach win11            # serial: attaches at once. net: the target connects while the guest boots
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

The Python SDK has the same tools as methods, `vt.<group>.<verb>(...)`:

```python
from ntdrive.sdk import NtDrive

vt = NtDrive()                     # talks to the daemon, starts it if needed
vt.vm.start("win11")
print(vt.sys.state(vm="win11"))    # sys_state takes keyword arguments only
```

## What an agent can do

- **VM power and snapshots**: start, stop, suspend, three reboot modes, and live snapshots with a
  tree listing, revert, and delete.
- **Kernel debugging over KDNET or a serial pipe**: set up the guest, attach `kd.exe`, break in, run debugger
  commands, wait for a bugcheck or breakpoint, and detach.
- **Real-time terminal**: open SSH PTY sessions, stream output, render the screen, wait on a regex,
  and send keys including `{ctrl+c}`.
- **Console and files**: capture a console screenshot (for a BSOD or login screen) and copy files
  both ways with checksum verification.
- **Unified state**: one `sys_state` call returns VM power, debugger state and terminal sessions,
  and compound actions like snapshot revert run as a single orchestrated step.

The full list below is generated from the tool registry (`scripts/tools_table.py --write
README.md` refreshes it, a test keeps it current). Effect is what the MCP annotations say: a read tool
changes nothing, an additive one adds or starts something, a destructive one can discard state,
and the ones that need `confirm=true` say so in their arguments. Arguments are in `PRD.md` section
7 and in `ntdrive <group> <verb> --help`.

<!-- tools:start -->
| Tool | Effect | What it does |
|---|---|---|
| `vm_list` | read | List registered VMs with power, debugger and terminal state. |
| `vm_state` | read | Power, debugger and terminal state of one VM. |
| `vm_start` | additive | Power on (or resume) a VM without the GUI by default. discard_saved_state boots fresh when a stale saved state blocks the resume. |
| `vm_stop` | destructive | Stop the VM: mode soft, hard or kill. hard and kill need confirm=true. |
| `vm_reboot` | destructive | Reboot the guest (soft, hard or from the debugger) and bring kd and terminals back, the terminals under new session ids. hard needs confirm=true. |
| `vm_suspend` | additive | Suspend the VM to disk. |
| `vm_resume` | additive | Resume a suspended VM (same as vm_start). |
| `vm_config` | additive | Read or change the VM hardware in the vmx: cpus, memory_mb, nic. Without arguments it reports the current values. A change needs the VM powered off. |
| `snap_list` | read | Snapshot tree of a VM plus the current snapshot and stored metadata. |
| `snap_take` | additive | Take a snapshot (memory included while running), record description and kd state, and return the snapshot list. |
| `snap_revert` | destructive | Revert to a snapshot: detach kd, revert, start, reattach kd, reopen terminals. |
| `snap_delete` | destructive | Delete a snapshot (and optionally its children). Needs confirm=true. |
| `kd_setup_host` | additive | Prepare the host side of the kd transport: serial adds the named-pipe COM port to the vmx (VM must be off), net checks the host firewall for kd.exe and repairs it through one UAC prompt. |
| `kd_setup_guest` | additive | Enable kernel debugging in the guest with bcdedit over SSH (serial or KDNET per kd_transport) and save the KDNET port and key to vms.yaml. Settings that already point at this host are read back, not rewritten. |
| `kd_attach` | additive | Start kd.exe for the VM and wait until the target connects. |
| `kd_detach` | additive | Resume the target if needed and stop kd.exe. |
| `kd_break` | additive | Break into the running target and wait for the kd> prompt. |
| `kd_go` | additive | Resume the target (g). |
| `kd_exec` | destructive | Run one or more debugger commands at the kd> prompt and return each command's output. |
| `kd_wait_event` | read | Wait until the running target stops (bugcheck, breakpoint, ...) or the timeout expires. |
| `kd_state` | read | Debugger state: attached (kd.exe alive), state (detached, waiting, running, broken), transport, target info, last event and log path. |
| `kd_log_tail` | read | Last bytes of the kd.exe transcript. |
| `term_open` | additive | Open a real-time PTY session (SSH) on the guest and return its session_id. |
| `term_send` | destructive | Type text and/or keys into a session and return at once. Tokens: {enter} {tab} {esc} {ctrl+c} {up}. Use it for a long-running command or one that will stop in the debugger, then term_read or kd_wait_event. |
| `term_read` | read | Read new output (delta), wait for a regex (until), or render the screen (mode=screen). A wait ends with guest_frozen_by_debugger when the target stops at kd>. |
| `term_exec` | destructive | Run one command in the session, wait for it to end (up to timeout) and return only its output and exit code. For a command that will stop in the kernel debugger or drop SSH, use term_send, then kd_wait_event or term_read. |
| `term_resize` | additive | Resize the PTY. |
| `term_close` | additive | Close a session. |
| `term_list` | read | List terminal sessions (the usable ids in open) and the CoView page that mirrors them live in a browser (#<session_id> selects one). |
| `term_prune` | additive | Forget closed and disconnected terminal sessions (their open successors stay), so the list shows only what is usable. |
| `con_screenshot` | read | Save a PNG of the VM console and return its path (base64 on request). method=vnc reads the console framebuffer without any guest login (con_enable_vnc turns VNC on first). |
| `con_send_keys` | destructive | Type keys into the VM console over VNC, no guest login needed (con_enable_vnc turns VNC on). For a lock or login screen or before the network is up: keys=['{password}', '{enter}'] logs in without the password crossing the wire. |
| `con_enable_vnc` | additive | Turn on the console VNC server in the vmx so con_screenshot method=vnc can read the screen without a guest login. Run it with the VM off, then start the VM. |
| `file_push` | destructive | Copy a file, directory or glob from the host into the guest and verify it by SHA-256 (over SFTP, or through VMware Tools when SSH is down). |
| `file_pull` | additive | Copy a file from the guest to the host. |
| `file_stat` | read | Size, last-modified time and is_dir of a guest path, so freshness can be checked without a shell. exists=false when the path is not there. |
| `file_ls` | read | List a guest directory (each entry name, size, modified, is_dir), without a shell. |
| `file_delete` | destructive | Delete a guest file or directory (recurse for a non-empty directory). deleted=false when it was already absent. |
| `sys_state` | read | VM power, debugger state, terminal sessions and last events in one answer. |
| `sys_health` | read | Check binaries, config and backend capabilities, then probe every VM: power, guest SSH port and the debugger transport on the host. Run this first: ok is false when the host or any VM has an issue, and every issue names its fix. |
<!-- tools:end -->

## The state model to respect

- While the debugger is broken in (`kd_state == broken`) the whole guest is frozen. Terminal, file
  and screenshot tools return `guest_frozen_by_debugger` at once instead of hanging, and a running
  `term_exec` or `term_read` wait ends the same way. Run `kd go` first.
- `snap revert` detaches the debugger and `vm reboot` keeps kd.exe waiting for the reconnect. Both
  drop terminal sessions, then reattach and reopen them unless you pass `--no-reattach-kd` or
  `--no-reopen-term`. The reopened sessions have new ids, listed in the result as `term[].new`.
- After a reconnect the old terminal session id is kept and points to its successor.

The full state model and the procedures are in `SKILL.md`.

## Encrypted VMs

Set `encryption_password_env` (the name of an environment variable, preferred) or
`encryption_password` (inline, acceptable because `vms.yaml` is git-ignored) and every operation
that opens the vmx passes `-vp` to vmrun. Power, snapshot list, revert, delete, screenshot, guest IP
and file copy all work on an encrypted VM, including the partial encryption that a Windows 11 vTPM
requires. The one rough edge: vmrun refuses a live snapshot, or a memory snapshot delete, of a
running encrypted VM. `snap_take` and `snap_delete` then answer with
`error.reason=encrypted_live_snapshot`, and `--allow-suspend` handles it (suspend, snapshot or
delete, resume: terminal sessions are dropped and the debugger is reattached). A snapshot of a
powered-off VM never needs it. When the resume fails the call fails with the snapshot recorded
and the facts in the error, and `vm_start --discard-saved-state` boots fresh when the vmx still
names a saved state Workstation cannot restore. What vmrun accepts is recorded in `AGENTS.md`, Things that bit us.

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
- Destructive actions (`snap_delete`, `vm_stop mode=hard` or `kill`, `vm_reboot mode=hard`) require
  `confirm=true` and go
  through a policy gate you can tune in `policy.yaml`.

## Documentation map

| File | Read it if you are |
|---|---|
| `README.md` | a human setting up or using ntdrive |
| `SKILL.md` | an agent using the tools. State model, standard procedures, and moves to avoid |
| `AGENTS.md` | any coding agent changing this repo. Conventions, commands and where things live |
| `CLAUDE.md` | Claude Code. Points at `AGENTS.md` and adds the Claude-specific bits |
| `PRD.md` | anyone who wants the full requirements and design rationale |
| `vms.example.yaml` | someone editing `vms.yaml` by hand: every field, documented |
| `policy.example.yaml` | someone tuning the allow, confirm, deny level per tool |
| `scripts/` | host and guest setup, see Setup above |

`SKILL.md` is written for the agent. MCP clients that read skill files pick it up, and any agent can
be pointed at it. It covers the state model, the setup and debug-loop and BSOD-recovery procedures,
and the forbidden moves (such as touching the terminal while the debugger is broken in).

## Development

```powershell
uv sync
uv run --no-sync pre-commit run --all-files   # ruff format, ruff check, mypy, prettier, ASCII check
uv run --no-sync pytest -q                    # unit tests, all fakes, no real VM needed
```

Both must pass before a commit, and CI runs exactly these two on windows-latest. `--no-sync`
because a sync rewrites `ntdrive-mcp.exe`, which fails while an MCP client such as Claude Code
holds it. Conventions, the map of the code and what to update when a tool changes are in
`AGENTS.md`. Ruff is the only Python formatter and linter, mypy is strict for the whole package,
and Prettier runs on the CoView web assets only. A pre-commit hook rejects non-ASCII characters
and semicolons in Markdown prose, which keeps every document English and plain. Nothing in the
test suite needs a VM: the one test that touches the real Windows firewall runs only with
`NTDRIVE_LIVE_TESTS=1`, and the live check is `ntdrive verify` (Setup, step 3).

The MCP face can be poked by hand with the inspector: `npx @modelcontextprotocol/inspector
ntdrive-mcp` (from a clone, `npx @modelcontextprotocol/inspector uv run ntdrive-mcp`) opens a page
that lists the tools with their annotations and calls them.

Only the VMware backend is implemented. Hyper-V and VirtualBox sit behind the same
`HypervisorAdapter` interface and are planned for a later version.

## Layout

```
src/ntdrive/
  config.py       vms.yaml and policy.yaml models
  errors.py       NtDriveError with code, hint and backend reason tags
  hostproc.py     runs vmrun and the PowerShell helpers without a console window
  paths.py        host path helpers shared by the CLI, SDK and file tools
  core/           registry, state store, policy gate, audit log, orchestrator, service
  core/tools/     tool handlers: vm, snap, kd, term, console, file, sys
  hypervisor/     HypervisorAdapter, the VMware (vmrun) adapter, vmx file access
  kd/             KdSession around kd.exe, host firewall repair for KDNET
  term/           terminal transports, sessions, reconnect and successor ids, key tokens
  screen/         VNC framebuffer capture for con_screenshot method=vnc
  daemon/         ntdrived HTTP and WebSocket app, lifecycle, client, CoView page
  mcp/ sdk/       generated front doors
  cli/            generated tool commands plus setup, verify, term attach and the daemon group
scripts/          host and guest setup with their .cmd launchers, a guest probe, the ASCII hook,
                  the README table generator
tests/            unit tests with fake vmrun, fake kd.exe and fake terminal channels
```

## License

MIT. See `LICENSE`.
