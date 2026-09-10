# AGENTS.md

Instructions for any coding agent (Claude Code, Codex, Cursor, Copilot and others) that works in
this repository. `CLAUDE.md` points here. `SKILL.md` is a different document: it explains how an
agent *uses* the ntdrive tools to drive a VM, not how to change this code.

## What this is

`ntdrive` is a local daemon (`ntdrived`) plus three thin front doors (MCP server, CLI, Python SDK)
that let an agent drive VMware Workstation guests on Windows: power and snapshots, kernel
debugging with `kd.exe` over a serial named pipe or KDNET, and a real-time SSH terminal.
Requirements live in `PRD.md`. Setup is in `README.md`.

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
- Destructive tools (`snap_delete`, hard `vm_stop`, hard `vm_reboot`) require `confirm=true` and
  go through the policy gate in `ntdrive.core.policy`.
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

## Commands

```powershell
uv sync                                  # install everything, including dev tools
uv run pytest -q                         # unit tests with fakes for vmrun, kd.exe and SSH
uv run ruff format src tests scripts     # the only formatter
uv run ruff check src tests scripts      # the only linter
uv run mypy                              # strict for ntdrive.core, basic elsewhere
uv run pre-commit run --all-files        # everything above plus the ASCII check
uv run ntdrive daemon restart            # after editing daemon-side code, or the old code keeps running
```

All four checks (ruff format, ruff check, mypy, pytest) must pass before a commit. Commit
messages follow Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).

## Where things live

- `src/ntdrive/config.py`: `vms.yaml` and `policy.yaml` models and loaders.
- `src/ntdrive/errors.py`: `NtDriveError(code, message, hint)`, the error codes and the
  `REASON_*` tags the VMware adapter attaches so tools never match error text themselves.
- `src/ntdrive/paths.py`: host path helpers shared by the CLI, SDK and file tools.
- `src/ntdrive/core/registry.py`: `ToolSpec`, `ToolRegistry`, the `tool` decorator.
- `src/ntdrive/core/service.py`: `NtDriveService`, the object that owns adapters, sessions and
  state and dispatches tool calls with validation, policy and audit.
- `src/ntdrive/core/orchestrator.py`: multi-step flows (`snap_revert`, `vm_reboot`).
- `src/ntdrive/core/tools/`: one module per tool group (`vm`, `snap`, `kd`, `term`, `console`,
  `file`, `sys`). Note `console.py`, not `con.py`: `CON` is a reserved file name on Windows.
- `src/ntdrive/hypervisor/`: `HypervisorAdapter` and `VmwareAdapter` (vmrun, `-vp` for encrypted
  VMs, error classification, serial pipe setup in the vmx).
- `src/ntdrive/kd/session.py`: `KdSession` (kd.exe subprocess, sentinel-delimited command output,
  break-in via CTRL_BREAK, event classification, serial pipe liveness check).
- `src/ntdrive/term/`: `TermSession` (ring buffer, pyte screen, cursors, regex wait),
  `SshPtyTransport` (every paramiko failure becomes `NtDriveError`), key tokens like `{ctrl+c}`.
- `src/ntdrive/daemon/`: aiohttp app, `daemon.json` lifecycle, `DaemonClient`, CoView page.
- `src/ntdrive/mcp/server.py`, `src/ntdrive/cli/main.py`, `src/ntdrive/sdk/__init__.py`:
  generated front doors.
- `tests/conftest.py`: `FakeVmrun`, `FakeTransport`, `FakeKdProcess` and the `service` fixture.
  Live testing against a real VM is manual and described in `README.md`.

## When you change a tool

1. Edit the parameter model and handler in `src/ntdrive/core/tools/<group>.py`.
2. Add or update a test that uses the fakes. `tests/test_registry.py` lists every tool name and
   `tests/test_faces.py` checks that MCP, HTTP, CLI and SDK all expose it.
3. Update `PRD.md` section 7 (the tool tables) and `SKILL.md` if the procedure for agents changes.
4. Restart the daemon (`uv run ntdrive daemon restart`) before trying it live.

## Things that bit us in live testing

- `vmrun` cannot create or delete a memory snapshot of a running encrypted VM. The adapter tags
  that error `reason=encrypted_live_snapshot` and the snapshot tools offer `allow_suspend`.
- Right after a suspend `vmrun` may briefly report the vmx as unreadable, and a delete that worked
  can then report "does not exist". The suspend path checks the snapshot list, not the op result.
- KDNET needs an inbound firewall allow for `kd.exe`, and Windows often has a leftover Block rule
  that wins. The serial pipe transport avoids all of that and is the default recommendation.
- Win32-OpenSSH resolves `C:/x` relative to the home directory over SFTP. Paths must be `/C:/x`.
- `Add-WindowsCapability` for OpenSSH fails on some Insider builds. The zip install works.
- PSReadLine redraws the input line on every keystroke and floods terminal reads. The terminal
  unloads it at session start.
- The daemon runs detached, without a console. Any child started without `CREATE_NO_WINDOW`
  gets a console window of its own, so the desktop flashed an empty window on every vmrun call.
  kd.exe keeps a console of its own (CREATE_NO_WINDOW gives it one, just without a window)
  because break-in attaches to that console to send CTRL_BREAK. `tests/test_kd.py` checks that
  delivery from a detached parent, so keep it green when touching `spawn_kd`.
