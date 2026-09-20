---
name: ntdrive
description: Drive a VMware Workstation Windows guest through the ntdrive MCP tools - power, snapshots, kernel debugging with kd.exe over a serial pipe or KDNET, and a real-time SSH terminal. Use when asked to boot, snapshot, revert, debug a driver, analyze a BSOD, or run commands inside a VM.
---

# ntdrive for agents

You control VMs through MCP tools named `<group>_<verb>`: `vm_*`, `snap_*`, `kd_*`, `term_*`,
`con_*`, `file_*`, `sys_*`. Tools that act on a VM take `vm`. Get the names from `vm_list`, or
from `sys_health`, which you call first anyway. Do not read `vms.yaml`: it holds credentials and
the daemon has already loaded it. `term_send`, `term_read`, `term_exec`, `term_resize` and
`term_close` take `session_id` (from `term_open`) instead of `vm`. Every tool returns JSON.
Errors carry `error.code` and `error.hint`, and the hint names the next call. Backend errors also
carry `error.reason` when the daemon recognized the cause (for example `encrypted_live_snapshot`).

## State model (read this first)

- VM power: `off`, `running`, `suspended`.
- Debugger (`kd_state`): `detached`, `waiting` (kd.exe is up, target not connected yet),
  `running` (target runs, no prompt), `broken` (target frozen at a `kd>` prompt). `kd_state` also
  reports `transport` (`serial` or `net`) and, for serial, the `serial_pipe` in use.
- Terminal session: `open`, `disconnected` (the guest rebooted, was reverted or suspended),
  `closed`.

Rules that follow from the state model:

1. While `kd_state == broken` the whole guest is frozen. `term_*`, `file_*` and `con_screenshot`
   fail at once with `guest_frozen_by_debugger`. Call `kd_go` before touching the guest.
   `vm_reboot` (soft or hard) resumes a broken-in target itself and reports it as a `kd_go` step.
2. `snap_revert`, `vm_suspend`, `vm_stop` and the `allow_suspend` path of `snap_take` and
   `snap_delete` detach the debugger and drop every terminal session. `vm_reboot` drops the
   terminals but keeps kd.exe running and waits for the target to reconnect (step
   `kd_reconnect`), respawning it only when that takes longer than `timeout`. Revert and reboot
   reattach the debugger (`reattach_kd`, default true) and reopen terminals (`reopen_term`,
   default true). The result has `steps` (each `{step, ok, ...}`, `skipped` says why a step did
   not apply), `kd` (the debugger status after the reattach, or null) and `term`, a list of
   `{old, new}` session ids. Use `new` at once, no `term_list` needed. Revert steps: kd_detach,
   term_drop, snapshot_revert, start, kd_attach, guest_ip, term_reopen. Reboot steps: kd_go (only
   for a broken-in target), term_drop, guest_shutdown or reset or kd_reboot, kd_reconnect (or
   kd_attach), guest_ip, term_reopen.
3. When a step fails the call returns that step's error and `error.steps` lists what ran before
   it. A `timeout` from term_reopen means the guest rebooted and kd is back, only SSH was late:
   `term_open` once the guest is up. An old session id still answers `term_list` and names its
   `successor`.
4. `kd_state` and `term_list` answer in milliseconds and cover the debugger and the terminals.
   `sys_state` adds power, which costs one `vmrun list` (about half a second) for every VM of the
   call. `sys_health` probes the guest too and is the slowest, so call it first and then when
   something is wrong, not as a heartbeat.
5. One VM is one shared resource: a `kd_break` freezes it and a revert or reboot drops its
   sessions, so several agents must not share a VM. To work in parallel, give each agent its own
   VM with `vm_clone` (a linked clone shares the base disk, so it is cheap, but a running clone
   uses its own RAM, so keep only as many running as the host has memory for) and `vm_delete` it
   when done. A fresh clone's guest still points at the base's debugger, so run `kd_setup_guest`
   then `vm_reboot mode=soft` on the clone before `kd_attach`.

## Standard procedures

### Check that everything is connected

`sys_health` is the one call for "is the host wired up and does the guest answer". It lists
host `problems` (binaries, config) and, per VM, `issues` plus a live probe: `power`,
`kd_state`, `guest.ssh_open` (a TCP connect to the guest SSH port, bounded to a few seconds)
and either `serial_pipe.open` (the host has a pipe server, so the running VM exposes COM1) or
`kdnet_port.free`. The guest probe is skipped while the VM is off or frozen at a kd prompt,
and `guest.skipped` says which. Read `issues` first: every entry names the fix.

Two things the probe cannot prove. An open SSH port is not a working login, so `term_open` is
the real test. Over the serial transport `kd_state == running` only means kd.exe is alive.
The target is proven connected when `kd_break` reaches a `kd>` prompt and `target_info` fills
in. A person can run `ntdrive verify` on the host, which does exactly this sequence for every
VM and prints ALL SET or the first failing check with its fix.

### Set up a fresh guest (net transport, the default)

KDNET is the default. `kd_setup_host` reads the host firewall rules for kd.exe and, when they
block it, repairs them through one UAC prompt that a person at the desktop must approve (pass
`fix_firewall=false` to only look, `sys_health` reports the same check as `kdnet_firewall`).
When no KDNET key is saved yet, `kd_attach` first reads the port and key that
scripts/setup-guest.ps1 configured in the guest and saves them. `kd_setup_guest` is the explicit
form of that step (`adopted: true`), for a guest set up by hand (it then writes the settings,
`needs_reboot: true`, so `vm_reboot mode=soft` next) or to change the port or key.

```
sys_health                                 -> issues per VM, for example "host firewall blocks KDNET"
kd_setup_host vm=win11-dev                 -> firewall checked, repaired after the UAC prompt
vm_start vm=win11-dev
term_open vm=win11-dev                     -> session_id (needs OpenSSH in the guest)
kd_attach vm=win11-dev                     -> no key saved: reads the guest's KDNET settings over SSH
                                              (adopted), then waiting, then running once the target connects.
                                              Still waiting after the timeout (the result carries a note):
                                              vm_reboot mode=soft, kd.exe keeps waiting and the target
                                              connects while the guest boots
kd_break vm=win11-dev                      -> broken, target_info filled in
kd_exec vm=win11-dev cmd="!process 0 0"
kd_go vm=win11-dev
snap_take vm=win11-dev name=base-kd
```

With `kd_transport: serial` (a VMware named pipe, no firewall and no prompt) the flow is the same
except that `kd_setup_host` must run while the VM is off, because it adds the COM port to the vmx,
`kd_setup_guest` writes the serial bcdedit setting, and `kd_attach` reports `running` at once.

Waiting for a boot: `vm_start` returns when vmrun does, before the guest is usable. `term_open` is
the boot wait: it blocks up to 60 s for VMware Tools to report an IP and 10 s for SSH. A `timeout`
that says VMware Tools reported no IP, or a `backend_error` whose message starts with `ssh
connect`, in the first minutes after a start means the guest is still booting: retry `term_open`,
or poll `sys_health` until `guest.ssh_open` is true. Do not touch credentials for that.

`vm_config` reads or changes the virtual hardware in the vmx. When `sys_health` says the guest
NIC is not `e1000e`, `vm_config vm=win11-dev nic=e1000e` fixes it, and `cpus=1` or
`memory_mb=4096` shape the VM for a debugging session (a single CPU makes some races easier to
follow). Changes need the VM off: `vm_stop`, `vm_config`, `vm_start`. Without arguments it only
reports the current values, at any power state.

### Driver deploy and debug loop

```
file_push vm=win11-dev local=C:\work\build\mydrv.sys remote=C:\drv\mydrv.sys
term_exec session_id=<sid> cmd="sc create mydrv type= kernel binPath= C:\drv\mydrv.sys"
kd_break vm=win11-dev
kd_exec vm=win11-dev cmd="bp mydrv!DriverEntry"
kd_go vm=win11-dev
term_exec session_id=<sid> cmd="sc start mydrv"        # ends with guest_frozen_by_debugger if the bp
                                                        # hits first: kd_exec, kd_go, then term_read
kd_wait_event vm=win11-dev timeout=120                  -> event=breakpoint
kd_exec vm=win11-dev cmds=["k", "dv", "r"]
kd_go vm=win11-dev
```

### BSOD analysis and recovery

```
kd_wait_event vm=win11-dev timeout=600     -> event=bugcheck
kd_exec vm=win11-dev cmd="!analyze -v"
kd_exec vm=win11-dev cmd=".dump /f C:\\dumps\\crash.dmp"   # written on the host, no guest needed
con_screenshot vm=win11-dev                -> png_path. vmrun captureScreen needs a working guest login, so for a login screen, a boot hang or a frozen guest enable VNC once (con_enable_vnc, VM off) and use con_screenshot method=vnc, which reads the framebuffer with no guest login
con_screenshot vm=win11-dev method=vnc     -> read the pixel of a user tile or field on the lock screen
con_click vm=win11-dev x=640 y=400         -> click it over VNC (framebuffer pixels), no guest login
con_send_keys vm=win11-dev keys=["{password}","{enter}"]  -> then type the password over VNC, no SSH
con_autologon vm=win11-dev account=standard  -> a reboot lands on the unlocked desktop, no click needed
con_run vm=win11-dev cmd="myexe.exe" account=standard  -> run a GUI/session program on that desktop, capture output
snap_revert vm=win11-dev name=base-kd      -> steps: kd_detach, term_drop, snapshot_revert, start,
                                              kd_attach, guest_ip, term_reopen, and term: [{old, new}]
```

A crashed guest answers neither SSH nor VMware Tools, so `file_pull` cannot fetch its logs until it
is back: the debugger is the post-mortem tool, and `C:\\Windows\\MEMORY.DMP` can be pulled after the
reboot. When KDNET dropped during the crash (`kd_state` shows no target, `kd_break` times out with
[no_debuggee]) `vm_reboot mode=kd` cannot work: use `vm_reboot mode=hard confirm=true`. When vmrun
itself stops answering for the VM (timeouts on stop, reset or list after a crash), `vm_stop
mode=kill confirm=true` ends its vmware-vmx process on the host and clears the `.lck` files, then
`vm_start` boots it again. `mode=hard` and `kill` discard whatever the guest had not flushed, so
`mode=soft` is the clean way down while the guest still answers.

### Watch a streaming command

```
term_send session_id=<sid> text="ping -t 127.0.0.1"
term_read session_id=<sid> mode=delta                     # repeat; each call returns new output
term_send session_id=<sid> keys=["{ctrl+c}"] enter=false
term_read session_id=<sid> until="PS .*> $" timeout=30
```

### Snapshot a running encrypted VM

`snap_take` on a running encrypted VM fails with `error.reason=encrypted_live_snapshot` because
vmrun cannot snapshot its live memory. Retry with `allow_suspend=true`: the daemon suspends the
VM, snapshots the saved state (memory included), resumes, and reattaches the debugger. Terminal
sessions are dropped, so reopen them with `term_open`. `snap_delete` on such a snapshot needs the
same flag. A snapshot of a powered-off VM never needs it. The result lists `snapshots`, so the
snapshot is confirmed without a `snap_list`.

When the resume fails the call fails, but the snapshot exists and is recorded: `error.completed`
is the result it would have returned, `error.power` is where the VM was left (`suspended` or
`off`) and `error.resume_error` is the start error. `vm_start` resumes it. When
`error.resume_error.reason` (or a later `vm_start` error) is `saved_state_stale`, the vmx names
a saved state Workstation cannot restore: `vm_start discard_saved_state=true` boots fresh from
the disk, and the snapshot keeps the memory state. `sys_health` reports such a leftover too.

## Files

- `file_push` and `file_pull` use SFTP when the guest has OpenSSH and fall back to VMware Tools
  (`via: guest_tools`). A guest-tools push is hashed too (`Get-FileHash` in the guest through the
  tools), so `verified` is true or false either way. A `null` with a `verify_error` means the hash
  could not be read.
- `local` must be an absolute host path. The daemon runs in another process and does not share
  your working directory. A trailing separator on `file_pull local` means "put it in this
  directory".
- `file_stat`, `file_ls` and `file_delete` inspect and clear guest files without the terminal.
  `file_pull` and `file_stat` always read the live guest filesystem (no host-side cache), so use
  `file_stat` to tell a fresh artifact from a stale one by its `size` and `modified`, and
  `file_delete` to clear a leftover before a new run. `vm_reboot` (soft or hard) keeps the disk:
  it is not a snapshot revert, so a file written before the reboot is still there afterwards. To
  start from a known disk use `snap_revert`. A pushed executable that vanishes after a reboot is
  usually Windows Defender removing it, not the disk resetting.

## Tips

- `term_exec` runs one complete statement and returns when the prompt is back: it appends its
  own end marker to the same line, so the statement must end normally. Nothing interactive, no
  trailing `&`, no `exit`, `shutdown` or `logoff`. `exit_code` is null when PowerShell ran no
  external program. For anything that streams, prompts, or ends the SSH connection use
  `term_send` (fire-and-forget, returns at once) and read later with `term_read`, or write to a
  file and `file_pull` it. After a command that ended the connection the session is
  `disconnected` with no successor: `term_open` again.
- `kd_state.attached` is the truth about kd.exe. A `detached` answer with `previous_session` means
  the last session's banner and break, not the present: call `kd_attach` before `kd_exec`.
- `term_list` returns `open`, the ids that are usable, and `coview`, the browser page that mirrors
  sessions live (`#<session_id>` selects one). `sys_state` and `vm_list` only list ids
  (`term_open`, and `term_disconnected` with successors). After reboots and reverts the stale ids
  stay in `term_list` (each names its successor) until `term_prune` forgets them.
- `term_open account=standard` opens the shell as the guest's plain account (`guest.standard_user`
  in vms.yaml, created by `setup-guest.cmd -Standard`) instead of the administrator. Use it when
  the question is what a normal user sees: UAC, access denied, per-user settings. `file_push`,
  `file_pull` and `kd_setup_guest` always use the administrator account. Without a standard
  account configured the call fails with `invalid_args` and says how to add one.
- `term_read mode=screen` shows exactly what a person sees on the terminal (rows x cols). Use it
  for menus, progress bars and anything that redraws the screen.
- `kd_exec` accepts a list in `cmds` so that several debugger commands cost one tool call.
- Symbols resolve out of the box: `kd_attach` passes a normalized `srv*C:\symbols*<msdl>` path
  and kd.exe downloads from the Microsoft server (it runs on the host). No `.sympath` fix needed.
- Reading output, three different `truncated` flags:
  - `term_read` (delta): `truncated: true` means more output waits after `cursor`. Call
    `term_read` again, it continues from the cursor. `lost_before_cursor: true` means the 1 MB
    ring overflowed and that output is gone. `until` with the default `timeout=0` returns at
    once: a miss is not an error response but a normal result with `matched: null` and an
    `error` object inside, so always pass `timeout` with `until` and check `matched`.
  - `term_exec`: `truncated: true` means `output` was cut at `max_bytes` (up to 1 MB) and the
    rest is not retrievable by tool. Redirect the command to a file and `file_pull` it. A
    `timeout` error carries the partial `output` and the command may still be running:
    `term_read` to watch it, `term_send keys=["{ctrl+c}"] enter=false` to stop it.
  - `kd_exec`: `truncated: true` per command, the full text is in the transcript (`kd_log_tail`,
    path in `kd_state.log_path`).
- Only `snap_delete`, `vm_stop mode=hard` or `kill` and `vm_reboot mode=hard` take `confirm=true`.
  No other tool has a `confirm` argument, and passing one is an `invalid_args` error.
- `unauthorized` or `daemon_unavailable` means the daemon was restarted or is down. The client
  re-reads `daemon.json` and retries once by itself, so a second call normally works. Live kd and
  terminal sessions are gone after a restart. If it keeps failing a person runs `ntdrive daemon
  status` on the host.
- `kd_exec` runs whatever you send at the `kd>` prompt, including `.shell`, which executes
  commands on the host. Do not use it unless the task calls for it.
- SSH runs in Windows session 0 (services), so `term_*` work even at a locked or logged-out
  desktop. The interactive desktop (session 1) is a separate thing: an app window opens only
  there, and only when it is logged in and unlocked. When a task needs an unlocked desktop and
  `con_screenshot method=vnc` shows a lock or login screen, log in over the console with
  `con_send_keys` (VNC must be on, `con_enable_vnc` with the VM off): `con_send_keys vm=... keys=`
  `["{password}", "{enter}"]` types the guest password from `vms.yaml` without it crossing the
  wire. When the login screen shows more than one account, read the tile's pixel off the same
  `con_screenshot method=vnc` and `con_click vm=... x=... y=...` it first, then send the password.
  To make a reboot land on an unlocked desktop instead of unlocking by hand each time, set
  Windows autologon: `con_autologon vm=... account=standard` writes the Winlogon keys over SSH
  (the password comes from vms.yaml, never through the arguments) and then `vm_reboot mode=soft`
  boots straight into the standard account's desktop. Autologon signs in one account only, so
  pick the one the task needs (standard for a Medium-IL desktop), and `con_autologon
  enabled=false` clears it when done. The password is stored in the guest registry in cleartext,
  so keep it to a debugging VM.
- Never put passwords or KDNET keys in tool arguments. They live in `vms.yaml` and environment
  variables on the host. `con_send_keys` `{password}` and `term_open` read them there for you.
