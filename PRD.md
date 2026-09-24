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
| VM-2 | Start (headless by default), stop (soft = guest shutdown, hard = power off), suspend, resume. A start that fails while the vmx names a saved state that is not a `.vmss` next to it (`checkpoint.vmState` left behind by a suspend or by a snapshot taken while suspended) is reported with `reason=saved_state_stale`, `sys_health` lists the leftover as an issue, and `vm_start discard_saved_state=true` drops it and boots fresh from the disk. | P0 |
| VM-3 | Three reboot modes: `soft` (`shutdown /r /t 0` in the guest), `hard` (backend reset), `kd` (`.reboot` while broken in the debugger). A soft reboot is confirmed by the guest's boot time advancing (some guests return success from `shutdown /r` without rebooting), and when it does not the daemon falls back to a hard reset (a `reboot_verify` step then a `reset` step in the result). Take an option for auto-reattaching KD and the terminal after reboot. A soft or hard reboot requested while the target is broken in resumes it first (a `kd_go` step in the result) instead of refusing: the reboot is the orchestrated step, so it does not send the caller away to do that by hand. | P0 |
| SNAP-1 | Take a snapshot. Includes memory when running (live snapshot). Record name, description and auto tags (time taken, kd state). The result states `via` (`direct` or `suspend-resume`) and `memory_included`. On a running **encrypted** VMware VM vmrun refuses a memory snapshot, so with `allow_suspend=true` the daemon suspends the VM (memory goes into the encrypted `.vmss`), snapshots, then resumes. That path behaves like `vm_suspend`: it refuses while the debugger is broken in, detaches the debugger and drops terminal sessions first, and reattaches the debugger afterwards. When the resume fails (one retry after a pause) the snapshot stays and is recorded, and the call fails with `error.completed` (the result it would have returned), `error.power` (where the VM was left) and `error.resume_error` (its own reason and hint, `saved_state_stale` naming `vm_start discard_saved_state=true`). The result lists `snapshots` and `current`, so the caller need not call `snap_list` to confirm. Hyper-V forces a Standard checkpoint (`Set-VM -CheckpointType Standard`). | P0 |
| SNAP-2 | Return the snapshot list as a **tree**. Report the current position (which snapshot the VM descends from). | P0 |
| SNAP-3 | Revert a snapshot. Because the post-revert power state differs by backend (vmrun does not run it, Hyper-V may be saved), the adapter **normalizes to running** (can be turned off). Detach KD before, and orchestrate KD re-attach and terminal reconnect after. | P0 |
| SNAP-4 | Delete a snapshot (optional children). **Refused without `confirm: true`.** Deleting a memory snapshot of a running encrypted VM has the same vmrun limitation as SNAP-1 and the same `allow_suspend` workaround. | P1 |
| VM-4 | Guest IP lookup (`vmrun getGuestIPAddress -wait` / `Get-VMNetworkAdapter`). Used internally by the terminal and file tools. | P0 |
| VM-5 | The VMware backend uses **`vmrun` as the single path**. The Workstation REST API (vmrest) does not support snapshots. | P0 |
| VM-6 | **Encrypted VM support.** A VMware Workstation encrypted VM needs a password to open the vmx. Store the per-VM password in `vms.yaml` as an environment variable name (`encryption_password_env`, preferred) or inline (`encryption_password`, acceptable because `vms.yaml` is git-ignored) and add `-vp <password>` to every vmrun command that opens the vmx. Like other secrets, the password never appears in tool arguments, results or logs. If the password is required but missing, surface the vmrun error as `backend_error`. The adapter classifies vmrun failures once and tags the error with a `reason` (`password_required`, `encrypted_live_snapshot`, `config_unreadable`, `snapshot_missing`), so no tool matches English error text. | P0 |
| VM-7 | **Hardware settings.** `vm_config(vm, cpus?, memory_mb?, nic?)` reads the VM's virtual hardware from the vmx (`numvcpus`, `cpuid.coresPerSocket`, `memsize`, `ethernet0.virtualDev`) and, when arguments are given, writes them while the VM is powered off (Workstation rewrites the vmx on power off, the same rule as the serial pipe). `cpus` writes one socket with that many cores because Windows client editions ignore CPUs beyond their socket limit. `nic` is `e1000e`, `e1000` or `vmxnet3`, and `sys_health`'s NIC warning names this tool as the fix. The vmx is edited byte for byte apart from the touched lines. The same shape as `modify_vm_resources` in vSphere MCP servers and the network reconfiguration tools of Proxmox ones. | P1 |
| VM-9 | **Clone and delete a VM, to give each agent its own guest.** `vm_clone` makes a new registered VM from a source snapshot, linked by default (shares the base disk, so it starts near zero and grows only with its own writes) or full. Each clone gets its own KDNET port (net) or pipe (serial), and a note that the clone's guest still points at the base's, so `kd_setup_guest` then `vm_reboot mode=soft` are needed on the clone before `kd_attach`. `vm_delete` (confirm required) powers the VM off, deletes its files and drops its `vms.yaml` entry. A linked clone shares disk but not memory: a running clone uses its own guest RAM, so the host runs as many at once as it has memory for, with the rest off or suspended. This is the recommended way to run several agents without them interfering, since one VM is a single shared resource (a `kd_break` freezes it, a revert or reboot drops its sessions). | P1 |
| VM-8 | **Kill a VM that vmrun no longer controls.** After a guest bugcheck under load, `vmrun stop hard` and `vmrun reset` can time out again and again while the VM's `vmware-vmx.exe` sits wedged and its `*.lck` files block the next start. `vm_stop mode=kill` (confirm required, like hard) ends the `vmware-vmx` and `vmrun` processes whose command line names the vmx, waits for them, deletes the `*.lck` entries next to the vmx and reports `{killed, locks_removed}`. `vm_start` then boots the VM again. Every vmrun timeout on a power or snapshot command names this path in its hint, while a `getGuestIPAddress -wait` timeout is reported as the guest still booting or VMware Tools not running, with kill as the last resort. `vm_reboot mode=kd` names `mode=hard` and kill when KDNET dropped during a crash. It is a power cut: whatever the guest had not flushed is lost, which `mode=soft` avoids while the guest still answers. | P1 |

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
| KD-8 | Symbol path: pass the configured value with `-y`, normalized so dbghelp accepts it. The local cache element of a `srv*<cache>*<url>` path must use backslashes (`C:\symbols`, not `C:/symbols`, which symsrv reports as "not a valid store" and which stopped `.reload`), and a path that names a URL without a store keyword gets an `srv*` prefix. Default `srv*C:\symbols*https://msdl.microsoft.com/download/symbols`. kd.exe runs on the host so it can reach the Microsoft symbol server. | P0 |
| KD-9 | On detach, resume the target (`g`) and then stop the process. Force-kill option. | P0 |
| KD-10 | Reconnect the debugger after snapshot revert or reboot, for both transports. The target looks for the debugger again early in boot, so restarting the host-side kd.exe reconnects. A live session is kept through a reboot and waited on. A session that does not announce the reconnection within the timeout is respawned, for KDNET as much as for serial, so the reported state is a known one: a KDNET session left waiting sat at [no_debuggee] after a hard reboot until someone detached and attached by hand. Retry policy (count, interval) on failure. | P0 |
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
| TERM-8 | Resize, close, session list (each session's state, last activity time, shell, and the ids that are open), and prune: forget closed and disconnected sessions, which pile up after reboots and reverts because a stale id must keep pointing at its successor until someone drops it. | P0 |
| TERM-9 | On reboot/revert/network drop the session is marked `disconnected` and reads report it at once. The server polls the SSH port and, once reachable, creates a **new session**, and reading with the old id points to the successor id. | P0 |
| TERM-10 | While the debugger is `broken`, the guest is frozen, so `term_*` calls do not wait and return `guest_frozen_by_debugger` at once. | P0 |
| TERM-11 | Record all session input and output with a `T+mm:ss.mmm` relative timestamp (asciicast v2 compatible). Tag human input separately from agent input. | P1 |
| TERM-12 | **Human co-view**: from a local web page (xterm.js + WebSocket) a person can watch a session live and type into it. Agent input and human input are multiplexed onto the same PTY. | P1 |
| TERM-13 | Output size cap: a single `term_read` is at most 64 KB. Beyond that it returns `truncated: true` and the next cursor. | P0 |
| TERM-14 | **PowerShell Direct transport** (Hyper-V only): run guest commands without a network via `New-PSSession -VMName`. It is not a PTY, so `screen` mode is unsupported and only `term_exec` and `delta` work. Used for early setup (installing OpenSSH, KDNET bcdedit) before SSH works. Later version. | P2 |
| TERM-15 | `hvc ssh` (SSH over a Hyper-V socket) transport: a PTY without a network for Linux guests. Excluded for Windows guests because Win32-OpenSSH does not yet accept Hyper-V sockets. | P2 |
| TERM-16 | **Standard-user terminals.** A VM may name a second guest account without administrator rights (`guest.standard_user`, its password in `standard_password_env` or `standard_password`). `term_open account=standard` logs in as it, so the agent can drive the guest the way a plain user sees it (UAC prompts, access-denied paths, per-user settings). The default `account=admin` (`guest.user`) stays what ntdrive itself uses for bcdedit over SSH and SFTP, and each account gets its own SSH connection. Reopened sessions keep their account. `scripts/setup-guest.ps1 -Standard` creates the account (`ntdrive-user`), `ntdrive setup` asks for it, `ntdrive verify` proves its login. | P1 |
| TERM-16 | Serial console transport for Linux guests. Same interface. | P2 |

### 5.5 FR-CON: console screen (secondary)

| ID | Requirement | Priority |
|---|---|---|
| CON-1 | Save a console screenshot as a PNG file and return the path. base64 optional. For BSOD, boot hang, login screen. `vmrun captureScreen` on Workstation is a VIX guest operation (verified on 17.6: no credentials returns "Anonymous guest operations are not allowed"), so it needs a working guest login and cannot shoot a broken or logged-out guest. The failure hint points at fixing the login or reading a crashed guest through the debugger. `con_enable_vnc` turns on Workstation's built-in VNC server (`RemoteDisplay.vnc`, VM off) and `con_screenshot method=vnc` then reads the framebuffer without a guest login. | P0 |
| CON-2 | Console key and pointer input (`con_send_keys`, `con_click`), VMware over its built-in VNC (`RemoteDisplay.vnc`), so no guest login is needed. For when SSH is unavailable: a lock or login screen, or before the network is up. Keys are text or `{token}` in the terminal's vocabulary ({enter}, {ctrl+alt+delete}, {win+r}, ...), and a `{password}` or `{standard_password}` item is expanded from `vms.yaml` inside the daemon and typed character by character, so the secret never enters the arguments, the result or the audit log. `con_click` sends an RFB PointerEvent (move, press, release) at a framebuffer pixel read off `con_screenshot method=vnc`, for example to pick a user tile on the lock screen. `con_run` executes a command on the logged-in interactive desktop (session 1), which SSH in session 0 cannot reach, through a scheduled task with LogonType Interactive and captures its output. `con_autologon` sets Windows automatic logon (the Winlogon `AutoAdminLogon`/`DefaultUserName`/`DefaultPassword` keys) over SSH, for one account, so a reboot lands on an unlocked interactive desktop (session 1), which SSH in session 0 cannot open. The password is read from `vms.yaml` inside the daemon and stored in the guest registry, in cleartext as Windows autologon requires, so it is for a debugging VM and `enabled=false` clears it. Hyper-V would use WMI `Msvm_Keyboard` in a later version. | P1 |
| CON-3 | **Login-free screenshot over VNC.** `con_enable_vnc(vm, port?)` writes `RemoteDisplay.vnc.enabled/port` into the vmx (VM off, like the serial pipe), and `con_screenshot method=vnc` reads that framebuffer from the host loopback with a built-in RFB client (no dependency), so it works at a login screen, a boot hang or on a frozen or unprovisioned guest, where `vmrun captureScreen` cannot. `method=auto` tries vmrun and falls back to VNC when guest login fails. VNC is set with no password and reached only over the host, which the enable result states. | P1 |

### 5.6 FR-FILE: file transfer

| ID | Requirement | Priority |
|---|---|---|
| FILE-1 | Copy files host->guest and guest->host. Primary is SFTP (reusing the SSH connection), secondary is the backend fallback (`vmrun copyFile*` / PowerShell Direct `Copy-Item`). A fallback push is verified too: one `Get-FileHash` run in the guest through the guest tools hashes every file of the call, the report is copied back and compared, so `verified` is filled either way. | P0 |
| FILE-2 | Driver deploy convenience: copy `.sys` and `.pdb` to a guest path and refresh the symbol path. | P1 |
| FILE-3 | Recursive directory copy and globs (`build/*.sys`). Create the destination directory if missing. For large files, compare SHA-256 on both sides after transfer and record it as `verified` in the result. If the debugger is `broken`, fail at once with `guest_frozen_by_debugger` like TERM-10. | P0 |
| FILE-4 | **Query and delete guest files without a shell.** `file_stat(vm, remote)` returns `{exists, size, modified, is_dir}`, `file_ls(vm, remote)` lists a directory, and `file_delete(vm, remote, recurse=false)` removes a path. SFTP first, VMware Tools (a PowerShell one-liner captured through a temp file) as the fallback, so freshness can be checked (size and mtime) and stale files cleared even when the terminal is unavailable. `file_pull`/`file_stat` always read the live guest filesystem: there is no host-side content cache. | P1 |

### 5.7 FR-STATE: unified state and orchestration

| ID | Requirement | Priority |
|---|---|---|
| ST-1 | `sys_state` returns VM power, current snapshot position, KD state, terminal session list and last event in one call. | P0 |
| ST-2 | The server guarantees the order of compound operations. Example: `snap_revert` = KD detach -> revert -> start -> KD attach (optional) -> wait for SSH -> resume terminal (optional). The result records each step. | P0 |
| ST-3 | Log every tool call to a JSONL audit log (secrets masked in arguments, `T+` relative time included). | P0 |
| ST-4 | Destructive operations (`snap_delete`, `vm_stop mode=hard` or `kill`, `vm_reboot mode=hard`) are refused without `confirm: true`. No other tool has a `confirm` argument. A policy file (`policy.yaml`) tunes the per-tool level (allow/confirm/deny). | P0 |
| ST-5 | The front doors are **MCP, CLI and Python SDK**, defined in section 5.8 (FR-FACE). The core is a pure Python library (`ntdrive.core`) and the three front doors are thin layers on top. The core must be callable directly from pytest and a REPL, without the daemon or MCP. | P0 |
| ST-6 | Ship `SKILL.md`: document the state model, standard procedures (setup, debug loop, BSOD recovery) and forbidden moves (calling the terminal while broken, and so on) for agents. | P0 |
| ST-7 | **One tool per action, and a cheap tool list.** Tool schemas load into context at session start, so every description is one or two sentences, parameter descriptions do not repeat what the schema carries, and the registry emits a compact JSON schema (no property titles, no model docstrings, `x or null` as one node). Measured 2026-09-14: 39 tools, about 18k chars of descriptions plus schemas, about 4.6k tokens per `tools/list`. Decided in open issue 8. | P1 |
| ST-8 | The max wait of a long-poll tool (`kd_wait_event`, `term_read(until)`) must be shorter than the MCP client's tool-call timeout. The server holds the cap as a setting and clips a longer request to the cap, returning a `timeout` event. | P0 |
| ST-9 | **Language rule**: every document and text string in the repository (README, CLAUDE.md, SKILL.md, this PRD, docstrings, tool description and hint, error messages, logs, commit messages) is written in **English**. **Code comments too.** No file is exempt. This is enforced by the DEV-5 hook. | P0 |
| ST-10 | **Style rule**: documents and code text are written plainly, without an AI look. No emoji, no em dash, no decorative symbols (arrow, check, star glyphs), no box-drawing-character diagrams, and no overuse of bold and headers per section. **No semicolon as sentence punctuation** either. End sentences with a period. Semicolons inside code and commands (`cmd; .echo`, PowerShell) are the exception. Draw diagrams with plain characters like `+ - | >` and write arrows as `->`. The DEV-5 hook mechanically catches non-ASCII and semicolons in Markdown prose. | P0 |

### 5.8 FR-FACE: front doors (MCP, CLI, SDK)

| ID | Requirement | Priority |
|---|---|---|
| FACE-1 | **Daemon `ntdrived`**: the only process that holds sessions (SSH connections, PTY, `kd.exe`, ring buffers) and the StateStore. Exposes the tool API over local HTTP + WebSocket (127.0.0.1, default 8765). One instance per user (lock file). | P0 |
| FACE-2 | **Single source tool registry**: tool name, argument schema (pydantic) and handler are defined once in the `ntdrive.core.tools` registry. MCP tools, HTTP endpoints, CLI subcommands and SDK methods are all **generated** from this registry. The same definition is never written three times by hand. | P0 |
| FACE-3 | **MCP server `ntdrive-mcp`**: stdio, `mcp.server.Server` from the official Python SDK. Stateless, and forwards tool calls to the daemon over HTTP. Auto-starts the daemon if absent. Registered in Claude Code with one line in `.mcp.json`, whose command is the installed `ntdrive-mcp` (`uv tool install`, editable for contributors) so the entry carries no path and no `uv run`. At initialize the server sends `instructions`, a short digest of SKILL.md (call sys_health first, a broken-in debugger freezes the guest, revert and reboot reattach for you and reopen terminals under new ids, only snap_delete, vm_stop mode=hard or kill and vm_reboot mode=hard take confirm, no secrets in arguments), so an agent without the skill file still gets the rules. Every tool carries MCP annotations from the registry: `readOnlyHint` and `destructiveHint` come from the tool's `effect` (read, additive, destructive), `idempotentHint` from its `idempotent` flag, and `openWorldHint` is false because the tools reach only the VMs in `vms.yaml`. | P0 |
| FACE-4 | **CLI `ntdrive`**: `ntdrive <group> <verb> [args]` maps one-to-one to tools (`ntdrive kd exec win11-dev "!process 0 0"`). Default output is a human table, `--json` gives the raw tool result. Exit codes map to `error.code`. To avoid shell quoting, command bodies can come from `--stdin`/`--file`. | P0 |
| FACE-5 | **CLI `term attach <session>`**: connect the local console raw to a daemon PTY session (WebSocket). A person sits down in a session the agent opened. Detach key is `Ctrl+]`. Input is logged with a human tag. | P1 |
| FACE-6 | **Python SDK `ntdrive`**: `NtDrive()` is a daemon client (default). `NtDrive(inprocess=True)` runs the core in the same process without the daemon (for tests and a REPL). Method names and arguments match the tools (`vt.kd.exec("win11-dev", "!process 0 0")`). | P0 |
| FACE-7 | **Daemon lifecycle**: `ntdrive daemon start\|stop\|status\|restart\|logs`. Clients read `%LOCALAPPDATA%\ntdrive\daemon.json` (port, pid, token, version) and attach, and if the file is missing or the pid is dead they auto-start a detached process and wait for `/health`. The detached daemon runs windowless (`pythonw.exe`, `DETACHED_PROCESS`) so nothing flashes on the desktop, and it logs one line per tool call (name, caller, outcome, never the arguments) to `daemon.out.log`, which `daemon logs [-f]` tails. The daemon stays up until an explicit stop (no idle exit). | P0 |
| FACE-8 | **Auth and version**: every request carries the random token from `daemon.json` as a header. The file has per-user ACL. `/health` returns the version and a client with a different major version is refused. | P0 |
| FACE-9 | **Front-door parity**: calling the same tool over MCP, CLI (`--json`) and SDK yields the same JSON (AT-9). Error codes and hints match too. | P0 |
| FACE-10 | **`ntdrive setup`**: a CLI command that writes the `vms.yaml` entry for one VM so nobody edits YAML by hand. It lists the VMs in the VMware Workstation inventory (or takes `--vmx`), reads the vmx (display name, encryption, NIC, Secure Boot), asks for the guest account and the passwords with hidden input, stores the passwords as User environment variables (`--inline-secrets` keeps them in `vms.yaml` instead), writes the entry, restarts the daemon and prints the `sys_health` issues that remain. Running it again adds another VM or updates one. Passwords are never accepted on the command line. `scripts/setup-host.ps1` wraps it into the one-run host setup: tools check, `uv sync`, `ntdrive setup` per VM, daemon restart, `kd_setup_host` per VM, `sys_health`, with no elevated shell. | P1 |
| FACE-11 | **`ntdrive verify`**: a CLI command that proves a VM end to end and says so: sys_health issues, the VM running (started if off), an SSH login as the configured account, the debugger transport on the host (firewall or serial pipe), then a real attach, break in, resume and detach. When the attach reports that KDNET was just configured in the guest, verify soft-reboots the guest and attaches again. It ends with ALL SET or the first failing check and its fix, exit code 0 or 1, `--json` for the report. `scripts/setup-host.ps1` runs it last, `-Verify` runs only it, so the order in which the host and guest scripts ran does not matter. | P1 |

### 5.9 Non-functional requirements

| Item | Bar |
|---|---|
| Latency | Terminal output to agent visibility within 100 ms. A simple kd command (`r`, `k`) round trip within 1 s. A Hyper-V PowerShell call round trip within 500 ms (reusing the long-lived pwsh process). |
| Stability | Zero server crashes over a 48-hour continuous session. Detect a dead child process (kd.exe, pwsh), update state and restart. |
| Concurrency | Up to 8 terminal sessions and 1 KD session per VM. Several VMs run at once, and the daemon serves several clients concurrently (no global lock, state keyed per VM), so several agents work in parallel by each driving its own VM (`vm_clone` gives each one). Two agents on the same VM are not isolated: it is one shared resource. There is no per-agent access control yet, any client with the daemon token can drive any VM. |
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
- **A snapshot of a suspended VM can leave `checkpoint.vmState` pointing at the snapshot's `.vmsn`** (seen 2026-09-18 on an encrypted VM after the `allow_suspend` path). `vmrun start` then fails with "The operation was canceled" and nothing else. Removing the `checkpoint.vmState` lines from the vmx boots the VM fresh from its disk, and the snapshot keeps the memory state.
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
| DEV-1 | **Ruff is the only Python formatter and linter.** Use `ruff format` and `ruff check` (the pre-commit hook adds `--fix`). Config lives in `pyproject.toml`: line-length 100, target py312, the rule families listed there (`E, F, I, UP, B, SIM, D` with Google-style docstrings, plus `RUF, BLE, SLF, ISC, N, PIE, C4, PERF, FURB, RSE, RET, T20, PT, ERA, TID, PGH, A, Q, LOG, G` and a few `PL` rules). Do not add Black, isort or flake8. | P0 |
| DEV-2 | **mypy for type checking.** `strict` for the whole package. Parsers of external binary output declare their return types. | P0 |
| DEV-3 | **Prettier for web assets (CoView HTML/CSS/JS).** `.prettierrc` is printWidth 100, singleQuote, semicolons on. Not applied to Python. | P1 |
| DEV-4 | **Everything automated by a pre-commit hook.** `.pre-commit-config.yaml` runs ruff check, ruff format and mypy from the project environment (the versions in `uv.lock`, not a second pin), prettier (web assets only), the non-ASCII check (DEV-5), trailing-whitespace, end-of-file-fixer, check-yaml, check-toml, check-merge-conflict. M0 puts `pre-commit install` in the setup script. CI (GitHub Actions, windows-latest) runs `pre-commit run --all-files` and `pytest`. | P0 |
| DEV-5 | **Code comments, docstrings, identifiers, strings and log messages are English only.** The hook `scripts/check_ascii.py` refuses a commit when it finds a non-ASCII character in any text file pre-commit hands it (`types: [text]`, so scripts, LICENSE and `uv.lock` included). In Markdown files it also catches semicolons outside code fences and inline code. The `[tool.check_ascii] exclude` list in `pyproject.toml` is empty, so every file including this PRD is checked. This hook mechanically enforces ST-9 (English) and ST-10 (no emoji, decorative symbols or semicolons). | P0 |
| DEV-6 | **Docstrings required on public functions and classes** (ruff `D` rules). One-line summary plus args, returns and raises. Comments say "why", not "what". | P0 |
| DEV-7 | **Editor and repo conventions.** `.editorconfig` (UTF-8, LF, Python and TOML 4 spaces, PowerShell, YAML, JSON and web assets 2 spaces, `.cmd` files CRLF), LF pinned by `.gitattributes`. Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`), in English. | P0 |
| DEV-8 | **Tests.** pytest + pytest-asyncio. Unit tests replace `vmrun`, `kd.exe` and SSH with fakes. Live checks against a real VM are manual: `ntdrive verify` (FACE-11) and the `SKILL.md` procedures. The one test that touches the real Windows firewall runs only with `NTDRIVE_LIVE_TESTS=1`. `uv run pytest` is the one entry point. | P0 |
| DEV-9 | **AGENTS.md (English) is the source of truth for these conventions**, with `pyproject.toml` for the tool settings. `CLAUDE.md` points at it. This table states the requirement, not the reference. | P0 |

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
| ntdrive-mcp | stdio MCP server. Generates tools from the registry and forwards calls to the daemon. Stateless | `mcp.server.Server` from the official Python SDK. Auto-starts the daemon |
| ntdrive CLI | Generates subcommands from the registry. Table or `--json` output, `term attach` raw passthrough | click + httpx + websockets |
| Python SDK | `NtDrive()` daemon client, or `inprocess=True` runs the core directly | httpx. Methods generated from the registry |
| StateStore | The single truth of VM, KD and TERM state. Adapters and sessions push changes into it, tools read it before touching a VM | In-memory, session-relative `T+` clock, last event per VM. No event bus and no periodic reconciliation in this version |
| HypervisorAdapter | Backend abstraction for power, snapshots, IP, console and vmx hardware | `VmwareAdapter` (vmrun, `-T ws`, `-vp` for encrypted VMs, one `vmrun list` per call) plus `hypervisor/vmx.py` for vmx reads and edits. Hyper-V is a later version (3.2) |
| KdSession | kd.exe lifecycle, output reader thread, prompt and sentinel detection, break-in over CTRL_BREAK, serial pipe liveness | Prompt regex on the tail of the output. The daemon writes the session log itself (`kd_state.log_path`), kd.exe gets no `-loga` |
| TermManager / TermTransport | Per-session PTY, ring buffer, pyte screen, reconnect polling | `SshPtyTransport` (paramiko). `PsDirectTransport`, `HvcSshTransport` and `SerialTransport` are names reserved for a later version, no code yet |
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
- While `KD.broken`, `term_*`, `file_*` and `con_screenshot` (guest path) updates fail at once with `guest_frozen_by_debugger`. The VNC console tools (`con_screenshot method=vnc`, `con_send_keys`) read or drive the framebuffer over the host, so they are not blocked, but a broken-in guest will not process the keys until `kd_go`.
- `snap_revert` sets `KD` to `detached`, `vm_reboot` keeps kd.exe attached and waiting for the reconnect, and both set every `TERM` to `disconnected`, then recover per the options.
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

Common: tools that act on a VM take `vm` (a name from `vm_list`), the `term_*` session tools take `session_id`, and `vm_list` and `sys_health` take nothing. Every field that carries guest or debugger output is capped by `max_bytes` (default 65536, ceiling 1048576) and paired with `truncated`. Results are JSON. Errors carry `error.code`
(`vm_not_running`, `guest_frozen_by_debugger`, `kd_not_attached`, `session_disconnected`, `confirm_required`,
`backend_unsupported`, `timeout`) and `error.hint` (what the agent should do next). Tool names and schemas do
not change with the backend (VMware/Hyper-V). The same tool is also exposed over HTTP (`POST /api/tools/<name>`),
CLI (`ntdrive <group> <verb>`) and SDK (`vt.<group>.<verb>()`) (section 7.6). Tool descriptions, hints and error
messages are written in English (ST-9).

### 7.1 VM / snapshots

| Tool | Arguments | Returns |
|---|---|---|
| `vm_list` | - | `{vms:[{name, backend, power, kd, current_snapshot, term_open:[ids], term_disconnected:[{session_id, successor}], guest_frozen, last_event, power_error?}]}` |
| `vm_state` | `vm, probe=false` | detail of one item above. `probe=true` adds `guest_reachable` (whether SSH answers now). The `kd` block already tells a running kernel from one halted at the debugger (state `broken`, `last_event.event` `bugcheck`) |
| `vm_wait_ready` | `vm, timeout=180` | `{ready, ip, waited_s, note?}`. Long-polls until the guest answers SSH, for after a reboot or a bugcheck's auto-restart. Returns `ready=false` at the timeout instead of erroring |
| `vm_start` | `vm, gui=false, discard_saved_state=false` | `{power, saved_state_dropped?: {saved_state, removed}}` |
| `vm_stop` | `vm, mode=soft\|hard\|kill, confirm?` | `{power, terms_dropped, killed?, locks_removed?, power_error?}` |
| `vm_reboot` | `vm, mode=soft\|hard\|kd, confirm? (hard), reattach_kd=true, reopen_term=true, timeout=180` | `{steps:[...], kd, term:[{old, new}]}`. On a failure `error.steps` lists what ran |
| `vm_suspend` / `vm_resume` | `vm` | `{power}` |
| `vm_config` | `vm, cpus?, memory_mb?, nic=e1000e\|e1000\|vmxnet3?` | `{hardware:{cpus, cores_per_socket, memory_mb, nic}, before?, changed:[vmx keys]}` (a change needs the VM off) |
| `vm_clone` | `vm, name, snapshot?, linked=true` | `{vm, source, vmx, linked, snapshot, kd_transport, kdnet_port?, note}`. Registers a new VM in vms.yaml. Linked shares the base disk. An encrypted source is refused fast (`invalid_args`): vmrun cannot clone an encrypted VM, linked or full, so use the GUI or a snapshot/revert workflow on the base |
| `vm_register` | `vm (template), name, vmx` | `{vm, template, vmx, kd_transport, kdnet_port?, note}`. Registers an existing vmx (a manual or GUI clone) as a new VM, inheriting the template's guest, debugger and encryption config with a fresh KDNET port. The supported path for encrypted VMs, which no CLI can clone: clone in the GUI, then register |
| `vm_delete` | `vm, confirm` | `{deleted, terms_dropped}`. Powers off, deletes the files, drops the vms.yaml entry. A base with linked clones cannot be deleted until they are gone |
| `snap_list` | `vm` | `{tree:[{name, children:[...]}], current}` |
| `snap_take` | `vm, name, description?, allow_suspend=false` | `{name, taken_at, kd_state_at_snapshot, via, memory_included, snapshots, current, terms_dropped?, kd?}`. A failed resume after `allow_suspend` is a `backend_error` with `completed`, `power` and `resume_error` |
| `snap_revert` | `vm, name, start=true, reattach_kd=true, reopen_term=true, timeout=180` | `{steps:[...], kd, term}` |
| `snap_delete` | `vm, name, children=false, confirm, allow_suspend=false` | `{deleted:[...], current, via, terms_dropped?, kd?}` |

### 7.2 Kernel debugger

| Tool | Arguments | Returns |
|---|---|---|
| `kd_setup_host` | `vm`, `fix_firewall=true`, `timeout=120` (serial: VM must be off, edits the vmx. net: reads the host firewall rules for kd.exe and, when they block KDNET, removes the Block rules and adds an Allow rule through one UAC prompt. `fix_firewall=false` only reports) | `{transport, changed, serial_pipe?, firewall?, next}` |
| `kd_setup_guest` | `vm, port?, key?` (needs SSH to the guest) | `{transport, port?, key_saved, adopted, needs_reboot, steps}`. `adopted` means the guest already debugged to this host's IP (scripts/setup-guest.ps1 does that by default), so the port and key were read back over SSH instead of written, and `needs_reboot` is false when debugging was already on |
| `kd_attach` | `vm, port?, key?, symbol_path?, wait_for_target=true, timeout=120` | `{state, transport, target_info?, note?}` (`note` when the target did not connect within the timeout: a KDNET target connects while it boots, so `vm_reboot mode=soft` next). On net with no saved key it runs the `kd_setup_guest` step first (reads the guest's settings over SSH, `adopted`) and fails with a reboot hint when settings had to be written |
| `kd_detach` | `vm, force=false` | `{state}` |
| `kd_break` | `vm, timeout=20` | `{state, output}`. A timeout with no target ever connected (kd at [no_debuggee]) tells the caller to reboot the guest so KDNET reconnects |
| `kd_go` | `vm` | `{state}` |
| `kd_exec` | `vm, cmd | cmds[], timeout=60, max_bytes=65536` | `{outputs:[{cmd, output, truncated, elapsed_ms}]}` |
| `kd_wait_event` | `vm, timeout=300` | `{event: bugcheck\|breakpoint\|module_load\|user_break\|timeout, output, state, bugcheck?:{code, arguments}}`. On a bugcheck the code (`0x0000003b`) and up to four arguments are parsed from the banner, so `!analyze -v` is not needed just to see them |
| `kd_state` | `vm` | `{attached, state, transport, port, serial_pipe, target_info, last_event, log_path, pid, previous_session?, note?}`. `attached` and `state` come from the live kd.exe process: when it is gone the state is `detached` and what the previous session saw sits under `previous_session`, never mixed into the present |
| `kd_log_tail` | `vm, bytes=16384` | `{text}` |

### 7.3 Terminal

| Tool | Arguments | Returns |
|---|---|---|
| `term_open` | `vm, shell=powershell\|cmd\|pwsh, transport=auto\|ssh, account=admin\|standard, cols=120, rows=40` | `{session_id, transport, account, coview_url}` |
| `term_send` | `session_id, text | keys[], enter=true` | `{bytes_sent}` |
| `term_read` | `session_id, mode=delta\|screen, until?, timeout=0, max_bytes=65536, cursor?, clean=true` | `{text, cursor, truncated, lost_before_cursor, matched?, state, successor?}`. A wait ends with `guest_frozen_by_debugger` when the target stops at `kd>` |
| `term_exec` | `session_id, cmd, timeout=60, max_bytes=65536` | `{output, exit_code?, state, truncated, note?, elapsed_ms}` (`exit_code` is null with a `note` when the shell reported no number: PowerShell sets `$LASTEXITCODE` only after an external program ran. A dead session raises `session_disconnected` instead) |
| `term_resize` | `session_id, cols, rows` | `{}` |
| `term_close` | `session_id` | `{}` |
| `term_list` | `vm?` | `{sessions:[{session_id, vm, shell, transport, account, state, last_activity, successor?}], open:[session ids that are usable], coview: the CoView page URL, #<session_id> selects one}` |
| `term_prune` | `vm?` | `{pruned:[ids], remaining}`. Forgets closed and disconnected sessions. Open successors stay |

### 7.4 Console / file / system

| Tool | Arguments | Returns |
|---|---|---|
| `con_screenshot` | `vm, base64=false, method=auto\|guest\|vnc` | `{png_path, via, png_base64?}` |
| `con_enable_vnc` | `vm, port?` | `{changed, port, note}` (VM off) |
| `con_send_keys` | `vm, keys[]` | `{sent}` (the count of items, not characters, so a password's length does not leak). VNC must be on (`con_enable_vnc`). `{password}`/`{standard_password}` items type that account's password from `vms.yaml` |
| `con_click` | `vm, x, y, button=left\|right\|middle, double=false` | `{clicked:[x, y], button, double}`. VNC must be on. Pixels are the ones `con_screenshot method=vnc` captures |
| `con_run` | `vm, cmd, account=standard\|admin, timeout=60, capture=true, max_bytes=65536, detach=false` | `{exit_code, state, output?, truncated?, note?}`, or with `detach=true` `{detached, task, log, state, note}`. Runs `cmd` in the account's interactive session (session 1) through a scheduled task, so the account must be logged in (con_autologon). `detach=true` starts it and returns at once, leaving a long-lived process running with no time limit and its output going to `log` (read later with `file_pull`). A Task Scheduler status in `exit_code` (>= 0x41300) means the task never ran, usually no interactive session |
| `con_autologon` | `vm, enabled=true, account=standard\|admin` | `{enabled, account, user, needs_reboot}`. Sets or clears the Winlogon autologon keys over SSH so a reboot lands on an unlocked interactive desktop. The password comes from `vms.yaml` server-side, never in the arguments, result or log |
| `file_push` | `vm, local, remote, verify=true` (`local` is an absolute host path, the CLI and SDK absolutize) | `{files, bytes, verified, verified_count, via: sftp\|guest_tools, copied:[{local, remote, bytes, verified, verify_error?}], note?}`. Top-level `verified` is a bool like each `copied[].verified` (true when every file matched, false on any mismatch or unreadable hash, null when verify is off). `verified_count` is how many matched |
| `file_pull` | `vm, remote, local` (a trailing separator on `local` means directory) | `{bytes, via, note?}` |
| `file_stat` | `vm, remote` | `{exists, size?, modified?, is_dir?, via}` |
| `file_ls` | `vm, remote` | `{exists, entries:[{name, size, modified, is_dir}], via}` |
| `file_delete` | `vm, remote, recurse=false` | `{deleted, via}` |
| `sys_state` | `vm?` | unified VM, KD, TERM state |
| `sys_health` | - | `ok` (false when the host or any VM has an issue), `problems`, `vm_issues`, binary paths and versions, backend capabilities, hypervisor service, a `kdnet_firewall` read when any VM uses net, and per VM: config `issues` (missing vmx, Secure Boot on, wrong NIC for KDNET, encrypted VM without a password, empty password environment variables, missing serial pipe or KDNET key, a stale saved state in the vmx, each naming the fix), `power`, `kd_state`, `guest {ip, user, standard_user, ssh_port, ssh_open, skipped}`, and `serial_pipe {path, open}` or `kdnet_port {port, free, held_by_ntdrive, firewall_ok}`. The guest probe is bounded to a few seconds and skipped while the VM is off or frozen by the debugger |

### 7.5 Config file (`vms.yaml`)

```yaml
host:
  vmrun: "C:/Program Files (x86)/VMware/VMware Workstation/vmrun.exe"
  kd:    "C:/Program Files (x86)/Windows Kits/10/Debuggers/x64/kd.exe"
  kdnet: "C:/Program Files (x86)/Windows Kits/10/Debuggers/x64/kdnet.exe"
  symbol_path: 'srv*C:\symbols*https://msdl.microsoft.com/download/symbols'
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
      user: "dev"                  # an administrator: ntdrive's own guest work and the default for terminals
      password_env: "NTDRIVE_WIN11_DEV_PW"
      standard_user: ""            # optional: a plain account for term_open account=standard
      standard_password_env: ""    # its password (or standard_password inline)
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
| `vm_config` | `ntdrive vm config win11-dev --cpus 2 --memory-mb 4096 --nic e1000e` | `vt.vm.config("win11-dev", cpus=2, nic="e1000e")` |
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
| Login | At least one local administrator account with a password (for PowerShell Direct and SSH auth, and bcdedit over SSH). `scripts/setup-guest.ps1` creates `ntdrive` for this. `-Standard` also creates `ntdrive-user`, a member of Users only, for terminals opened with `account=standard` |
| Power | Sleep and hibernate off: a debugged or remotely driven VM must never sleep (it freezes and drops SSH) or hibernate (it tears down the KDNET/serial link). `scripts/setup-guest.ps1` sets `powercfg /change standby-timeout-* 0` and `/hibernate off` |

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
| **M5 collaboration and hardening** | CoView web terminal (in the daemon), CLI `term attach`, a cheap tool list (ST-7), audit log, 48-hour soak test, docs | AT-7, AT-9, NFR met |
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
- **A failed resume after `allow_suspend` is an error with the facts (2026-09-18).** Live use showed the resume failing after suspend and snapshot had succeeded, the VM left off and the error blaming the Workstation install. The call now retries the resume once, records the snapshot, and fails with `completed`, `power` and `resume_error`. A saved state the vmx names but cannot restore is classified `saved_state_stale`, reported by `sys_health`, and cleared by `vm_start discard_saved_state=true` (VM-2, SNAP-1).
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
8. Tool consolidation: decided on 2026-09-14 to keep one tool per action (39 tools) and to keep the tool list cheap instead (ST-7: short descriptions, a compact schema, about 4.6k tokens per `tools/list`).
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
