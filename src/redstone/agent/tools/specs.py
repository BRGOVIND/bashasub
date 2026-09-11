"""Shared tool types.

Split out so `files.py` and `validation.py` can each define their `TOOL_SPECS`
without importing the registry (which imports both of them to build the
default registry), and without importing each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ...config import Limits
from ...workspace.manager import Workspace

__all__ = ["ArgSpec", "ToolSpec", "ToolContext"]


@dataclass(frozen=True, slots=True)
class ArgSpec:
    type: type
    required: bool = True
    max_length: int | None = None   # for str arguments


#: "read" (list/read/search — moves the task to INSPECTING), "mutate"
#: (write/create/delete/rename — moves to EDITING, triggers a lazy
#: snapshot-before-first-mutation), or "validate" (typecheck/lint/build —
#: moves to VALIDATING).
ToolKind = str


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    args: dict[str, ArgSpec]
    handler: Callable[["ToolContext", dict], dict]
    timeout_seconds: float = 10.0
    kind: ToolKind = "read"

    def to_catalog_entry(self) -> dict:
        return {"name": self.name, "description": self.description}


@dataclass(slots=True)
class ToolContext:
    """What a handler is given. Nothing here lets a handler address anything
    outside its own workspace: there is no field for another workspace id,
    and `workspace` was resolved by the service before the loop started."""

    workspace: Workspace
    limits: Limits
    validation_runner: object = field(default=None)
