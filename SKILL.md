---
name: ntdrive
description: Drive a VMware Workstation Windows guest through the ntdrive MCP tools - power, snapshots, kernel debugging with kd.exe over a serial pipe or KDNET, and a real-time SSH terminal. Use when asked to boot, snapshot, revert, debug a driver, analyze a BSOD, or run commands inside a VM.
---

# ntdrive for agents

You control VMs through MCP tools named `<group>_<verb>`: `vm_*`, `snap_*`, `kd_*`, `term_*`,
`con_*`, `file_*`, `sys_*`. Every tool takes `vm` (the name from `vms.yaml`) and returns JSON.
Errors carry `error.code` and `error.hint`. The hint tells you what to do next. Backend errors
also carry `error.reason` when the daemon recognized the cause (for example
`encrypted_live_snapshot`).

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
2. `snap_revert`, `vm_reboot`, `vm_suspend`, `vm_stop` and the `allow_suspend` path of
   `snap_take` and `snap_delete` detach the debugger and drop every terminal session. Revert,
   reboot and the suspend path reattach the debugger for you (`reattach_kd`, default true), and
   revert and reboot reopen terminals (`reopen_term`). Read the `steps` array (or `terms_dropped`
   and `kd` on the snapshot tools) to see what happened.
3. After a reconnect the old session id still works for `term_list` and reports `successor`, the
   id of the new session. Use the new id.
4. Call `sys_state` whenever you are unsure. It is cheap and returns VM, debugger and terminal
   state in one answer.

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
in.

### Set up a fresh guest (net transport, the default)

KDNET is the default. `kd_setup_host` reads the host firewall rules for kd.exe and, when they
block it, repairs them through one UAC prompt that a person at the desktop must approve (pass
`fix_firewall=false` to only look, `sys_health` reports the same check as `kdnet_firewall`).
`kd_setup_guest` generates and saves the KDNET key.

```
sys_health                                 -> issues per VM, for example "host firewall blocks KDNET"
kd_setup_host vm=win11-dev                 -> firewall checked, repaired after the UAC prompt
vm_start vm=win11-dev
term_open vm=win11-dev                     -> session_id (needs OpenSSH in the guest)
kd_setup_guest vm=win11-dev                -> bcdedit /dbgsettings net, key saved, needs_reboot=true
vm_reboot vm=win11-dev mode=soft confirm=true
kd_attach vm=win11-dev                     -> waiting, then running once the guest boots with the debugger on
kd_break vm=win11-dev                      -> broken, target_info filled in
kd_exec vm=win11-dev cmd="!process 0 0"
kd_go vm=win11-dev
snap_take vm=win11-dev name=base-kd
```

With `kd_transport: serial` (a VMware named pipe, no firewall and no prompt) the flow is the same
except that `kd_setup_host` must run while the VM is off, because it adds the COM port to the vmx,
`kd_setup_guest` writes the serial bcdedit setting, and `kd_attach` reports `running` at once.

### Driver deploy and debug loop

```
file_push vm=win11-dev local=C:\work\build\mydrv.sys remote=C:\drv\mydrv.sys
term_exec session_id=<sid> cmd="sc create mydrv type= kernel binPath= C:\drv\mydrv.sys"
kd_break vm=win11-dev
kd_exec vm=win11-dev cmd="bp mydrv!DriverEntry"
kd_go vm=win11-dev
term_exec session_id=<sid> cmd="sc start mydrv"        # may hang if the bp hits first; use term_send
kd_wait_event vm=win11-dev timeout=120                  -> event=breakpoint
kd_exec vm=win11-dev cmds=["k", "dv", "r"]
kd_go vm=win11-dev
```

### BSOD analysis and recovery

```
kd_wait_event vm=win11-dev timeout=600     -> event=bugcheck
kd_exec vm=win11-dev cmd="!analyze -v"
con_screenshot vm=win11-dev                -> png_path
snap_revert vm=win11-dev name=base-kd      -> steps: detach, revert, start, attach, term
```

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
same flag. A snapshot of a powered-off VM never needs it.

## Files

- `file_push` and `file_pull` use SFTP when the guest has OpenSSH and fall back to VMware Tools
  (`via: guest_tools`). Guest-tools copies are not hashed, so `verified` is `null` for them and
  `note` says why. A `verify_error` on a copied entry means the hash could not be read.
- `local` must be an absolute host path. The daemon runs in another process and does not share
  your working directory. A trailing separator on `file_pull local` means "put it in this
  directory".

## Tips

- `term_exec` is for short commands with a clear end. For interactive or streaming programs use
  `term_send` plus `term_read`.
- `term_read mode=screen` shows exactly what a person sees on the terminal (rows x cols). Use it
  for menus, progress bars and anything that redraws the screen.
- `kd_exec` accepts a list in `cmds` so that several debugger commands cost one tool call.
- Large outputs are cut at `max_bytes` (64 KB by default) and flagged `truncated: true`. The
  full text is in the session log named in `kd_state.log_path`.
- Destructive tools need `confirm=true`: `snap_delete`, `vm_stop mode=hard`, `vm_reboot mode=hard`.
  Soft and kd reboots run without it.
- `kd_exec` runs whatever you send at the `kd>` prompt, including `.shell`, which executes
  commands on the host. Do not use it unless the task calls for it.
- Never put passwords or KDNET keys in tool arguments. They live in `vms.yaml` and environment
  variables on the host.
