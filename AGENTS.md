# AGENTS.md

Instructions for any coding agent (Claude Code, Codex, Cursor, Copilot and others) that works in
this repository. `CLAUDE.md` points here. `SKILL.md` is a different document: it explains how an
agent *uses* the ntdrive tools to drive a VM, not how to change this code.

## What this is

`ntdrive` is a local daemon (`ntdrived`) plus three thin front doors (MCP server, CLI, Python SDK)
that let an agent drive VMware Workstation guests on Windows: power and snapshots, kernel
debugging with `kd.exe` over a serial named pipe or KDNET, and a real-time SSH terminal.
Requirements live in `PRD.md`. Setup is in `README.md`.

## What to read for a change

- A bug fix inside one module: this file and the module. `README.md` and `PRD.md` are not needed.
- A tool change (a new parameter, a new tool): this file, the tables in `PRD.md` section 7 and the
  matching requirement row in section 5 (rule 3 below needs the feature in the PRD), plus
  `SKILL.md` when the procedure for agents changes. The README table is generated.
- Setup, the scripts, `ntdrive setup` or `ntdrive verify`: also `README.md`, Setup.
- `PRD.md` section 12 lists decisions already made: read it before changing a default. The other
  PRD sections are background.

## Hard rules

- English only. Code, comments, docstrings, tool descriptions, error messages, logs, docs and
  commit messages. A pre-commit hook (`scripts/check_ascii.py`) rejects any non-ASCII character
  anywhere in the repo.
- Write plainly. No emoji, no em dashes, no decorative symbols, no box-drawing diagrams, and no
  semicolons as punctuation in prose (end the sentence instead). Semicolons inside code and
  commands are fine. Draw diagrams with `+ - | >` and write arrows as `->`. The hook checks
  Markdown prose for semicolons too.
- Do not add features that are not in `PRD.md`. If something is missing from the PRD, say so
  instead of building it. When a fix needs a new tool, add it to the PRD in the same change.
- One tool definition, three front doors. Every tool is declared once in
  `src/ntdrive/core/tools/*.py` through the registry. Never hand-write a tool a second time in
  the MCP server, the CLI or the SDK. They are generated from the registry.
- The daemon owns state. Clients (`ntdrive-mcp`, `ntdrive`, `NtDrive()`) must stay stateless.
- Secrets (guest passwords, KDNET keys, the VM encryption password, the daemon token) never
  appear in tool arguments, results, logs or audit records. They live in `vms.yaml` (git-ignored)
  either inline or as the name of an environment variable. Environment variables are preferred.
  Mask them in anything that is logged (`_mask_argv` in the VMware adapter, `redact` in
  `kd_setup_guest`).
- Destructive tools (`snap_delete`, `vm_stop mode=hard` or `kill`, `vm_reboot mode=hard`) require
  `confirm=true` and go through the policy gate in `ntdrive.core.policy`. No other tool has a
  `confirm` argument.
- Anything that becomes a command line in the guest (bcdedit arguments from `kd_setup_guest`)
  is validated against a strict pattern first. Free text from tool arguments never gets
  interpolated into a shell command except in the tools whose purpose is running a command
  (`term_exec`, `kd_exec`).
- While the debugger is broken in, the guest is frozen. Tools that touch the guest must fail fast
  with `guest_frozen_by_debugger` instead of hanging. Anything that suspends, stops or reverts the
  VM must detach the debugger and drop terminal sessions first (`NtDriveService.release_guest`).
- Host paths cross a process boundary. Tools that take a `local` path refuse relative paths.
  The CLI and SDK absolutize before sending and keep a trailing separator, which means
  "directory".
- Never commit `vms.yaml`, `policy.yaml`, `logs/` or anything under `.venv/`. They are ignored,
  keep it that way.

## Output of the setup commands and scripts

`ntdrive setup`, `ntdrive verify`, `scripts/setup-host.ps1` and `scripts/setup-guest.ps1` (each with a
`.cmd` launcher that bypasses the execution policy) share one shape, defined in `src/ntdrive/cli/log.py` and copied as small functions in the scripts:

- A section is `== n/total title`.
- A result line is two spaces, a tag padded to five characters (`OK`, `FAIL`, `WARN`, `INFO`, or
  `..` for something still running), a space, the subject, and a detail after a colon.
- A `FAIL` line is followed by `        fix: <the exact command or action>`.
- The last line is a verdict: `ALL SET: ...`, `DONE: ...` or `NOT READY: ...`, and NOT READY is
  followed by `  next:` with numbered steps.
- Passwords are typed masked and echoed partly masked (`mask()` in `cli/setup.py`). Nothing
  else about a secret is printed, ever.

## Commands

```powershell
uv sync                                          # once, and after a dependency change
uv tool install -e .                             # ntdrive, ntdrive-mcp, ntdrived on PATH, running this checkout
uv tool install -e . --reinstall                 # after a dependency change, with every MCP client closed
uv run --no-sync pre-commit install              # once per clone (scripts/setup-host.ps1 does it too)
uv run --no-sync pre-commit run --all-files      # ruff format, ruff check, mypy, prettier, ASCII check: what CI runs
uv run --no-sync pytest -q                       # unit tests with fakes for vmrun, kd.exe and SSH: what CI runs next
uv run --no-sync ntdrive daemon restart          # after editing daemon-side code, or the old code keeps running
```

`pre-commit run --all-files` and `pytest` must both pass before a commit. They are exactly what
`.github/workflows/ci.yml` (GitHub Actions, windows-latest) runs on every push to main and every
pull request, so a red check on GitHub means one of them failed on a clean machine. The hooks run
ruff and mypy from the project environment, so the versions in `uv.lock` are the only ones.
`--no-sync` because a sync rewrites `ntdrive-mcp.exe`, which fails while an MCP client holds it.
Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `ci:`,
`perf:`).

## Where things live

- `src/ntdrive/config.py`: `vms.yaml` and `policy.yaml` models and loaders.
- `src/ntdrive/errors.py`: `NtDriveError(code, message, hint)`, the error codes and the
  `REASON_*` tags the VMware adapter attaches so tools never match error text themselves.
- `src/ntdrive/hostproc.py`: `run_hidden` for vmrun and the PowerShell helpers, `no_window_kwargs`
  for kd.exe, `force_utf8_stdio` for the CLI and daemon stdio. Start every new host subprocess here.
- `src/ntdrive/paths.py`: host path helpers shared by the CLI, SDK and file tools.
- `src/ntdrive/core/registry.py`: `ToolSpec`, `ToolRegistry`, the `tool` decorator, and the
  compact JSON schema (no titles, `x | None` as one node) that LLM clients receive.
- `src/ntdrive/core/service.py`: `NtDriveService`, the object that owns adapters, sessions and
  state and dispatches tool calls with validation, policy and audit. `release_guest` detaches the
  debugger and drops terminals before anything that suspends, stops or reverts. `refresh_powers`
  asks the hypervisor once for every VM of a call.
- `src/ntdrive/core/state.py`: `StateStore`, `VmRuntime`, `TermInfo`, the state enums and the
  session-relative `T+` clock.
- `src/ntdrive/core/policy.py`: the allow, confirm, deny gate for destructive tools.
- `src/ntdrive/core/audit.py`: the JSONL audit log with secrets and view tokens masked.
- `src/ntdrive/core/orchestrator.py`: multi-step flows (`snap_revert`, `vm_reboot`) whose `steps`
  also travel in the error when a step fails.
- `src/ntdrive/core/tools/`: one module per tool group (`vm`, `snap`, `kd`, `term`, `console`,
  `file`, `sys`) plus `common.py` for the shared parameter models. Note `console.py`, not
  `con.py`: `CON` is a reserved file name on Windows.
- `src/ntdrive/hypervisor/`: `HypervisorAdapter` (`base.py`), `VmwareAdapter` (`vmware.py`: vmrun,
  `-vp` for encrypted VMs, `_mask_argv`, error classification, one `vmrun list` per call through
  `power_states`, serial pipe and VNC settings in the vmx) and `vmx.py` (the vmx reader and the
  hardware edits behind `vm_config`).
- `src/ntdrive/kd/session.py`: `KdSession` (kd.exe subprocess, sentinel-delimited command output,
  break-in via CTRL_BREAK, event classification, serial pipe liveness check).
  `src/ntdrive/kd/firewall.py`: the host firewall read and repair for KDNET.
- `src/ntdrive/term/`: `TermSession` (`session.py`: ring buffer, pyte screen, cursors, a bounded
  regex wait with a frozen-guest abort), `TermManager` (`manager.py`: sessions, reconnect,
  successor ids, prune), `SshPtyTransport` (`ssh.py`: every paramiko failure becomes
  `NtDriveError`), `transport.py`, and key tokens like `{ctrl+c}` in `keys.py`.
- `src/ntdrive/screen/vnc.py`: the RFB (VNC) client for `con_screenshot method=vnc` (read one
  framebuffer) and `con_send_keys` (send KeyEvent messages), sharing one handshake.
  `src/ntdrive/screen/keymap.py`: the `{token}` vocabulary turned into X keysym press and release
  events, the VNC counterpart of `term/keys.py`.
- `src/ntdrive/daemon/`: the aiohttp app (`app.py`), the `daemon.json` lifecycle (`lifecycle.py`),
  `DaemonClient` (`client.py`: one kept connection, re-reads `daemon.json` after a restart) and
  the CoView page (`static/coview.html`).
- `src/ntdrive/mcp/server.py` and `src/ntdrive/sdk/__init__.py`: generated front doors, no
  hand-written tool. `src/ntdrive/cli/main.py`: the generated tool commands plus the hand-written
  `daemon` group and `term attach` (its body is `cli/attach.py`). `cli/setup.py` (`ntdrive setup`,
  writes `vms.yaml`), `cli/verify.py` (`ntdrive verify`, the end-to-end check) and `cli/log.py`
  (the shared output shape) are hand-written too.
- `scripts/`: `setup-host.ps1` and `setup-guest.ps1`, each with a `.cmd` launcher that bypasses
  the execution policy, `probe-guest.ps1` (a guest diagnostic), `check_ascii.py` (the hook) and
  `tools_table.py` (the README table).
- `tests/conftest.py`: `FakeVmrun`, `FakeTransport`, `FakeKdProcess` and the `service` fixture.
  `tests/test_registry.py` pins the tool list and the README table, `tests/test_faces.py` the
  front-door parity over HTTP, `tests/test_imports.py` the light CLI and MCP imports. Live testing
  against a real VM is manual: `uv run ntdrive verify`, then the SKILL.md procedures by hand.

Import direction: `config.py`, `errors.py`, `hostproc.py`, `paths.py` and `core/state.py` import
nothing else from ntdrive. `hypervisor/`, `kd/`, `term/` and `screen/` import only those and their
own package. `core/tools/` import `core.service` only under `TYPE_CHECKING`. `cli/`, `mcp/` and
`sdk/` never import `core.service` at module level (the SDK imports it in its in-process mode),
which is what keeps a CLI command well under a second (`tests/test_imports.py` guards it).

## When you change a tool

1. Edit the parameter model and handler in `src/ntdrive/core/tools/<group>.py`. Every parameter
   gets a `description`, and the decorator states `effect` (read, additive or destructive) and
   `idempotent`: they become the MCP annotations and the Effect column of the README table.
2. Add or update a test that uses the fakes. `tests/test_registry.py` lists every tool name and
   `tests/test_faces.py` checks that MCP, HTTP, CLI and SDK all expose it.
3. Update `PRD.md` section 7 (the tool tables), refresh the README table with
   `uv run --no-sync python scripts/tools_table.py --write README.md`, and `SKILL.md` if the procedure for
   agents changes.
4. Restart the daemon (`uv run --no-sync ntdrive daemon restart`) before trying it live.

## Things that bit us in live testing

- `vmrun` cannot create or delete a memory snapshot of a running encrypted VM. The adapter tags
  that error `reason=encrypted_live_snapshot` and the snapshot tools offer `allow_suspend`.
- Right after a suspend `vmrun` may briefly report the vmx as unreadable, and a delete that worked
  can then report "does not exist". The suspend path checks the snapshot list, not the op result.
- A snapshot taken while suspended can leave `checkpoint.vmState` in the vmx pointing at the
  snapshot's `.vmsn`, and the resume then fails with "The operation was canceled" (2026-09-18,
  encrypted VM). The adapter reports that as `saved_state_stale`, `sys_health` lists it, and
  `vm_start discard_saved_state=true` drops the lines and boots fresh. The suspend path retries
  the resume once and, when it still fails, records the snapshot and fails with the facts
  (`error.completed`, `error.power`, `error.resume_error`) instead of a raw vmrun error.
- KDNET needs an inbound firewall allow for `kd.exe`, and Windows often has a leftover Block rule
  that wins. `kd_setup_host` (net) reads the rules without privilege and repairs them through one
  UAC prompt (`ntdrive.kd.firewall`). KDNET is the default. The serial pipe transport avoids
  all of that for hosts where nobody can approve a prompt.
- Win32-OpenSSH resolves `C:/x` relative to the home directory over SFTP. Paths must be `/C:/x`.
- `Add-WindowsCapability` for OpenSSH fails on Insider builds (no Feature-on-Demand package for
  them). The Win32-OpenSSH zip works, and `scripts/setup-guest.ps1` falls back to it on its own.
- PSReadLine redraws the input line on every keystroke and floods terminal reads. The terminal
  unloads it at session start.
- The daemon runs detached, without a console. A child started the normal way opens a console
  window on the desktop, and `CREATE_NO_WINDOW` alone still lets conhost flash for a frame.
  `ntdrive.hostproc` (`run_hidden`, `no_window_kwargs`) pairs it with a hidden `STARTUPINFO`, and
  vmrun and the PowerShell helpers go through it, so start every new host subprocess there.
  `daemon/lifecycle.py` starts ntdrived with `pythonw.exe` (`_daemon_executable`, the GUI-subsystem
  interpreter that never allocates a console) plus `DETACHED_PROCESS` and the same hidden
  STARTUPINFO, so the daemon cannot flash a window even for a frame. kd.exe takes
  `no_window_kwargs` too but keeps a console of its own (with `CREATE_NEW_PROCESS_GROUP`) because
  break-in attaches to that console to send CTRL_BREAK. `tests/test_kd.py` checks that delivery
  from a detached parent, so keep it green when touching `spawn_kd`.
- The daemon has no window, so `service.call` logs one line per tool call (name, caller, outcome,
  never the arguments, which may hold secrets) through the `ntdrive.call` logger to
  `daemon.out.log`. `ntdrive daemon logs [-f]` tails it: that is how a person watches what an
  agent is doing. Do not log argument values there.
- `vms.yaml` used to be looked up in the working directory too, so the daemon picked up whatever
  checkout the first client ran from. The config now lives only in `%LOCALAPPDATA%\ntdrive` (or
  `NTDRIVE_CONFIG`, or `--config`), clients resolve it and pass `--config`, and `ensure_daemon`
  restarts a config-less daemon once a config exists.
- `VmConfig.kd_transport` defaults to `net`, and every document says so. The default was flipped
  to serial for a few hours on 2026-09-11 and flipped back: a serial default makes every entry
  without the field count as configured (`kd_configured`), so reboot and revert tried to attach
  over a pipe that was never set up.
- `uv run` (when pyproject changed) and `uv tool install` rewrite `ntdrive-mcp.exe`, which fails
  with os error 32 while Claude Code has the MCP server open. Close Claude Code for those, or
  run the checks with `uv run --no-sync`.
