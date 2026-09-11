# CLAUDE.md

Claude Code reads this file first. The guidance for every coding agent in this repository, Claude
included, lives in `AGENTS.md`: what ntdrive is, the hard rules (English and ASCII only, plain
writing, one tool definition for three front doors, secrets never in results or logs, no features
outside `PRD.md`), the commands to run, where things live and what to update when a tool changes.
Read `AGENTS.md` and follow it.

`SKILL.md` is for an agent that *uses* the tools to drive a VM, not for changing this code.

## Claude Code specifics

- `.mcp.json` registers the `ntdrive` MCP server as the installed `ntdrive-mcp` command, with no
  path. Install it once with `uv tool install -e .` (`scripts/setup-host.ps1` does this), which
  makes `ntdrive`, `ntdrive-mcp` and `ntdrived` run this checkout's code from any directory.
  Allow its tools with the permission rule `mcp__ntdrive__*`. The wait tools long-poll, so the
  MCP tool-call timeout must be above the daemon cap (600 s by default). After a dependency
  change run `uv tool install -e . --reinstall` with Claude Code closed, because its MCP server
  holds `ntdrive-mcp.exe`. While it is open, run the checks with `uv run --no-sync`.
- After editing daemon-side code run `uv run ntdrive daemon restart`, or the running daemon keeps
  serving the old code.
- Before committing, run `uv run ruff format`, `uv run ruff check`, `uv run mypy` and
  `uv run pytest -q`. All four must pass. Never stage `vms.yaml`: it holds real credentials.
