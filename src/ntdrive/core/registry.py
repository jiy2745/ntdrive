"""The single source of truth for tool definitions.

A tool is declared once with the `tool` decorator: a name like `kd_exec`, a pydantic parameter
model and an async handler. The daemon HTTP API, the MCP server, the CLI and the SDK are all
generated from this registry, which is what keeps the three front doors identical.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from ntdrive.core.service import NtDriveService

Handler = Callable[["NtDriveService", Any], Awaitable[dict[str, Any]]]


@dataclass
class ToolSpec:
    """Everything the front doors need to know about one tool."""

    name: str
    description: str
    params: type[BaseModel]
    handler: Handler
    positional: tuple[str, ...] = ()
    destructive: bool = False
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
            "destructive": self.destructive,
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
    positional: tuple[str, ...] = ("vm",),
    destructive: bool = False,
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
                positional=positional,
                destructive=destructive,
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
