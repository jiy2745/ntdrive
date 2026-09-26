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
- A **new tool, or a changed parameter schema, is invisible to an MCP client that already
  connected**: it cached the tool list when its session started, and a healthy connector cannot be
  re-dialled ("only a failed server can be reconnected"). A restart of the daemon is not enough, so
  when shipping a tool for another session to use, say that it lands in their *next* session. Behaviour
  changes inside an existing tool do take effect at once, because those live in the daemon.
- **`SKILL.md` text is NOT subject to that**: any session can re-read it from disk with a file tool
  and get the current version. So the delivery gap is specifically tool schemas and parameters, not
  documentation.
- **But a documented warning still gets missed.** A peer session twice reported traps that were
  already in its own checkout (the WinRE plus KDNET NMI and the kd_detach transport error, both
  added in 1794fb6, an ancestor of the commit it was on), having cost it guests. The cause turned
  out to be a search that could not match: `grep -E "WinRE\|boottore"` looks for a literal pipe,
  because `\|` is alternation in BRE but an escaped literal in ERE. So do not treat committing
  guidance as delivering it. Message the sessions that need a guidance change, and prefer putting
  anything that can destroy a guest into a tool description or a tool result, which the caller
  reads at the moment it acts rather than having to go looking.
- Before restarting the daemon, check `ntdrive --json sys state` for a VM whose `kd.state` is not
  `detached`: a restart kills kd.exe, and over KDNET re-attaching costs a guest reboot. `daemon
  stop`/`restart` refuse in that case unless given `--force`.
- Before committing run the two commands under Commands in `AGENTS.md`. Never stage `vms.yaml`:
  it holds real credentials.
