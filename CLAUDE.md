# CLAUDE.md

Claude Code reads this file first. The guidance for every coding agent in this repository, Claude
included, lives in `AGENTS.md`: what ntdrive is, the hard rules (English and ASCII only, plain
writing, one tool definition for three front doors, secrets never in results or logs, no features
outside `PRD.md`), what to read for a change, the commands to run, where things live and what to
update when a tool changes. Read `AGENTS.md` and follow it.

`SKILL.md` is for an agent that *uses* the tools to drive a VM, not for changing this code.

## Claude Code specifics

- `.mcp.json` registers the `ntdrive` MCP server as the installed `ntdrive-mcp` command, with no
  path. Install it once with `uv tool install -e .` (`scripts/setup-host.ps1` does this), which
  makes `ntdrive`, `ntdrive-mcp` and `ntdrived` run this checkout's code from any directory.
  Allow its tools with the permission rule `mcp__ntdrive__*`. The wait tools long-poll, so the
  MCP tool-call timeout must be above the daemon cap (600 s by default).
- While Claude Code is open its MCP server holds `ntdrive-mcp.exe`, so `uv sync`, `uv tool
  install` and a `uv run` after a dependency change fail with "access is denied". Run everything
  as `uv run --no-sync ...` and do a reinstall (`uv tool install -e . --reinstall`) with Claude
  Code closed and the daemon stopped (`ntdrive daemon stop`). A plain code edit needs no
  reinstall, the install is editable. The installed command runs whichever checkout the editable
  install points at: `uv tool dir` then the `_editable_impl_ntdrive.pth` names it.
- `uv run --no-sync ntdrive daemon restart` after editing daemon-side code, or the running daemon
  keeps serving the old code. The open MCP connection survives the restart: the client re-reads
  `daemon.json` on its next call. Live kd and terminal sessions do not survive it.
- Before committing run the two commands under Commands in `AGENTS.md`. Never stage `vms.yaml`:
  it holds real credentials.
