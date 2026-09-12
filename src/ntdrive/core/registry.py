"""The single source of truth for tool definitions.

A tool is declared once with the `tool` decorator: a name like `kd_exec`, a pydantic parameter
model and an async handler. The daemon HTTP API, the MCP server, the CLI and the SDK are all
generated from this registry, which is what keeps the three front doors identical.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from ntdrive.core.service import NtDriveService

Handler = Callable[["NtDriveService", Any], Awaitable[dict[str, Any]]]
# What a call can do to the world. It becomes the MCP annotations (readOnlyHint, destructiveHint)
# and the Effect column of the README tool table: read changes nothing, additive adds or starts
# something, destructive can discard state (a snapshot, a running guest, a file, a shell command).
Effect = Literal["read", "additive", "destructive"]
# Group order of the README tool table, the order of the PRD section 7 tables.
TABLE_GROUP_ORDER = ("vm", "snap", "kd", "term", "con", "file", "sys")


@dataclass
class ToolSpec:
    """Everything the front doors need to know about one tool."""

    name: str
    description: str
    params: type[BaseModel]
    handler: Handler
    effect: Effect
    positional: tuple[str, ...] = ()
    destructive: bool = False  # needs confirm=true, a subset of effect == "destructive"
    idempotent: bool = False  # the same call again changes nothing more (MCP idempotentHint)
    long_poll: bool = False
    touches_guest: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def group(self) -> str:
        """`kd` for `kd_exec`."""
        return self.name.split("_", 1)[0]

    @property
    def verb(self) -> str:
        """`exec` for `kd_exec`, `wait_event` for `kd_wait_event`."""
        return self.name.split("_", 1)[1] if "_" in self.name else self.name

    def input_schema(self) -> dict[str, Any]:
        """JSON schema of the parameter model, as MCP expects it."""
        schema = self.params.model_json_schema()
        schema.pop("title", None)
        return schema

    def summary(self) -> dict[str, Any]:
        """Wire representation used by `GET /api/tools` and the CLI help."""
        return {
            "name": self.name,
            "group": self.group,
            "verb": self.verb,
            "description": self.description,
            "positional": list(self.positional),
            "effect": self.effect,
            "destructive": self.destructive,
            "idempotent": self.idempotent,
            "long_poll": self.long_poll,
            "input_schema": self.input_schema(),
        }


class ToolRegistry:
    """Ordered collection of ToolSpec objects."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        """Add a tool. Registering the same name twice is a programming error."""
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name} registered twice")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        """Look up by name."""
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self) -> Any:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        """Tool names in registration order."""
        return list(self._tools)

    def markdown_table(self) -> str:
        """The README tool table, one row per tool, groups in the order of PRD section 7."""
        rows = ["| Tool | Effect | What it does |", "|---|---|---|"]
        groups = self.groups()
        order = [g for g in TABLE_GROUP_ORDER if g in groups]
        order += [g for g in groups if g not in order]
        for group in order:
            for spec in groups[group]:
                text = spec.description.replace("|", "/")
                rows.append(f"| `{spec.name}` | {spec.effect} | {text} |")
        return "\n".join(rows) + "\n"

    def groups(self) -> dict[str, list[ToolSpec]]:
        """Tools bucketed by group, in registration order."""
        out: dict[str, list[ToolSpec]] = {}
        for spec in self._tools.values():
            out.setdefault(spec.group, []).append(spec)
        return out


registry = ToolRegistry()


def tool(
    name: str,
    description: str,
    params: type[BaseModel],
    *,
    effect: Effect,
    positional: tuple[str, ...] = ("vm",),
    destructive: bool = False,
    idempotent: bool = False,
    long_poll: bool = False,
    touches_guest: bool = False,
    tags: tuple[str, ...] = (),
) -> Callable[[Handler], Handler]:
    """Register `handler(service, params)` as a tool."""

    def decorate(handler: Handler) -> Handler:
        registry.register(
            ToolSpec(
                name=name,
                description=description,
                params=params,
                handler=handler,
                effect=effect,
                positional=positional,
                destructive=destructive,
                idempotent=idempotent,
                long_poll=long_poll,
                touches_guest=touches_guest,
                tags=tags,
            )
        )
        return handler

    return decorate


def load_builtin_tools() -> ToolRegistry:
    """Import every tool module so their decorators run, then return the registry."""
    import ntdrive.core.tools  # noqa: F401

    return registry
