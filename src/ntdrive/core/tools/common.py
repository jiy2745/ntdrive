"""Parameter models shared by several tools."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class NoParams(BaseModel):
    """Tool without arguments."""

    model_config = ConfigDict(extra="forbid")


class VmParams(BaseModel):
    """Tools that address one VM."""

    model_config = ConfigDict(extra="forbid")

    vm: str = Field(description="VM name as registered in vms.yaml")


class ConfirmMixin(BaseModel):
    """Adds the confirm flag used by the policy gate."""

    confirm: bool = Field(
        default=False, description="Set true to acknowledge a destructive operation"
    )
