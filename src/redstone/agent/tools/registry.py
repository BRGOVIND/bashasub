"""The controlled tool registry.

The agent may only call tools registered here — a fixed Python dict built at
import time. There is no dynamic import, no `getattr` on a user-supplied name,
and no path from a tool name string to arbitrary code: an unknown name is a
structured `TOOL_NOT_FOUND` result, never an attempt to resolve one.

Every call passes through the same three gates before a handler ever runs:
name lookup, argument-schema validation, and a wall-clock timeout. Handlers
receive already-validated arguments and a `ToolContext` pinned to one
workspace; they raise the existing workspace/security exceptions on failure,
which this module turns into a structured `ToolResult` rather than letting
propagate — a tool failure is part of the conversation, not a crash.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from ...workspace.paths import PathSecurityError
from ...workspace.files import FileOperationError
from ...workspace.safety import UnsafeFileError
from ...changes.snapshots import SnapshotError
from ..errors import AgentErrorCode
from ..models import ToolResult
from .specs import ArgSpec, ToolContext, ToolSpec

__all__ = [
    "ArgSpec",
    "ToolSpec",
    "ToolContext",
    "ToolRegistry",
    "default_registry",
]

# Exceptions raised by the existing secure layers. Their messages are already
# designed to be safe to surface (SECURITY.md: "Rejection messages name the
# broken rule, never the resolved path"), so they become the ToolResult
# message directly rather than being replaced with something generic.
_SAFE_LAYER_EXCEPTIONS = (PathSecurityError, FileOperationError, UnsafeFileError, SnapshotError)

# One shared pool: tool calls are short-lived local filesystem operations, and
# a small bounded pool is enough to enforce a wall-clock timeout per call
# without spinning up a thread per invocation.
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="redstone-tool")


def _validate_arguments(spec: ToolSpec, arguments: dict, max_arg_size: int) -> str | None:
    """Return an error message, or None if arguments are acceptable."""
    if not isinstance(arguments, dict):
        return "arguments must be a JSON object"

    unknown = set(arguments) - set(spec.args)
    if unknown:
        return f"unknown argument(s): {', '.join(sorted(unknown))}"

    for name, arg_spec in spec.args.items():
        present = name in arguments
        if arg_spec.required and not present:
            return f"missing required argument '{name}'"
        if not present:
            continue

        value = arguments[name]
        if arg_spec.type is str:
            if not isinstance(value, str):
                return f"argument '{name}' must be a string"
            limit = arg_spec.max_length or max_arg_size
            if len(value.encode("utf-8")) > limit:
                return f"argument '{name}' exceeds the {limit} byte limit"
        elif arg_spec.type is bool:
            if not isinstance(value, bool):
                return f"argument '{name}' must be a boolean"
        elif arg_spec.type is int:
            if isinstance(value, bool) or not isinstance(value, int):
                return f"argument '{name}' must be an integer"
        else:  # pragma: no cover - defensive, all current specs use str/bool/int
            if not isinstance(value, arg_spec.type):
                return f"argument '{name}' has the wrong type"

    return None


def _truncate_output(output: dict, limit: int) -> tuple[dict, bool]:
    """Cap the serialized size of a tool's output so one result cannot consume
    the whole agent context budget."""
    serialized = json.dumps(output, ensure_ascii=False, default=str)
    if len(serialized.encode("utf-8")) <= limit:
        return output, False

    # Truncate the largest string field, which is normally file content.
    result = dict(output)
    string_fields = [(k, v) for k, v in result.items() if isinstance(v, str)]
    if string_fields:
        key, value = max(string_fields, key=lambda kv: len(kv[1]))
        # Leave room for the rest of the payload plus the truncation marker.
        overhead = len(serialized.encode("utf-8")) - len(value.encode("utf-8"))
        budget = max(0, limit - overhead - 64)
        result[key] = value.encode("utf-8")[:budget].decode("utf-8", "ignore") + "…[truncated]"
    return result, True


class ToolRegistry:
    def __init__(self, specs: dict[str, ToolSpec]) -> None:
        self._specs = specs

    def catalog(self) -> list[dict]:
        return [spec.to_catalog_entry() for spec in self._specs.values()]

    def has(self, name: str) -> bool:
        return name in self._specs

    def kind_of(self, name: str) -> str:
        """The tool's kind ("read"/"mutate"/"validate"), or "read" if unknown.

        Used by the loop to decide which state a tool call moves the task
        into; defaulting to "read" for an unknown name is safe because an
        unknown tool never actually runs (`call` rejects it before this would
        matter for anything but display).
        """
        spec = self._specs.get(name)
        return spec.kind if spec else "read"

    def call(self, call_id: str, name: str, arguments: dict, context: ToolContext) -> ToolResult:
        # Limits come from context.limits, not a separate parameter: a handler
        # enforces context.limits internally (e.g. write_file's size checks),
        # so a caller passing a different Limits here would validate arguments
        # against one policy and execute against another. One source of truth.
        limits = context.limits
        spec = self._specs.get(name)
        if spec is None:
            return ToolResult(
                call_id=call_id, tool=name, ok=False,
                error_code=AgentErrorCode.TOOL_NOT_FOUND.value,
                output={"message": f"Unknown tool '{name}'."},
            )

        problem = _validate_arguments(spec, arguments, limits.max_tool_argument_size)
        if problem:
            return ToolResult(
                call_id=call_id, tool=name, ok=False,
                error_code=AgentErrorCode.TOOL_INVALID_ARGUMENTS.value,
                output={"message": problem},
            )

        future = _EXECUTOR.submit(spec.handler, context, arguments)
        try:
            output = future.result(timeout=spec.timeout_seconds)
        except FutureTimeoutError:
            future.cancel()
            return ToolResult(
                call_id=call_id, tool=name, ok=False,
                error_code="AGENT_TOOL_TIMEOUT",
                output={"message": f"'{name}' did not finish within {spec.timeout_seconds}s."},
            )
        except _SAFE_LAYER_EXCEPTIONS as exc:
            return ToolResult(
                call_id=call_id, tool=name, ok=False,
                error_code="AGENT_TOOL_REJECTED",
                output={"message": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001 - converted to a safe structured result
            return ToolResult(
                call_id=call_id, tool=name, ok=False,
                error_code=AgentErrorCode.INTERNAL_ERROR.value,
                output={"message": f"'{name}' failed unexpectedly ({type(exc).__name__})."},
            )

        capped, truncated = _truncate_output(output, limits.max_tool_output)
        return ToolResult(call_id=call_id, tool=name, ok=True, output=capped, truncated=truncated)


def default_registry() -> ToolRegistry:
    from . import files as file_tools
    from . import validation as validation_tools

    specs: dict[str, ToolSpec] = {}
    for spec in file_tools.TOOL_SPECS:
        specs[spec.name] = spec
    for spec in validation_tools.TOOL_SPECS:
        specs[spec.name] = spec
    return ToolRegistry(specs)
