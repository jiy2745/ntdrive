# PRD: ntdrive
## Agent-driven VM control, kernel debugging, and a real-time terminal

| Field | Value |
|---|---|
| Version | 0.10 (draft). Serial kd transport added as the alternative to KDNET (KD-11), `kd_setup_host` added, encrypted live snapshots through suspend (SNAP-1, SNAP-4). 0.9 translated to English, 0.8 added encryption (VM-6) |
| Date | 2026-09-11 |
| Status | Under review |
| Callers | Agents use **MCP**, humans and scripts and CI use the **CLI**, tests and automation use the **Python SDK**. All three are clients of one local daemon (`ntdrived`) |
| Hypervisor | **VMware Workstation Pro 17.x on a Windows host only.** Hyper-V and VirtualBox get the adapter interface but no implementation this version |
| Guest | Windows 10/11 x64 (primary). Linux (later) |

---

## 1. Summary

Let an LLM agent drive a Windows guest on a hypervisor the way a person sitting at the machine would.
It powers the VM and takes snapshots freely, attaches a kernel debugger over KDNET, and works inside a
real-time guest terminal. These three capabilities live behind one MCP server, and the server keeps the
state between them consistent (reattach the debugger after a snapshot revert, freeze the terminal while
the debugger is broken in, and so on). The hypervisor sits behind an adapter. This version implements
only VMware Workstation (`vmrun`) and leaves the Hyper-V and VirtualBox adapters as interfaces. Sessions
(SSH PTY, `kd.exe`) are held by one local daemon, `ntdrived`, and the **MCP server, CLI and Python SDK**
are all clients of that daemon. A human can sit down at the CLI in a terminal the agent opened over MCP.

---

## 2. Background and problem

### 2.1 The loop

Kernel driver development and vulnerability research repeat this loop endlessly.

```
build -> deploy to guest -> load/run -> (crash / breakpoint) -> analyze -> revert snapshot -> again
```

Today a person runs this loop by hand across the VMware GUI, WinDbg and an RDP/SSH window. To hand it to
an agent, all three windows must be exposed as tools. Existing public tools leave these gaps.

| Project | VM control | KDNET | Guest commands | Real-time terminal | Snapshot/debugger consistency | Hyper-V |
|---|---|---|---|---|---|---|
| memoryforensics1/windbg-mcp (C#, DbgEng COM + vmrun) | O | O | one-shot only | X | X | X |
| svnscha/mcp-windbg (Python, cdb/kd subprocess) | X | O | X | X | X | X |
| gengstah/windbg-mcp (Python, pybag) | X | O | X | X | X | X |
| **this project** | O | O | O | **O** | **O** | interface only (later) |

Three things set this apart.
1. **Real-time PTY terminal**. Read output as it arrives and send keystrokes as typed. Streaming commands like `ping -t`, interactive prompts, and Ctrl+C are all handled the way a person would.
2. **State consistency**. The server itself handles the effect that snapshot revert, reboot and debugger break have on the other two capabilities.
3. **Hypervisor adapter structure**. This version implements VMware only, but power, snapshots and console sit behind an adapter interface, so adding Hyper-V or VirtualBox later leaves the agent's procedure unchanged.

### 2.2 Design borrowed from google/artemis

[google/artemis](https://github.com/google/artemis) is Google's PTE team framework that automates Android
devices from natural language. The target differs, but it solves the same "let an agent drive a real
device" problem, so this project adopts the following patterns.

| artemis pattern | How this project applies it |
|---|---|
| MCP server + CLI + Python SDK | **Adopted.** Agents use MCP, humans and CI use the CLI, tests use the SDK. All three are generated from one tool registry and attach to the same daemon `ntdrived` (sections 5.8, 6.5) |
| Live screen mirroring in a web console | CoView: watch terminal sessions and the console screenshot stream live in a browser (P1) |
| History compression (replace old screenshots with summaries) | Tool results always carry a size cap (64 KB). The full text goes to a file and the result gives the path. Screenshots return a path by default, base64 on request |
| Pre-execution safety net | Destructive operations are refused without `confirm: true`. A policy file tunes the level per tool |
| Action bursts (several actions per turn) | `term_send` takes a list of keys, `kd_exec` takes a list of commands. Compound operations (`snap_revert`, `vm_reboot`) run as one call |
| Session-relative timestamps (`T+mm:ss`) | Every session log and the `elapsed` in tool results use a session-relative clock |
| A guidance file for the AI assistant | The repo ships `SKILL.md` (state model, standard procedures, forbidden moves) so an agent uses the tools correctly from the start |

---

## 3. Goals and non-goals

### 3.1 Goals
- **G1. Fully tool-ify VM lifecycle and snapshots.** Start/stop/reboot/suspend, snapshot take/list(tree)/revert/delete.
- **G2. Let the agent do KDNET kernel debugging end to end.** Guest KDNET setup, debugger attach, command execution, break/go, waiting for events (bugcheck, bp), detach.
- **G3. Drive the guest terminal in real time, both directions.** Multiple sessions, streaming reads, screen snapshots, special-key input, regex waits.
- **G4. The three capabilities share one state model.** Whichever tool is called, the current VM, debugger and terminal state read consistently.
- **G5. Put the hypervisor behind an adapter.** This version implements VMware only. The interface is shaped so Hyper-V and VirtualBox can be added later.
- **G6. A human can watch alongside.** A person can watch and step into the agent's terminal from a browser (P1).
- **G7. The three front doors share the same sessions.** Whether opened over MCP, CLI or SDK, the terminal and debugger sessions live in one daemon. A person can take over a session the agent opened, and sessions survive a Claude Code restart.

### 3.2 Non-goals (not this version)
- ESXi/vSphere/Fusion, non-Windows hosts.
- A serial console shell for Linux guests (interface reserved only).
- User-mode debugging (cdb/Frida/TTD). Use the existing projects alongside if needed.
- GUI automation (mouse clicks, image recognition). Console screenshot and key input are the limit.
- Remote hosts. The daemon and the three clients run locally on the host where the hypervisor is installed. `ntdrived` binds only to 127.0.0.1.
- Hyper-V and VirtualBox adapter implementations. Only the `HypervisorAdapter` interface is defined, and implementation is a later version. VirtualBox was confirmed feasible (`VBoxManage` plus KDNET support for the Intel PRO/1000 NIC) but is deferred (section 12).

---

## 4. Users and scenarios

**User**: a kernel driver developer or Windows security researcher (the author). **Caller**: an MCP client agent such as Claude Code.

| ID | Scenario | Features |
|---|---|---|
| S1 | Driver deploy/debug loop: push a built `.sys` to the guest, run `sc create/start` in the terminal, set a `bp` in kd, and on a hit analyze with `!analyze`, `dv`, `k`, then `g` | FILE, TERM, KD |
| S2 | Analyze a BSOD and recover: catch the bugcheck event, save `!analyze -v`, confirm the blue screen with a console screenshot, revert to a clean snapshot, and the debugger and terminal recover automatically | KD, CON, SNAP, STATE |
| S3 | New VM setup: enter the terminal, confirm OpenSSH, set up KDNET with `bcdedit`, reboot, confirm the debugger attaches, take a "base" snapshot | TERM, KD, VM, SNAP |
| S4 | Human/agent collaboration: a person watches the agent's terminal in a browser and steps in by typing directly | TERM (co-view) |
| S5 | Long watch: while the agent waits minutes for a bp hit with `kd_wait_event`, it runs a trigger program in the terminal | KD, TERM |
| S6 | (later) Host swap: run the same procedure on a Hyper-V host (Windows 11 Pro) by changing only `backend` in `vms.yaml` | HV |

---

## 5. Requirements

Priority: **P0** = MVP required, **P1** = required for 1.0, **P2** = later.

### 5.1 FR-VM: power and snapshots

| ID | Requirement | Priority |
|---|---|---|
| VM-1 | List registered VMs and each VM's power state (off/running/suspended). Registration is in the config file (`vms.yaml`), and the running list is cross-checked against the backend (`vmrun list` / `Get-VM`). | P0 |
| VM-2 | Start (headless by default), stop (soft = guest shutdown, hard = power off), suspend, resume. | P0 |
| VM-3 | Three reboot modes: `soft` (`shutdown /r /t 0` in the guest), `hard` (backend reset), `kd` (`.reboot` while broken in the debugger). Take an option for auto-reattaching KD and the terminal after reboot. | P0 |
| SNAP-1 | Take a snapshot. Includes memory when running (live snapshot). Record name, description and auto tags (time taken, kd state). The result states `via` (`direct` or `suspend-resume`) and `memory_included`. On a running **encrypted** VMware VM vmrun refuses a memory snapshot, so with `allow_suspend=true` the daemon suspends the VM (memory goes into the encrypted `.vmss`), snapshots, then resumes. That path behaves like `vm_suspend`: it refuses while the debugger is broken in, detaches the debugger and drops terminal sessions first, and reattaches the debugger afterwards. Hyper-V forces a Standard checkpoint (`Set-VM -CheckpointType Standard`). | P0 |
| SNAP-2 | Return the snapshot list as a **tree**. Report the current position (which snapshot the VM descends from). | P0 |
| SNAP-3 | Revert a snapshot. Because the post-revert power state differs by backend (vmrun does not run it, Hyper-V may be saved), the adapter **normalizes to running** (can be turned off). Detach KD before, and orchestrate KD re-attach and terminal reconnect after. | P0 |
| SNAP-4 | Delete a snapshot (optional children). **Refused without `confirm: true`.** Deleting a memory snapshot of a running encrypted VM has the same vmrun limitation as SNAP-1 and the same `allow_suspend` workaround. | P1 |
| VM-4 | Guest IP lookup (`vmrun getGuestIPAddress -wait` / `Get-VMNetworkAdapter`). Used internally by the terminal and file tools. | P0 |
| VM-5 | The VMware backend uses **`vmrun` as the single path**. The Workstation REST API (vmrest) does not support snapshots. | P0 |
| VM-6 | **Encrypted VM support.** A VMware Workstation encrypted VM needs a password to open the vmx. Store the per-VM password in `vms.yaml` as an environment variable name (`encryption_password_env`, preferred) or inline (`encryption_password`, acceptable because `vms.yaml` is git-ignored) and add `-vp <password>` to every vmrun command that opens the vmx. Like other secrets, the password never appears in tool arguments, results or logs. If the password is required but missing, surface the vmrun error as `backend_error`. The adapter classifies vmrun failures once and tags the error with a `reason` (`password_required`, `encrypted_live_snapshot`, `config_unreadable`, `snapshot_missing`), so no tool matches English error text. | P0 |

### 5.2 FR-HV: hypervisor adapter

| ID | Requirement | Priority |
|---|---|---|
| HV-1 | `HypervisorAdapter` interface: `list, power_state, start, stop, reset, suspend, resume, snapshot_take, snapshot_list, snapshot_revert, snapshot_delete, guest_ip, screenshot, send_keys?`. Every higher tool sees only this interface. | P0 |
| HV-2 | `VmwareAdapter`: wraps `vmrun -T ws`, output parsers, retries. | P0 |
| HV-3 | `HyperVAdapter`: call the PowerShell Hyper-V module (`Get-VM`, `Start/Stop/Restart/Suspend/Resume-VM`, `Checkpoint-VM`, `Get-VMCheckpoint`, `Restore-VMCheckpoint`, `Remove-VMCheckpoint`) through one **long-lived admin pwsh process** and read JSON back. Do not spawn a fresh pwsh per call (avoids a 2-3 s delay). Later version. | P2 |
| HV-4 | Hyper-V console: screenshot via WMI `Msvm_VirtualSystemManagementService.GetVirtualSystemThumbnailImage`, key input via `Msvm_Keyboard.TypeText/PressKey`. VMware uses `vmrun captureScreen` and its built-in VNC for key input. Hyper-V part is a later version. | P1 |
| HV-5 | Hyper-V file transfer fallback: a PowerShell Direct session plus `Copy-Item -ToSession/-FromSession`. VMware uses `vmrun copyFile*`. Hyper-V part is a later version. | P1 |
| HV-6 | The backend is chosen by the `backend` field in `vms.yaml`. This version accepts only `vmware`, and any other value is refused with `backend_unsupported`. Tool names, arguments and result schemas are the same across backends. Differences show up only in the `sys_health` capability list. | P0 |

### 5.3 FR-KD: KDNET kernel debugging

| ID | Requirement | Priority |
|---|---|---|
| KD-1 | Debugger attach: spawn `kd.exe -k net:port=<n>,key=<k>` (KDNET) or `kd.exe -k com:pipe,port=\\.\pipe\<name>,baud=115200,resets=0,reconnect` (serial pipe), chosen by `kd_transport`, as a subprocess and collect stdout in real time. Detect the transition from waiting for the target (`waiting`) to connected (`running`). Over serial kd.exe announces the connection only on the first sync, so attach checks that the pipe exists and that kd.exe stays alive, then reports `running`. Same regardless of backend. On `net` with no saved key, `kd_attach` first runs the `kd_setup_guest` step: it reads the guest's KDNET settings over SSH and saves the port and key it finds (`adopted`), or writes them and asks for a reboot. | P0 |
| KD-2 | Command execution: send an arbitrary kd command (or a list) and return **only that command's output**, framed with `<cmd>; .echo <sentinel>`. Support a timeout and a max output size (truncation flagged). | P0 |
| KD-3 | break / go. Break-in sends `CTRL_BREAK_EVENT` to the piped kd.exe (separate process group plus a hidden console). Fall back to DbgEng COM (`SetInterrupt`) if that fails. | P0 |
| KD-4 | Wait for events: from `running`, wait until a prompt returns from a bugcheck, breakpoint, module load, and so on (with a timeout). Include the event kind and the preceding output in the result. | P0 |
| KD-5 | State query: `detached / waiting / running / broken`, current port and key, connected target info, last event. | P0 |
| KD-6 | Keep the full session log in a file (`-loga`), and let a tool read the last N KB. | P0 |
| KD-7 | Automate guest debug setup (`kd_setup_guest`): over the terminal, first read `bcdedit /dbgsettings`, and when the guest already debugs to this host's IP with a key (`scripts/setup-guest.ps1` configures KDNET by default, inferring the host IP from the NAT gateway) save that port and key without rewriting anything (`adopted`). Otherwise apply `bcdedit /debug on` and, for `kd_transport: net`, `bcdedit /dbgsettings net hostip:<host virtual adapter IP> port:<n> key:<k>` (the server generates the key and saves it in `vms.yaml`), or, for `kd_transport: serial`, `bcdedit /dbgsettings serial debugport:1 baudrate:115200` (no key), then reboot. hostip is the VMnet8 host adapter for VMware, or the external/internal virtual switch vEthernet adapter IP for Hyper-V. | P0 |
| KD-8 | Symbol path: pass `_NT_SYMBOL_PATH` or the configured value with `-y`. Provide a default local cache directory. | P0 |
| KD-9 | On detach, resume the target (`g`) and then stop the process. Force-kill option. | P0 |
| KD-10 | Reconnect the debugger after snapshot revert or reboot, for both transports. The target looks for the debugger again early in boot, so restarting the host-side kd.exe reconnects. A live session is kept through a reboot and waited on. A serial session that does not announce the reconnection within the timeout is respawned so the reported state is a known one. Retry policy (count, interval) on failure. | P0 |
| KD-11 | **Serial transport (guest COM1 -> host named pipe), the alternative to KDNET for hosts where nobody can approve a UAC prompt.** `kd_setup_host` writes `serial0.*` (pipe server, `\\.\pipe\ntdrive-<vm>`) into the vmx while the VM is off and is idempotent. kd.exe connects as the pipe client, so there is no network, no host firewall rule and no administrator step, unlike KDNET. `sys_health` reports a missing pipe entry. Hyper-V would use `Set-VMComPort` (later). | P0 |
| KD-12 | Convenience wrappers for common commands (`kd_bp`, `kd_modules`). Internally call KD-2. | P2 |

### 5.4 FR-TERM: real-time terminal

| ID | Requirement | Priority |
|---|---|---|
| TERM-1 | The transport is behind a `TermTransport` interface. The first implementation is **SSH + PTY** (guest OpenSSH Server, default shell PowerShell). Keep one SSH connection per VM and open several PTY channels (sessions) on it. Common to both backends. | P0 |
| TERM-2 | `term_open(vm, shell, cols, rows)` creates a session and returns a `session_id`. Shell is one of `powershell`/`cmd`/`pwsh`. | P0 |
| TERM-3 | **Real-time read**: each session has a ring buffer (1 MB default) and a per-agent read cursor. `term_read(mode=delta)` returns output since the last cursor immediately. Output must be readable within 100 ms of being produced. | P0 |
| TERM-4 | **Screen read**: `term_read(mode=screen)` returns the current screen (rows x cols text) rendered by a virtual terminal emulator (pyte). Same as what a person sees. | P0 |
| TERM-5 | **Conditional wait**: `term_read(until=<regex>, timeout)` long-polls until the regex appears in output or the timeout. Max timeout 600 s. | P0 |
| TERM-6 | **Write**: `term_send(session, text | keys[], enter=true)`. Special keys are written as tokens like `{ctrl+c}`, `{enter}`, `{tab}`, `{up}`, `{esc}` and the server converts them to VT sequences. Bursts of keys sent at once are supported. | P0 |
| TERM-7 | Convenience run: `term_exec(session, cmd, timeout)` sends the command plus a unique marker echo and returns only the output up to the marker. | P0 |
| TERM-8 | Resize, close, session list (each session's state, last activity time, shell). | P0 |
| TERM-9 | On reboot/revert/network drop the session is marked `disconnected` and reads report it at once. The server polls the SSH port and, once reachable, creates a **new session**, and reading with the old id points to the successor id. | P0 |
| TERM-10 | While the debugger is `broken`, the guest is frozen, so `term_*` calls do not wait and return `guest_frozen_by_debugger` at once. | P0 |
| TERM-11 | Record all session input and output with a `T+mm:ss.mmm` relative timestamp (asciicast v2 compatible). Tag human input separately from agent input. | P1 |
| TERM-12 | **Human co-view**: from a local web page (xterm.js + WebSocket) a person can watch a session live and type into it. Agent input and human input are multiplexed onto the same PTY. | P1 |
| TERM-13 | Output size cap: a single `term_read` is at most 64 KB. Beyond that it returns `truncated: true` and the next cursor. | P0 |
| TERM-14 | **PowerShell Direct transport** (Hyper-V only): run guest commands without a network via `New-PSSession -VMName`. It is not a PTY, so `screen` mode is unsupported and only `term_exec` and `delta` work. Used for early setup (installing OpenSSH, KDNET bcdedit) before SSH works. Later version. | P2 |
| TERM-15 | `hvc ssh` (SSH over a Hyper-V socket) transport: a PTY without a network for Linux guests. Excluded for Windows guests because Win32-OpenSSH does not yet accept Hyper-V sockets. | P2 |
| TERM-16 | Serial console transport for Linux guests. Same interface. | P2 |

### 5.5 FR-CON: console screen (secondary)

| ID | Requirement | Priority |
|---|---|---|
| CON-1 | Save a console screenshot as a PNG file and return the path. base64 optional. For login screen, BSOD, boot hang. | P0 |
| CON-2 | Console key input (`con_send_keys`). Hyper-V uses WMI `Msvm_Keyboard`, VMware uses its built-in VNC (`RemoteDisplay.vnc.enabled`). For when SSH is unavailable (before login, dead network). | P1 |

### 5.6 FR-FILE: file transfer

| ID | Requirement | Priority |
|---|---|---|
| FILE-1 | Copy files host->guest and guest->host. Primary is SFTP (reusing the SSH connection), secondary is the backend fallback (`vmrun copyFile*` / PowerShell Direct `Copy-Item`). | P0 |
| FILE-2 | Driver deploy convenience: copy `.sys` and `.pdb` to a guest path and refresh the symbol path. | P1 |
| FILE-3 | Recursive directory copy and globs (`build/*.sys`). Create the destination directory if missing. For large files, compare SHA-256 on both sides after transfer and record it as `verified` in the result. If the debugger is `broken`, fail at once with `guest_frozen_by_debugger` like TERM-10. | P0 |

### 5.7 FR-STATE: unified state and orchestration

| ID | Requirement | Priority |
|---|---|---|
| ST-1 | `sys_state` returns VM power, current snapshot position, KD state, terminal session list and last event in one call. | P0 |
| ST-2 | The server guarantees the order of compound operations. Example: `snap_revert` = KD detach -> revert -> start -> KD attach (optional) -> wait for SSH -> resume terminal (optional). The result records each step. | P0 |
| ST-3 | Log every tool call to a JSONL audit log (secrets masked in arguments, `T+` relative time included). | P0 |
| ST-4 | Destructive operations (`snap_delete`, `vm_stop hard`, `.reboot`, revert without a snapshot) are refused without `confirm: true`. A policy file (`policy.yaml`) tunes the per-tool level (allow/confirm/deny). | P0 |
| ST-5 | The front doors are **MCP, CLI and Python SDK**, defined in section 5.8 (FR-FACE). The core is a pure Python library (`ntdrive.core`) and the three front doors are thin layers on top. The core must be callable directly from pytest and a REPL, without the daemon or MCP. | P0 |
| ST-6 | Ship `SKILL.md`: document the state model, standard procedures (setup, debug loop, BSOD recovery) and forbidden moves (calling the terminal while broken, and so on) for agents. | P0 |
| ST-7 | Target **20 tools or fewer**. Because tool schemas load into context at session start, group similar operations under an `action` argument (open issue 8). | P1 |
| ST-8 | The max wait of a long-poll tool (`kd_wait_event`, `term_read(until)`) must be shorter than the MCP client's tool-call timeout. The server holds the cap as a setting and clips a longer request to the cap, returning a `timeout` event. | P0 |
| ST-9 | **Language rule**: every document and text string in the repository (README, CLAUDE.md, SKILL.md, this PRD, docstrings, tool description and hint, error messages, logs, commit messages) is written in **English**. **Code comments too.** No file is exempt. This is enforced by the DEV-5 hook. | P0 |
| ST-10 | **Style rule**: documents and code text are written plainly, without an AI look. No emoji, no em dash, no decorative symbols (arrow, check, star glyphs), no box-drawing-character diagrams, and no overuse of bold and headers per section. **No semicolon as sentence punctuation** either. End sentences with a period. Semicolons inside code and commands (`cmd; .echo`, PowerShell) are the exception. Draw diagrams with plain characters like `+ - | >` and write arrows as `->`. The DEV-5 hook mechanically catches non-ASCII and semicolons in Markdown prose. | P0 |

### 5.8 FR-FACE: front doors (MCP, CLI, SDK)

| ID | Requirement | Priority |
|---|---|---|
| FACE-1 | **Daemon `ntdrived`**: the only process that holds sessions (SSH connections, PTY, `kd.exe`, ring buffers) and the StateStore. Exposes the tool API over local HTTP + WebSocket (127.0.0.1, default 8765). One instance per user (lock file). | P0 |
| FACE-2 | **Single source tool registry**: tool name, argument schema (pydantic) and handler are defined once in the `ntdrive.core.tools` registry. MCP tools, HTTP endpoints, CLI subcommands and SDK methods are all **generated** from this registry. The same definition is never written three times by hand. | P0 |
| FACE-3 | **MCP server `ntdrive-mcp`**: stdio, FastMCP. Stateless, and forwards tool calls to the daemon over HTTP. Auto-starts the daemon if absent. Registered in Claude Code with one line in `.mcp.json`, whose command is the installed `ntdrive-mcp` (`uv tool install`, editable for contributors) so the entry carries no path and no `uv run`. At initialize the server sends `instructions`, a short digest of SKILL.md (call sys_health first, a broken-in debugger freezes the guest, revert and reboot reattach for you, destructive tools need confirm, no secrets in arguments), so an agent without the skill file still gets the rules. | P0 |
| FACE-4 | **CLI `ntdrive`**: `ntdrive <group> <verb> [args]` maps one-to-one to tools (`ntdrive kd exec win11-dev "!process 0 0"`). Default output is a human table, `--json` gives the raw tool result. Exit codes map to `error.code`. To avoid shell quoting, command bodies can come from `--stdin`/`--file`. | P0 |
| FACE-5 | **CLI `term attach <session>`**: connect the local console raw to a daemon PTY session (WebSocket). A person sits down in a session the agent opened. Detach key is `Ctrl+]`. Input is logged with a human tag. | P1 |
| FACE-6 | **Python SDK `ntdrive`**: `NtDrive()` is a daemon client (default). `NtDrive(inprocess=True)` runs the core in the same process without the daemon (for tests and a REPL). Method names and arguments match the tools (`vt.kd.exec("win11-dev", "!process 0 0")`). | P0 |
| FACE-7 | **Daemon lifecycle**: `ntdrive daemon start\|stop\|status\|restart`. Clients read `%LOCALAPPDATA%\ntdrive\daemon.json` (port, pid, token, version) and attach, and if the file is missing or the pid is dead they auto-start a detached process and wait for `/health`. The daemon stays up until an explicit stop (no idle exit). | P0 |
| FACE-8 | **Auth and version**: every request carries the random token from `daemon.json` as a header. The file has per-user ACL. `/health` returns the version and a client with a different major version is refused. | P0 |
| FACE-9 | **Front-door parity**: calling the same tool over MCP, CLI (`--json`) and SDK yields the same JSON (AT-9). Error codes and hints match too. | P0 |
| FACE-10 | **`ntdrive setup`**: a CLI command that writes the `vms.yaml` entry for one VM so nobody edits YAML by hand. It lists the VMs in the VMware Workstation inventory (or takes `--vmx`), reads the vmx (display name, encryption, NIC, Secure Boot), asks for the guest account and the passwords with hidden input, stores the passwords as User environment variables (`--inline-secrets` keeps them in `vms.yaml` instead), writes the entry, restarts the daemon and prints the `sys_health` issues that remain. Running it again adds another VM or updates one. Passwords are never accepted on the command line. `scripts/setup-host.ps1` wraps it into the one-run host setup: tools check, `uv sync`, `ntdrive setup` per VM, daemon restart, `kd_setup_host` per VM, `sys_health`, with no elevated shell. | P1 |
| FACE-11 | **`ntdrive verify`**: a CLI command that proves a VM end to end and says so: sys_health issues, the VM running (started if off), an SSH login as the configured account, the debugger transport on the host (firewall or serial pipe), then a real attach, break in, resume and detach. When the attach reports that KDNET was just configured in the guest, verify soft-reboots the guest and attaches again. It ends with ALL SET or the first failing check and its fix, exit code 0 or 1, `--json` for the report. `scripts/setup-host.ps1` runs it last, `-Verify` runs only it, so the order in which the host and guest scripts ran does not matter. | P1 |

### 5.9 Non-functional requirements

| Item | Bar |
|---|---|
| Latency | Terminal output to agent visibility within 100 ms. A simple kd command (`r`, `k`) round trip within 1 s. A Hyper-V PowerShell call round trip within 500 ms (reusing the long-lived pwsh process). |
| Stability | Zero server crashes over a 48-hour continuous session. Detect a dead child process (kd.exe, pwsh), update state and restart. |
| Concurrency | Up to 8 terminal sessions and 1 KD session per VM. Running several VMs at once (separate ports) is P1. |
| Security | The daemon binds only to 127.0.0.1 and checks the token on every request. MCP is stdio. Secrets (guest password, KDNET key, encryption password, daemon token) live only in `vms.yaml`, environment variables and `daemon.json`, and never appear in tool arguments, logs or results. The CoView URL in `term_open` results carries a separate view token that only opens the session list and the terminal streams. |
| Portability | Python 3.12 + uv. Host Windows 11. External binary paths come from config (`vmrun.exe`, `kd.exe`, `kdnet.exe`, `pwsh.exe`). |
| Observability | Structured logs, per-session raw log files, `sys_health` (external binary presence and version, hypervisor service, backend capabilities, per-VM config issues, and a live probe of every VM: power, guest IP, whether the SSH port answers, and whether the serial pipe has a server on the host or the KDNET UDP port is free). |

### 5.10 Technical constraints (verified facts)

**Common**
- **While the debugger is broken in, the whole guest is frozen.** SSH, guest operations and screenshot updates all stop.
- **`bcdedit /debug on` is constrained on a VM with Secure Boot on.** Both hypervisors make turning VM Secure Boot off the standard procedure (Windows 11 still boots with Secure Boot off after install).
- **KDNET is independent of the guest firewall**, but the **host firewall must allow kd.exe/WinDbg UDP inbound**. Ports 50000-50039 recommended, unique per target. Windows adds inbound Block rules for kd.exe when its first listen raises the firewall prompt and nobody clicks Allow, and a Block rule wins over any Allow rule. `kd_setup_host` reads the rules without privilege and repairs them through one UAC prompt.
- Store-distributed WinDbg has included `kdnet.exe` and `VerifiedNicList.xml` since 2026-03 (the `kdnet` alias is added to PATH).

**VMware Workstation**
- **KDNET supported NIC**: the Intel e1000e (`8086:10D3`) and e1000 (`8086:100F`) that VMware emulates are supported on all Windows 10/11 builds. **vmxnet3 (`15AD:07B0`) is supported only on Windows 11 23H2 and later.** Default is e1000e.
- **The Workstation REST API (vmrest) has no snapshot API.** Only power, clone, NIC and shared folders.
- **`vmrun revertToSnapshot` does not run the VM after reverting.**
- If Hyper-V/VBS is on the host, Workstation runs on top of the Windows Hypervisor Platform and is slower, but KDNET and SSH work fine. Both hypervisors can coexist on one host.

**Hyper-V**
- **The host this was written on is Windows 11 Home. Hyper-V is officially supported only on Pro/Enterprise/Education.** So the Hyper-V adapter has its interface defined from M0, but real verification happens on a Pro host (open issue 6).
- **Gen2 VM KDNET is officially supported** (kdnet.exe recognizes "Microsoft Hypervisor Virtual Machine"). Connect the host and VM with an external (or internal) virtual switch, and hostip is that switch's vEthernet adapter IP. **Do not use Default Switch, whose IP changes every reboot.** The synthetic NIC (`1414:00B9`) is on the Windows 11 KDNET list.
- **Enhanced Session causes timeouts while broken in**, so turn it off on a debugging VM.
- **Checkpoint type**: a Production checkpoint does not include memory (VSS based). A debugging VM must be pinned to `Set-VM -CheckpointType Standard` for a live snapshot.
- **PowerShell Direct** works without a network on a guest running Windows 10+/Server 2016+, but it is **not a PTY** (no console apps, no screen mode). Good for setup steps and one-shot commands.
- **`hvc ssh`** is SSH over a Hyper-V socket and gives a PTY without a network, but it is for Linux guests. The Windows guest OpenSSH Server does not yet accept Hyper-V sockets.

### 5.11 FR-DEV: development conventions and tooling

| ID | Requirement | Priority |
|---|---|---|
| DEV-1 | **Ruff is the only Python formatter and linter.** Use `ruff format` and `ruff check --fix` (lint, import sorting). Config lives in `pyproject.toml`: line-length 100, target py312, rules `E, F, I, UP, B, SIM, D` (Google-style docstrings). Do not add Black, isort or flake8. | P0 |
| DEV-2 | **mypy for type checking.** `ntdrive.core` is `strict`, the rest is default. Parsers of external binary output declare their return types. | P0 |
| DEV-3 | **Prettier for web assets (CoView HTML/CSS/JS).** `.prettierrc` is printWidth 100, singleQuote, semicolons on. Not applied to Python. | P1 |
| DEV-4 | **Everything automated by a pre-commit hook.** `.pre-commit-config.yaml` runs ruff-format, ruff, mypy, prettier (web assets only), the non-ASCII check (DEV-5), trailing-whitespace, end-of-file-fixer, check-yaml, check-toml. M0 puts `pre-commit install` in the setup script. CI (GitHub Actions, windows-latest) runs `pre-commit run --all-files` and `pytest`. | P0 |
| DEV-5 | **Code comments, docstrings, identifiers, strings and log messages are English only.** The hook `scripts/check_ascii.py` refuses a commit when it finds a non-ASCII character in a `.py .toml .yaml .json .js .css .html .md` file. In Markdown files it also catches semicolons outside code fences and inline code. The `[tool.check_ascii] exclude` list in `pyproject.toml` is empty, so every file including this PRD is checked. This hook mechanically enforces ST-9 (English) and ST-10 (no emoji, decorative symbols or semicolons). | P0 |
| DEV-6 | **Docstrings required on public functions and classes** (ruff `D` rules). One-line summary plus args, returns and raises. Comments say "why", not "what". | P0 |
| DEV-7 | **Editor and repo conventions.** `.editorconfig` (UTF-8, LF, Python 4 spaces, YAML/JSON 2 spaces), LF pinned by `.gitattributes`. Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`), in English. | P0 |
| DEV-8 | **Tests.** pytest + pytest-asyncio. Unit tests replace `vmrun`, `kd.exe` and SSH with fakes. Integration tests that need a real VM are marked `@pytest.mark.vm` and skip when no test VM is in `vms.yaml`. `uv run pytest` is the one entry point. | P0 |
| DEV-9 | **CLAUDE.md (English) summarizes these conventions** so an agent follows the same rules when writing code. The source of truth is `CLAUDE.md` and `pyproject.toml`, not this PRD. | P0 |

---

## 6. Architecture

### 6.1 Diagram

```
 Claude Code (MCP client)     Human / CI (shell)        pytest / automation
         | stdio                     | argv                     | import
         v                           v                          v
    ntdrive-mcp                  ntdrive CLI                Python SDK
         |                           |                          |
         +---------------------------+--------------------------+
                                     | HTTP + WebSocket, 127.0.0.1:8765, token
                                     v
 +--------------------- ntdrived (daemon, holds every session) ----------------------+
 |  ToolRegistry -> StateStore -> Orchestrator -> AuditLog                            |
 |                                                                                    |
 |  HypervisorAdapter         KdSession            TermManager            CoView     |
 |   - VmwareAdapter (vmrun)   (kd.exe subprocess)  (SshPtyTransport,      (xterm.js, |
 |   - (later: HyperV, VBox)                        PsDirectTransport)     /coview)  |
 +-------+-------------------------+-----------------------+--------------------+-----+
         | vmrun.exe / pwsh.exe    | UDP 50000 (KDNET)     | TCP 22 (SSH)       | ws
         v                         v                       v   or PS Direct     v
 +------------------------- VM (Windows guest) ---------------------------+   Browser
 |  KDNET (boot-time, e1000e or Hyper-V synthetic NIC)                    |   (human)
 |  OpenSSH Server -> PowerShell PTY                                      |
 |  VMware Tools / Hyper-V integration services (IP, screenshot, files)   |
 +------------------------------------------------------------------------+
```

### 6.2 Components

| Component | Responsibility | Implementation notes |
|---|---|---|
| ToolRegistry | Single definition of tool name, argument schema and handler. Argument validation, state precondition checks (for example refuse `term_*` when `broken`), policy gate | pydantic models. MCP, HTTP, CLI and SDK are generated from it |
| ntdrived (daemon API) | Local HTTP + WebSocket server. `POST /api/tools/<name>`, `/ws/term/<sid>`, `/health`, CoView static files | aiohttp, 127.0.0.1:8765, token auth, one instance per user |
| ntdrive-mcp | stdio MCP server. Generates tools from the registry and forwards calls to the daemon. Stateless | FastMCP (official Python SDK). Auto-starts the daemon |
| ntdrive CLI | Generates subcommands from the registry. Table or `--json` output, `term attach` raw passthrough | click + httpx + websockets |
| Python SDK | `NtDrive()` daemon client, or `inprocess=True` runs the core directly | httpx. Methods generated from the registry |
| StateStore | The single truth of VM, KD and TERM state. Each adapter reports state changes over an event bus | In-memory plus periodic backend reconciliation |
| HypervisorAdapter | Backend abstraction for power, snapshots, IP and console | `VmwareAdapter` (vmrun, `-T ws`), `HyperVAdapter` (long-lived pwsh + JSON) |
| KdSession | kd.exe lifecycle, output reader thread, prompt/sentinel detection, break-in | Prompt regex `^\d*: ?kd> ?$`, log `-loga` |
| TermManager / TermTransport | Per-session PTY, ring buffer, pyte screen, reconnect polling | `SshPtyTransport` (paramiko), `PsDirectTransport`, reserved: `HvcSshTransport`, `SerialTransport` |
| CoView | Session mirroring web UI plus console screenshot stream. Served inside the daemon | xterm.js, `/coview` path on the same port |
| Orchestrator | Compound-operation sequences like `snap_revert`, `vm_reboot` | ST-2 |
| AuditLog | JSONL of tool calls and results | Secret masking, `T+` relative time |

### 6.3 State model

```
VM:    off --start--> running --suspend--> suspended
        ^               |  ^                  |
        +-----stop------+  +-------start------+

KD:    detached --attach--> waiting --(target connects)--> running
                                                  ^            |
                                               go |            | break / bugcheck / bp
                                                  +-- broken <-+
                                                      (guest frozen, TERM unavailable)

TERM:  none --open--> open --(ssh drop / revert / reboot)--> disconnected --reopen--> open'
```

Transition rules (partial):
- While `KD.broken`, `term_*`, `file_*` and `con_screenshot` updates fail at once with `guest_frozen_by_debugger`.
- `snap_revert` and `vm_reboot` set `KD` to `detached` and all `TERM` to `disconnected`, then try to recover per the options.
- `vm_stop` when `KD` is `broken` first sends `g` or allows only a hard stop.

### 6.4 Real-time terminal data flow

1. The transport reader thread, the moment it receives bytes, does (a) append to the ring buffer, (b) feed the pyte screen, (c) broadcast to CoView WebSocket subscribers, (d) write to the session log file.
2. `term_read(delta)` slices from the agent's cursor and returns at once. With `until` it waits on a condition event (asyncio.Event).
3. `term_send` writes agent and human input to the same channel and logs it tagged by source.
4. On reboot/revert the channel hits EOF -> session `disconnected` -> the reconnect poller checks port 22 every 2 s -> on success creates a successor session.

### 6.5 Process model (daemon plus three clients)

- **One `ntdrived` holds the sessions.** SSH connections, PTY channels, the `kd.exe` pipe, ring buffers and the StateStore all live in this process. The MCP server, CLI and SDK are stateless clients.
- **Why a daemon.** (1) Sessions must outlive a process lifetime. Terminal and debugger sessions survive a Claude Code restart. (2) A person can sit down with CLI `term attach` in a session the agent opened over MCP. Without a daemon the MCP server process and the CLI process would hold different sessions. (3) The CoView web server needs a long-lived process anyway.
- **Why not MCP only.** For a person poking around by hand the CLI is far faster, and CI and scripts cannot use MCP. For an agent, MCP is better: no passing kd and PowerShell commands through three layers of shell quoting, and `error.code` plus `hint` structured errors and long-poll tools come naturally. So keep all three, but **generate the definitions from one place (ToolRegistry)** to make the upkeep one-fold.
- **Cost.** One process and an IPC layer (local HTTP+WS, token, lifecycle management) are added in M0. In exchange, there is no later work to split the daemon out.
- **Lifecycle.** The first client auto-starts it, and it stays up until an explicit `ntdrive daemon stop`. The client resolves `vms.yaml` (explicit path, `NTDRIVE_CONFIG`, then `%LOCALAPPDATA%\ntdrive`, never the working directory, because the config belongs to the user and not to a checkout) and hands it to the daemon it starts, and a daemon that runs without any config is restarted by the next client that can find one, which is safe because such a daemon holds no sessions. Shutdown order: if KD is `broken`, `g` -> stop `kd.exe` -> close SSH channels -> flush session logs. Even after an abnormal exit the KDNET target waits for the debugger, so the next `kd_attach` recovers.
- **Claude Code setup.** Register `ntdrive-mcp` in `.mcp.json`, allow with the single rule `mcp__ntdrive__*`. Because of long-poll tools, set the MCP tool-call timeout longer than the server cap (600 s default) (ST-8). Allowing `Bash(ntdrive *)` lets an agent use the CLI too, but `SKILL.md` recommends MCP by default.

---

## 7. MCP tool specification

Common: every tool takes a `vm` argument (the registered name). Results are JSON. Errors carry `error.code`
(`vm_not_running`, `guest_frozen_by_debugger`, `kd_not_attached`, `session_disconnected`, `confirm_required`,
`backend_unsupported`, `timeout`) and `error.hint` (what the agent should do next). Tool names and schemas do
not change with the backend (VMware/Hyper-V). The same tool is also exposed over HTTP (`POST /api/tools/<name>`),
CLI (`ntdrive <group> <verb>`) and SDK (`vt.<group>.<verb>()`) (section 7.6). Tool descriptions, hints and error
messages are written in English (ST-9).

### 7.1 VM / snapshots

| Tool | Arguments | Returns |
|---|---|---|
| `vm_list` | - | `[{name, backend, power, ip?, kd, term_sessions}]` |
| `vm_state` | `vm` | detail of one item above |
| `vm_start` | `vm, gui=false` | `{power}` |
| `vm_stop` | `vm, mode=soft\|hard, confirm?` | `{power}` |
| `vm_reboot` | `vm, mode=soft\|hard\|kd, reattach_kd=true, reopen_term=true, timeout=180` | `{steps:[...], kd, term}` |
| `vm_suspend` / `vm_resume` | `vm` | `{power}` |
| `snap_list` | `vm` | `{tree:[{name, children:[...]}], current}` |
| `snap_take` | `vm, name, description?, allow_suspend=false` | `{name, taken_at, kd_state_at_snapshot, via, memory_included, terms_dropped?, kd?}` |
| `snap_revert` | `vm, name, start=true, reattach_kd=true, reopen_term=true, timeout=180` | `{steps:[...], kd, term}` |
| `snap_delete` | `vm, name, children=false, confirm, allow_suspend=false` | `{deleted:[...], current, via, terms_dropped?, kd?}` |

### 7.2 Kernel debugger

| Tool | Arguments | Returns |
|---|---|---|
| `kd_setup_host` | `vm`, `fix_firewall=true`, `timeout=120` (serial: VM must be off, edits the vmx. net: reads the host firewall rules for kd.exe and, when they block KDNET, removes the Block rules and adds an Allow rule through one UAC prompt. `fix_firewall=false` only reports) | `{transport, changed, serial_pipe?, firewall?, next}` |
| `kd_setup_guest` | `vm, port?, key?` (needs SSH to the guest) | `{transport, port?, key_saved, adopted, needs_reboot, steps}`. `adopted` means the guest already debugged to this host's IP (scripts/setup-guest.ps1 does that by default), so the port and key were read back over SSH instead of written, and `needs_reboot` is false when debugging was already on |
| `kd_attach` | `vm, port?, key?, symbol_path?, wait_for_target=true, timeout=120` | `{state, transport, target_info?}`. On net with no saved key it runs the `kd_setup_guest` step first (reads the guest's settings over SSH, `adopted`) and fails with a reboot hint when settings had to be written |
| `kd_detach` | `vm, force=false` | `{state}` |
| `kd_break` | `vm, timeout=10` | `{state, output}` |
| `kd_go` | `vm` | `{state}` |
| `kd_exec` | `vm, cmd | cmds[], timeout=60, max_bytes=65536` | `{outputs:[{cmd, output, truncated, elapsed_ms}]}` |
| `kd_wait_event` | `vm, timeout=300` | `{event: bugcheck\|breakpoint\|module_load\|user_break\|timeout, output, state}` |
| `kd_state` | `vm` | `{state, transport, port?, serial_pipe?, target_info, last_event, log_path}` |
| `kd_log_tail` | `vm, bytes=16384` | `{text}` |

### 7.3 Terminal

| Tool | Arguments | Returns |
|---|---|---|
| `term_open` | `vm, shell=powershell\|cmd\|pwsh, transport=auto\|ssh\|psdirect, cols=120, rows=40` | `{session_id, transport, coview_url}` |
| `term_send` | `session_id, text | keys[], enter=true` | `{bytes_sent}` |
| `term_read` | `session_id, mode=delta\|screen, until?, timeout=0, max_bytes=65536` | `{text, cursor, truncated, matched?, state}` |
| `term_exec` | `session_id, cmd, timeout=60` | `{output, exit_code?, elapsed_ms}` |
| `term_resize` | `session_id, cols, rows` | `{}` |
| `term_close` | `session_id` | `{}` |
| `term_list` | `vm?` | `[{session_id, vm, shell, transport, state, last_activity, successor?}]` |

### 7.4 Console / file / system

| Tool | Arguments | Returns |
|---|---|---|
| `con_screenshot` | `vm, base64=false` | `{png_path, png_base64?}` |
| `con_send_keys` | `vm, keys[]` | `{sent}` (P1) |
| `file_push` | `vm, local, remote, verify=true` (`local` is an absolute host path, the CLI and SDK absolutize) | `{files, bytes, verified, via: sftp\|guest_tools, copied:[...], note?}` |
| `file_pull` | `vm, remote, local` (a trailing separator on `local` means directory) | `{bytes, via, note?}` |
| `sys_state` | `vm?` | unified VM, KD, TERM state |
| `sys_health` | - | binary paths and versions, backend capabilities, hypervisor service, a `kdnet_firewall` read when any VM uses net, and per VM: config `issues` (missing vmx, Secure Boot on, wrong NIC for KDNET, encrypted VM without a password, empty password environment variables, missing serial pipe or KDNET key, each naming the fix), `power`, `kd_state`, `guest {ip, ssh_port, ssh_open, skipped}`, and `serial_pipe {path, open}` or `kdnet_port {port, free, held_by_ntdrive, firewall_ok}`. The guest probe is bounded to a few seconds and skipped while the VM is off or frozen by the debugger |

### 7.5 Config file (`vms.yaml`)

```yaml
host:
  vmrun: "C:/Program Files (x86)/VMware/VMware Workstation/vmrun.exe"
  kd:    "C:/Program Files (x86)/Windows Kits/10/Debuggers/x64/kd.exe"
  kdnet: "C:/Program Files (x86)/Windows Kits/10/Debuggers/x64/kdnet.exe"
  symbol_path: "srv*C:/symbols*https://msdl.microsoft.com/download/symbols"
  daemon_bind: "127.0.0.1:8765"    # ntdrived: HTTP API + WebSocket + CoView

vms:
  win11-dev:                       # VMware
    backend: vmware
    vmx: "D:/VMs/win11-dev/win11-dev.vmx"
    kdnet_hostip: "192.168.126.1"  # VMnet8 (NAT) host adapter IP, used by kd_transport: net only
    encryption_password_env: ""    # env var name holding the password if the VM is encrypted
    encryption_password: ""        # or the password itself (vms.yaml is git-ignored)
    kd_transport: net              # net (KDNET, the default, one UAC prompt) or serial (named pipe, no prompt)
    serial_pipe: ""                # empty = \\.\pipe\ntdrive-<vm name>
    guest:
      user: "dev"
      password_env: "NTDRIVE_WIN11_DEV_PW"
      ssh_port: 22
      shell: powershell
    kdnet:
      port: 50000
      key: ""                      # filled in by kd_setup_guest
```

Only `vmware` is a valid `backend` this version. The daemon refuses any other value with `backend_unsupported`.

### 7.6 CLI and SDK mapping

CLI groups and verbs are generated mechanically from the `_` split in the tool name. `--json` gives the raw
tool JSON. Exit codes: 0 ok, 2 bad arguments, 3 `confirm_required`, 4 `guest_frozen_by_debugger`, 5 `timeout`,
1 otherwise.

| Tool | CLI | SDK |
|---|---|---|
| `vm_start` | `ntdrive vm start win11-dev` | `vt.vm.start("win11-dev")` |
| `snap_revert` | `ntdrive snap revert win11-dev base-kdnet --no-reopen-term` | `vt.snap.revert("win11-dev", "base-kdnet", reopen_term=False)` |
| `kd_exec` | `ntdrive kd exec win11-dev "!process 0 0"` or `--file cmds.txt` | `vt.kd.exec("win11-dev", ["!process 0 0", "k"])` |
| `kd_wait_event` | `ntdrive kd wait-event win11-dev --timeout 300` | `vt.kd.wait_event("win11-dev", timeout=300)` |
| `term_open` | `ntdrive term open win11-dev` -> prints `session_id` | `s = vt.term.open("win11-dev")` |
| `term_read` | `ntdrive term read <sid> --until "PS .*> $" --timeout 30` | `vt.term.read(s, until=r"PS .*> $", timeout=30)` |
| `term_send` | `ntdrive term send <sid> "ping -t 127.0.0.1"` or `--keys "{ctrl+c}"` | `vt.term.send(s, keys=["{ctrl+c}"])` |
| `file_push` | `ntdrive file push win11-dev .\build\mydrv.sys C:\drv\` (directory and glob ok) | `vt.file.push("win11-dev", "build/mydrv.sys", r"C:\drv\")` |
| `file_pull` | `ntdrive file pull win11-dev C:\Windows\MEMORY.DMP .\dumps\` | `vt.file.pull("win11-dev", r"C:\Windows\MEMORY.DMP", "dumps/")` |
| (CLI only) | `ntdrive term attach <sid>` (a person sits in the session, `Ctrl+]` detaches) | - |
| (CLI only) | `ntdrive daemon start\|stop\|status\|restart` | `NtDrive.daemon_status()` |
| `sys_state` | `ntdrive sys state --json` | `vt.sys.state()` |

---

## 8. Environment and setup

### 8.1 Host (common)

| Item | Requirement |
|---|---|
| OS | Windows 11 |
| Debugger | Debugging Tools for Windows from the SDK/WDK (`kd.exe`, `kdnet.exe`, `VerifiedNicList.xml`), or Store WinDbg (includes `kdnet` since 2026-03) |
| Runtime | Python 3.12, uv |
| Firewall | Allow kd.exe UDP inbound (all three profiles), and no inbound Block rule for kd.exe. `kd_setup_host` (net) sets this up through one UAC prompt. `scripts/setup-host.ps1 -FirewallOnly` as Administrator is the manual route. The serial transport needs none of this |
| Package | One Python package provides three entry points `ntdrived`, `ntdrive`, `ntdrive-mcp`. Users install with `uv tool install git+<repo>`, contributors with `uv tool install -e .` (`scripts/setup-host.ps1` does it), and both get the commands on PATH |
| Dev tools | uv, Ruff, mypy, pre-commit. Prettier is not installed separately because pre-commit builds a Node environment and runs it |

### 8.2 Host (VMware backend)

| Item | Requirement |
|---|---|
| VMware | Workstation Pro 17.6 or newer (Broadcom distribution, free for personal use). Includes `vmrun.exe` |
| Guest NIC | `ethernet0.virtualDev = "e1000e"` (vmx). Confirm `VEN_8086&DEV_10D3` in guest Device Manager |
| Network | NAT (VMnet8) recommended. hostip = IPv4 of "VMware Network Adapter VMnet8" |
| Firmware | UEFI, Secure Boot **off** (VM settings -> Options -> Advanced) |
| VMware Tools | Installed (guest IP, screenshot, file-copy fallback) |

### 8.3 Host (Hyper-V backend, for a later version)

| Item | Requirement |
|---|---|
| Edition | **Windows 11 Pro/Enterprise/Education**. Home not officially supported |
| Feature | Hyper-V role plus the PowerShell Hyper-V module. `pwsh` 7 recommended |
| VM generation | Gen2. Secure Boot **off** (`Set-VMFirmware -EnableSecureBoot Off`). Enhanced Session off |
| Switch | External or internal virtual switch (static IP). No Default Switch. hostip = that vEthernet adapter IPv4 |
| Checkpoint | `Set-VM -CheckpointType Standard` |
| Guest services | "PowerShell Direct (Guest Service Interface)" of Integration Services turned on |

### 8.4 Guest (Windows 10 22H2 / 11 24H2 x64, common)

| Item | Requirement |
|---|---|
| OpenSSH Server | `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`, or the Win32-OpenSSH zip (`install-sshd.ps1`) where the capability cannot be installed (Insider builds have no Feature-on-Demand package). `scripts/setup-guest.ps1` tries the capability and falls back to the zip. Service auto-start, default shell set to PowerShell (`HKLM:\SOFTWARE\OpenSSH\DefaultShell`) |
| Kernel debug | `bcdedit /debug on` plus either `bcdedit /dbgsettings serial debugport:1 baudrate:115200` (serial) or `bcdedit /dbgsettings net hostip:<hostip> port:<n> key:<key>` (KDNET, the default), then reboot. `kd_setup_guest` does this over SSH |
| Login | At least one local account (for PowerShell Direct and SSH auth) |

### 8.5 First-time setup flow (run by the agent)

```
   ntdrive setup                       -> a person: pick the VM, enter the account and passwords,
                                          vms.yaml written, daemon restarted (FACE-10)
0. kd_setup_host win11-dev             -> net (default): read the host firewall rules for kd.exe,
                                          repair them through one UAC prompt when they block KDNET
                                          serial: add the named-pipe COM port to the vmx (VM off)
1. sys_health                          -> check binaries, config and backend capability
2. vm_start win11-dev                  -> boot
3. term_open win11-dev transport=auto  -> enter SSH (falls back to psdirect on Hyper-V if SSH is not up)
4. kd_setup_guest win11-dev            -> optional on net: kd_attach does it when no key is saved.
                                          Reads back the KDNET key the guest script set (adopted),
                                          or applies bcdedit (serial or net) and saves the key
5. vm_reboot win11-dev mode=soft       -> only when step 4 wrote settings (needs_reboot)
6. kd_attach win11-dev                 -> serial: running at once, net: running once the target connects
7. kd_break -> kd_exec "!process 0 0" -> kd_go
8. snap_take win11-dev "base-kd"
```

`ntdrive verify` (FACE-11) is the human form of steps 1 to 7: it runs them, reboots the guest when
KDNET was just configured, and ends with ALL SET or the first failing check and its fix.

---

## 9. Milestones

| Stage | Deliverables | Done when |
|---|---|---|
| **M0 skeleton** | Repo structure (English README, CLAUDE.md), `vms.yaml`/`policy.yaml` schema, ToolRegistry, `ntdrived` (HTTP+WS, `/health`, token, auto-start), `ntdrive-mcp` and `ntdrive` CLI and SDK skeletons (generated from the registry), `HypervisorAdapter` and `TermTransport` interfaces, `sys_health`, setup scripts, `SKILL.md` draft, pre-commit hooks (ruff, mypy, prettier, non-ASCII check) and CI | `sys_health` returns the same JSON over MCP, CLI and SDK, and `pre-commit run --all-files` passes (part of AT-9) |
| **M1 VM/snapshots (VMware)** | `VmwareAdapter`, `vm_*`, `snap_*`, vmrun parsers, snapshot tree | AT-1 passes |
| **M2 terminal** | TermManager, `SshPtyTransport`, `term_*`, pyte screen, reconnect poller, session logs | AT-2, AT-3 pass |
| **M3 KDNET** | KdSession, `kd_*`, sentinel execution, break-in, event wait | AT-4 passes |
| **M4 consistency** | Orchestrator (`snap_revert`, `vm_reboot`), StateStore transition rules, `sys_state`, policy gate | AT-5, AT-6 pass |
| **M5 collaboration and hardening** | CoView web terminal (in the daemon), CLI `term attach`, tool consolidation (<= 20), audit log, 48-hour soak test, docs | AT-7, AT-9, NFR met |
| **(later) Hyper-V / VirtualBox** | `HyperVAdapter` (long-lived pwsh) or `VirtualBoxAdapter` (`VBoxManage`), the matching transport and console, re-run AT-1 through AT-6 | AT-8 passes. Out of scope this release |

---

## 10. Acceptance tests

| ID | Scenario | Pass condition |
|---|---|---|
| AT-1 | Snapshot basics | On a running VM, `snap_take` -> shows in the `snap_list` tree -> after `snap_revert` the VM is running and its IP is queryable |
| AT-2 | Terminal streaming | After `term_send "ping -t 127.0.0.1"`, calling `term_read(delta)` five times at 1 s intervals shows new lines each time, and after `{ctrl+c}` the prompt returns via an `until` match |
| AT-3 | Screen mode | After `term_send "cls; Get-Process \| Select -First 5"`, the text of `term_read(screen)` matches the xterm.js co-view screen |
| AT-4 | Kernel debugger round trip | On a new guest, `kd_setup_host` (serial pipe, or the firewall repair for net) -> `kd_setup_guest` -> reboot -> `kd_attach` -> `kd_state` reaches `running` within 120 s -> `kd_break` reaches `broken` within 10 s -> `kd_exec "!process 0 0"` output contains `System` -> `kd_go`. Verified live over the serial pipe on 2026-09-11 (encrypted Windows 11 Insider guest, build 29648) |
| AT-5 | Recovery after revert | From `broken`, `snap_take` -> `kd_go` -> some work -> `snap_revert(reattach_kd, reopen_term)` -> all `steps` ok, `kd_state.running`, a successor session in `term_list` |
| AT-6 | BSOD loop | Deliberate bugcheck in the guest (NotMyFault, etc.) -> `kd_wait_event` returns `bugcheck` -> `kd_exec "!analyze -v"` -> `con_screenshot` has an image -> `snap_revert` -> guest healthy |
| AT-7 | Human intervention | When a person types `echo hi` in co-view, the agent's `term_read(delta)` shows that input (human tag) and its output |
| AT-8 | (later) Backend swap | After changing only `backend` to `hyperv` in `vms.yaml`, the section 8.5 flow and AT-1, AT-4, AT-5 pass with no change to tool names or arguments. `term_open transport=psdirect` runs `term_exec "hostname"` |
| AT-9 | Front-door parity | Calling `sys_state`, `snap_list`, `kd_exec "r"` over MCP, CLI (`--json`) and SDK yields identical JSON. Attaching with `ntdrive term attach` to a terminal the agent opened over MCP and typing `echo hi` shows it in the agent's `term_read(delta)` with a human tag. `term_list` and `kd_state` survive a Claude Code restart |

---

## 11. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Break-in signal does not reach a piped kd.exe | KD-3 fails | Verify early in M3 with a separate process group plus hidden console plus `CTRL_BREAK_EVENT`. If it fails, swap the KdSession backend to DbgEng COM (pybag/comtypes) |
| KDNET reconnect after snapshot revert is unstable | Core scenario S2 | Detach KD before revert as a rule, a retry policy, and worst case fall back to `vm_reboot mode=hard`. Regression-checked by AT-5 |
| Windows OpenSSH PTY (ConPTY) emits too many VT sequences and delta mode is noisy | Wasted agent tokens | Offer a pyte-based "cleaned delta" (screen diff) option in delta mode. Turn off PSReadLine prediction at shell start |
| `vmrun` fails intermittently depending on the Workstation GUI process | VM tool reliability | Retry (3 times, exponential backoff), and on failure re-check actual state with `vmrun list` |
| Win11 guest Secure Boot/vTPM rejects `bcdedit` | KD-7 fails | Document the Secure Boot disable procedure, and have `kd_setup_guest` pre-check |
| Long kd output (`!process 0 7`) blows up the MCP response | Wasted context | `max_bytes` default 64 KB, truncation flag, full text in the log file (artemis history-compression principle) |
| Agent forgets the `broken` state and hammers the terminal, hanging | Perceived deadlock | TERM-10 immediate error plus `hint: "call kd_go then retry"`. Listed as forbidden in `SKILL.md` |
| **Dev host is Windows 11 Home** so Hyper-V cannot be verified locally | Hyper-V adapter delay | Out of scope this version (section 12). Only the interface is set up in M0 |
| Hyper-V Default Switch IP changes every reboot | KDNET hostip mismatch | The setup script forces an internal/external switch plus static IP. `sys_health` checks hostip agreement |
| Hyper-V Production checkpoint drops memory | Live snapshot fails | The adapter checks and fixes `CheckpointType`. `snap_take` result states `memory_included` |
| PowerShell Direct session hangs on the guest `vmicvmsession` bug | psdirect transport down | Always pass `-Credential`, fall back to SSH on failure, and include the service-restart procedure in the message |
| Hyper-V on the host (when using VMware) | Slower | Document only. No functional impact |
| Daemon lifecycle failure (orphan `daemon.json`, port conflict, dead pid) | All three clients down | Record pid, version and start time in `daemon.json`. Clients check pid liveness and `/health`, and restart otherwise. On a port conflict move to the next port and update the file |
| Client/daemon version mismatch | Schema drift | Compare versions from `/health`. Refuse on a major mismatch and point to `ntdrive daemon restart` |
| Tool definitions drift across MCP, CLI and SDK | Threefold upkeep | FACE-2 single-source registry. AT-9 compares the three paths in CI |

---

## 12. Decisions and open issues

**Decided**
- Language: Python 3.12 (MCP official SDK, paramiko, pyte ecosystem). C# has easy DbgEng COM access but is awkward for SSH/PTY/web.
- KD backend MVP: `kd.exe` subprocess plus sentinel framing. Whether to swap to DbgEng COM is decided by the M3 break-in verification.
- **Scope: VMware Workstation only this version (settled 2026-09-10).** `vmrun` single path, no vmrest. Hyper-V and VirtualBox get only the `HypervisorAdapter` interface, not an implementation. VirtualBox was found feasible (`VBoxManage`, Intel PRO/1000 KDNET support) but deferred. Reason: shrink scope to finish M0 through M5 quickly, and Hyper-V cannot even be verified on the dev host (Windows 11 Home).
- Terminal transport: SSH PTY is the common primary for both backends. PowerShell Direct is secondary for Hyper-V setup and one-shot commands. Serial (as a terminal), hvc and VNC are interface-reserved only.
- **Kernel debug transport: KDNET is the default, serial is the alternative (2026-09-11, revised the same day).** The first decision of the day made the serial pipe the default because KDNET on this host was blocked by a pre-existing inbound Block rule for kd.exe that only an administrator could remove. `kd_setup_host` now repairs those rules itself through one UAC prompt, so the owner set KDNET back as the default: it is faster and needs no vmx edit. Serial stays fully supported as `kd_transport: serial` for hosts where nobody can approve a prompt (KD-11). The code default must stay `net`: a `serial` default makes every entry without the field count as configured (`kd_configured`), so reboot and revert try to attach over a pipe that was never set up.
- **Encrypted live snapshots go through suspend (2026-09-11).** vmrun refuses to create or delete a memory snapshot of a running encrypted VM even with the correct password, while every other operation works with `-vp`. `allow_suspend` on `snap_take` and `snap_delete` suspends, runs the operation, resumes and reattaches the debugger (SNAP-1, SNAP-4). The VMware UI can still snapshot in place if a human prefers that.
- **Front doors: MCP + CLI + Python SDK (re-decided 2026-09-10).** Set to MCP-only in v0.3 and then reversed. Agents use MCP, humans and CI the CLI, tests and automation the SDK. The three definitions are generated from one place, the ToolRegistry (FACE-2). Reasoning in section 6.5.
- **Process model: split out the `ntdrived` daemon from M0.** The MCP server, CLI and SDK are stateless clients. IPC is local HTTP + WebSocket, 127.0.0.1, token auth.
- **Language: repo documents and code text are English (2026-09-10).** README, CLAUDE.md, SKILL.md, comments, docstrings, tool descriptions and hints and error messages, logs, commit messages. **This PRD was made English too (settled 2026-09-11), so no file is exempt from the DEV-5 check.**
- **Style: no AI-looking notation (2026-09-10).** No emoji, em dash, decorative symbols or box-drawing diagrams (ST-10). This PRD follows the same rule.
- **Dev tooling: Ruff + mypy + Prettier (web assets only) + pre-commit (2026-09-10).** Formatting and linting are done by the hook, not by hand. The English-comment rule is enforced by the non-ASCII check hook (DEV-5). No Black, isort, flake8 or pylint.
- **Encryption: encrypted VMs supported (2026-09-11).** The VM encryption password is stored in `vms.yaml` as an env var name (preferred) or inline, and passed to vmrun with `-vp` (VM-6). The password is masked in results and logs.
- CoView is P1, served inside the daemon.

**Open issues**
1. Whether to put multi-VM kd.exe process and UDP port management (50000, 50001, ...) in M1 or defer to M4.
2. Whether to show human input to the agent tagged as "typed by a human" (for audit and context). Current plan: tag it.
3. Session log format: asciicast v2 vs a custom JSONL. Current plan: asciicast v2 plus a sidecar JSONL.
4. Whether to add a tool that pre-populates an offline symbol server cache.
5. Whether to include Linux guests (serial console, hvc ssh) in 1.0.
6. (deferred, later) How to get a Hyper-V verification host: upgrade to Windows 11 Pro vs a separate machine.
7. (deferred, later) Settle the post-`Restore-VMCheckpoint` VM state (saved/running) and the adapter normalization logic. VirtualBox needs a power-off before revert, so it fits the same normalization rule.
8. Tool consolidation: whether to group into `vm_power(action=start|stop|reboot|suspend|resume)`, `snap(action=list|take|revert|delete)` to get under 20, or keep one tool per action as now. Current plan: consolidate in M5 after looking at real usage logs.
9. Whether to add daemon idle exit. Current plan: no (explicit stop). If added, it must never exit while an active KD session exists.
10. Whether to generate CLI subcommand names mechanically from tool names (`kd wait-event`) or hand-tune them (`kd wait`). Current plan: mechanical generation plus aliases only.

---

## 13. References

**KDNET / debugger**
- Microsoft, Supported Ethernet NICs for Network Kernel Debugging, Windows 11: https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/supported-ethernet-nics-for-network-kernel-debugging-in-windows-11
- Microsoft, Supported Ethernet NICs, Windows 10: https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/supported-ethernet-nics-for-network-kernel-debugging-in-windows-10
- Microsoft, Set Up KDNET Network Kernel Debugging Manually: https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/setting-up-a-network-debugging-connection
- Microsoft, Setting Up KDNET Automatically (kdnet.exe): https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/setting-up-a-network-debugging-connection-automatically
- Microsoft, Setting Up Network Debugging of a Virtual Machine with KDNET (Hyper-V Gen2): https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/setting-up-network-debugging-of-a-virtual-machine-host

**VMware**
- Broadcom, vmrun command examples (Workstation 17): https://techdocs.broadcom.com/us/en/vmware-cis/desktop-hypervisors/workstation-pro/17-0/using-vmware-workstation-player-for-windows-17-0/using-the-vmrun-command-to-control-virtual-machines-win/running-vmrun-commands-win/examples-of-vmrun-commands-win.html
- Broadcom, Workstation Pro REST API: https://techdocs.broadcom.com/us/en/vmware-cis/desktop-hypervisors/workstation-pro/26H1/using-vmware-workstation-pro/using-vmware-workstation-pro-rest-api.html
- StevTheDev/vmrun-reference: https://github.com/StevTheDev/vmrun-reference

**Hyper-V**
- Microsoft, Manage Windows VMs with PowerShell Direct: https://learn.microsoft.com/en-us/virtualization/hyper-v-on-windows/user-guide/powershell-direct
- Thomas Maurer, HVC: SSH Direct for Linux VMs on Hyper-V: https://www.thomasmaurer.ch/2018/04/hvc-ssh-direct-for-linux-vms-on-hyper-v/
- PowerShell/Win32-OpenSSH issue #2200 (Hyper-V socket support request, open): https://github.com/PowerShell/Win32-OpenSSH/issues/2200

**Agent design references**
- google/artemis (Android automation, MCP+CLI+SDK, live mirroring, history compression): https://github.com/google/artemis
- memoryforensics1/windbg-mcp (C#, DbgEng COM + vmrun): https://github.com/memoryforensics1/windbg-mcp
- svnscha/mcp-windbg (cdb/kd subprocess): https://github.com/svnscha/mcp-windbg
- gengstah/windbg-mcp (pybag based): https://github.com/gengstah/windbg-mcp
