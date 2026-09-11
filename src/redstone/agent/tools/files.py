"""File tool handlers.

Every handler is a thin wrapper over ``redstone.workspace.files``. Path
resolution, secret filtering, hardlink/junction/ADS/alias rejection, size
limits and workspace locking all happen inside those functions — nothing here
duplicates them. A handler's only jobs are: call the real function, and shape
the result into a small dict of workspace-relative facts (never a host path,
never file content the caller did not ask for).

Security exceptions (PathSecurityError, FileOperationError and its subclasses,
UnsafeFileError) are allowed to propagate out of these handlers — the registry
catches them and turns them into a structured, non-fatal ToolResult. A handler
must never catch and swallow one.
"""

from __future__ import annotations

from ...workspace import files as wf
from .specs import ArgSpec, ToolContext, ToolSpec

__all__ = ["TOOL_SPECS"]


def _list_files(context: ToolContext, arguments: dict) -> dict:
    entries = wf.list_files(context.workspace.project_root, context.limits)
    return {
        "files": [
            {"path": e.path, "is_directory": e.is_directory, "size": e.size}
            for e in entries
        ],
        "count": len(entries),
    }


def _read_file(context: ToolContext, arguments: dict) -> dict:
    path = arguments["path"]
    content = wf.read_file(context.workspace.project_root, path, context.limits)
    return {"path": path, "content": content}


def _search_files(context: ToolContext, arguments: dict) -> dict:
    query = arguments["query"]
    # Bounded below the raw tool's own cap, so search cannot alone fill the
    # agent's context budget even if the raw layer's limit is raised later.
    max_results = min(context.limits.max_search_results, 100)
    hits = wf.search_files(
        context.workspace.project_root, query,
        max_results=max_results, limits=context.limits,
    )
    return {
        "query": query,
        "matches": [
            {"path": h.path, "line": h.line_number, "text": h.line} for h in hits
        ],
        "count": len(hits),
    }


def _write_file(context: ToolContext, arguments: dict) -> dict:
    path = arguments["path"]
    content = arguments["content"]
    entry = wf.write_file(context.workspace.project_root, path, content, context.limits)
    return {"path": entry.path, "size": entry.size}


def _create_file(context: ToolContext, arguments: dict) -> dict:
    path = arguments["path"]
    content = arguments.get("content", "")
    entry = wf.create_file(context.workspace.project_root, path, content, context.limits)
    return {"path": entry.path, "size": entry.size}


def _delete_file(context: ToolContext, arguments: dict) -> dict:
    path = arguments["path"]
    deleted = wf.delete_file(context.workspace.project_root, path)
    return {"path": deleted, "deleted": True}


def _rename_file(context: ToolContext, arguments: dict) -> dict:
    source = arguments["source"]
    destination = arguments["destination"]
    new_path = wf.rename_file(context.workspace.project_root, source, destination)
    return {"source": source, "destination": new_path}


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="list_files",
        description="List every file in the project, as project-relative paths.",
        args={},
        handler=_list_files,
        timeout_seconds=10.0,
    ),
    ToolSpec(
        name="read_file",
        description="Read a text file. Argument: path (project-relative).",
        args={"path": ArgSpec(type=str)},
        handler=_read_file,
        timeout_seconds=10.0,
    ),
    ToolSpec(
        name="search_files",
        description="Search project text files for a literal substring. "
                     "Arguments: query.",
        args={"query": ArgSpec(type=str, max_length=500)},
        handler=_search_files,
        timeout_seconds=15.0,
    ),
    ToolSpec(
        name="write_file",
        description="Create or overwrite a text file. Arguments: path, content.",
        args={"path": ArgSpec(type=str), "content": ArgSpec(type=str)},
        handler=_write_file,
        timeout_seconds=10.0,
        kind="mutate",
    ),
    ToolSpec(
        name="create_file",
        description="Create a new text file that must not already exist. "
                     "Arguments: path, content (optional).",
        args={"path": ArgSpec(type=str), "content": ArgSpec(type=str, required=False)},
        handler=_create_file,
        timeout_seconds=10.0,
        kind="mutate",
    ),
    ToolSpec(
        name="delete_file",
        description="Delete a file or directory. Argument: path.",
        args={"path": ArgSpec(type=str)},
        handler=_delete_file,
        timeout_seconds=10.0,
        kind="mutate",
    ),
    ToolSpec(
        name="rename_file",
        description="Move/rename a file. Arguments: source, destination.",
        args={"source": ArgSpec(type=str), "destination": ArgSpec(type=str)},
        handler=_rename_file,
        timeout_seconds=10.0,
        kind="mutate",
    ),
)
