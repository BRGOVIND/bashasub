"""Agent tool tests, against the real workspace security layer.

No part of `redstone.workspace` is mocked here. Every tool call in this file
goes through the actual path resolver, the actual hardlink/reparse/secret
checks from Phase 2A.1, and the actual size limits. This is what proves the
agent is a *consumer* of that boundary rather than a second implementation of
it.
"""

import os

import pytest

from redstone.config import Limits
from redstone.agent.errors import AgentErrorCode
from redstone.agent.tools.registry import ToolContext, default_registry
from redstone.workspace.manager import WorkspaceManager


@pytest.fixture
def registry():
    return default_registry()


@pytest.fixture
def workspace(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspaces")
    ws = manager.create("ws_tools1")
    (ws.project_root / "src").mkdir()
    (ws.project_root / "src" / "App.tsx").write_text("export default function App() {}\n",
                                                       encoding="utf-8")
    (ws.project_root / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
    return ws


@pytest.fixture
def outside(tmp_path):
    directory = tmp_path / "outside"
    directory.mkdir()
    (directory / "secret.txt").write_text("SHOULD-NEVER-BE-READ", encoding="utf-8")
    return directory


@pytest.fixture
def ctx(workspace):
    return ToolContext(workspace=workspace, limits=Limits())


# ------------------------------------------------------------------ list_files

def test_list_files_returns_project_relative_paths_only(registry, ctx):
    result = registry.call("c1", "list_files", {}, ctx)

    assert result.ok
    paths = [f["path"] for f in result.output["files"]]
    assert set(paths) == {"src/App.tsx", "package.json"}
    for path in paths:
        assert not os.path.isabs(path)
        assert ":" not in path            # no "C:" drive prefix
        assert "\\" not in path           # always forward slashes
        assert str(ctx.workspace.project_root) not in path


# ------------------------------------------------------------------ read_file

def test_read_file_returns_real_content(registry, ctx):
    result = registry.call("c1", "read_file", {"path": "src/App.tsx"}, ctx)

    assert result.ok
    assert "export default" in result.output["content"]
    assert result.output["path"] == "src/App.tsx"


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "../outside/secret.txt",
        "/etc/passwd",
        "C:\\Windows\\System32",
        "..",
        "src/../../etc/passwd",
    ],
)
def test_read_file_traversal_is_rejected(registry, ctx, path):
    result = registry.call("c1", "read_file", {"path": path}, ctx)

    assert not result.ok
    assert result.error_code == "AGENT_TOOL_REJECTED"
    assert "SHOULD-NEVER-BE-READ" not in str(result.output)


@pytest.mark.parametrize(
    "name", [".env", ".env.production", "credentials.json", "id_rsa", "server.key", "app.pem"]
)
def test_read_file_secret_files_are_rejected(registry, ctx, name):
    (ctx.workspace.project_root / name).write_text("SECRET-VALUE-abc123", encoding="utf-8")

    result = registry.call("c1", "read_file", {"path": name}, ctx)

    assert not result.ok
    assert "SECRET-VALUE-abc123" not in str(result.output)
    assert "permitted" in result.output["message"].lower() or "secret" in result.output["message"].lower()


def test_read_file_ads_syntax_is_rejected(registry, ctx):
    result = registry.call("c1", "read_file", {"path": "package.json:hidden"}, ctx)
    assert not result.ok


def test_read_file_null_byte_is_rejected(registry, ctx):
    result = registry.call("c1", "read_file", {"path": "a.txt\x00.png"}, ctx)
    assert not result.ok


def test_read_file_missing_is_reported_not_raised(registry, ctx):
    result = registry.call("c1", "read_file", {"path": "does/not/exist.ts"}, ctx)
    assert not result.ok
    assert result.error_code == "AGENT_TOOL_REJECTED"


def test_read_file_hardlink_escape_is_rejected(registry, ctx, outside):
    try:
        os.link(outside / "secret.txt", ctx.workspace.project_root / "linked.txt")
    except OSError:
        pytest.skip("hardlinks not supported here")

    result = registry.call("c1", "read_file", {"path": "linked.txt"}, ctx)

    assert not result.ok
    assert "SHOULD-NEVER-BE-READ" not in str(result.output)


def test_read_file_oversized_is_rejected(registry, ctx):
    tight_limits = Limits(max_read_size=10)
    ctx.limits = tight_limits
    (ctx.workspace.project_root / "big.txt").write_text("x" * 1000, encoding="utf-8")

    result = registry.call("c1", "read_file", {"path": "big.txt"}, ctx)

    assert not result.ok


# --------------------------------------------------------------------- search

def test_search_finds_real_matches(registry, ctx):
    result = registry.call("c1", "search_files", {"query": "export default"}, ctx)

    assert result.ok
    assert result.output["count"] == 1
    assert result.output["matches"][0]["path"] == "src/App.tsx"


def test_search_skips_secret_files(registry, ctx):
    (ctx.workspace.project_root / ".env").write_text("AI_API_KEY=super-secret-token",
                                                       encoding="utf-8")

    result = registry.call("c1", "search_files", {"query": "super-secret-token"}, ctx)

    assert result.ok
    assert result.output["count"] == 0


def test_search_rejects_empty_query(registry, ctx):
    result = registry.call("c1", "search_files", {"query": ""}, ctx)
    assert not result.ok


def test_search_query_over_max_length_is_rejected(registry, ctx):
    result = registry.call("c1", "search_files", {"query": "x" * 10_000}, ctx)
    assert not result.ok
    assert result.error_code == AgentErrorCode.TOOL_INVALID_ARGUMENTS.value


def test_search_result_count_is_bounded(registry, ctx):
    lines = "\n".join(f"needle-{i}" for i in range(500))
    registry.call("c1", "write_file", {"path": "many.txt", "content": lines}, ctx)

    result = registry.call("c2", "search_files", {"query": "needle"}, ctx)

    assert result.ok
    assert result.output["count"] <= 50   # agent-facing cap (max_search_results)


# ---------------------------------------------------------------- write_file

def test_write_file_creates_real_file(registry, ctx):
    result = registry.call("c1", "write_file", {"path": "new.txt", "content": "hello"}, ctx)

    assert result.ok
    assert (ctx.workspace.project_root / "new.txt").read_text(encoding="utf-8") == "hello"


@pytest.mark.parametrize(
    "path",
    [".env", ".env.local", "id_rsa", "credentials.json", "server.key"],
)
def test_write_file_cannot_create_secret_files(registry, ctx, path):
    result = registry.call("c1", "write_file", {"path": path, "content": "AI_API_KEY=x"}, ctx)

    assert not result.ok
    assert not (ctx.workspace.project_root / path).exists()


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", "..\\..\\evil.txt"])
def test_write_file_cannot_escape_workspace(registry, ctx, path, outside):
    result = registry.call("c1", "write_file", {"path": path, "content": "pwned"}, ctx)

    assert not result.ok
    assert not (outside / "pwned.txt").exists()


def test_write_file_oversized_content_is_rejected(registry, ctx):
    ctx.limits = Limits(max_write_size=10, max_file_size=10)

    result = registry.call(
        "c1", "write_file", {"path": "big.txt", "content": "x" * 1000}, ctx
    )
    assert not result.ok


def test_write_file_wrong_argument_type_is_rejected(registry, ctx):
    result = registry.call("c1", "write_file", {"path": "a.txt", "content": 12345}, ctx)
    assert not result.ok
    assert result.error_code == AgentErrorCode.TOOL_INVALID_ARGUMENTS.value


def test_write_file_unknown_argument_is_rejected(registry, ctx):
    result = registry.call(
        "c1", "write_file",
        {"path": "a.txt", "content": "x", "sudo": True}, ctx)
    assert not result.ok
    assert result.error_code == AgentErrorCode.TOOL_INVALID_ARGUMENTS.value


# --------------------------------------------------------------- create_file

def test_create_file_refuses_existing(registry, ctx):
    result = registry.call(
        "c1", "create_file", {"path": "package.json", "content": "{}"}, ctx)
    assert not result.ok


def test_create_file_content_is_optional(registry, ctx):
    result = registry.call("c1", "create_file", {"path": "empty.txt"}, ctx)
    assert result.ok
    assert (ctx.workspace.project_root / "empty.txt").read_text(encoding="utf-8") == ""


# --------------------------------------------------------------- delete_file

def test_delete_file_removes_real_file(registry, ctx):
    result = registry.call("c1", "delete_file", {"path": "package.json"}, ctx)
    assert result.ok
    assert not (ctx.workspace.project_root / "package.json").exists()


def test_delete_file_cannot_escape_workspace(registry, ctx, outside):
    result = registry.call("c1", "delete_file", {"path": "../outside/secret.txt"}, ctx)
    assert not result.ok
    assert (outside / "secret.txt").exists()


def test_delete_file_missing_is_reported(registry, ctx):
    result = registry.call("c1", "delete_file", {"path": "nope.txt"}, ctx)
    assert not result.ok


# --------------------------------------------------------------- rename_file

def test_rename_file_moves_real_file(registry, ctx):
    result = registry.call(
        "c1", "rename_file", {"source": "src/App.tsx", "destination": "src/Main.tsx"}, ctx)
    assert result.ok
    assert (ctx.workspace.project_root / "src" / "Main.tsx").exists()
    assert not (ctx.workspace.project_root / "src" / "App.tsx").exists()


@pytest.mark.parametrize(
    "source,destination",
    [
        ("src/App.tsx", "../escaped.tsx"),
        ("../../secret", "a.txt"),
        ("src/App.tsx", "/tmp/escaped.tsx"),
    ],
)
def test_rename_file_cannot_escape_either_end(registry, ctx, source, destination):
    result = registry.call("c1", "rename_file", {"source": source, "destination": destination}, ctx)
    assert not result.ok


def test_rename_secret_file_is_rejected(registry, ctx):
    (ctx.workspace.project_root / ".env").write_text("AI_API_KEY=x", encoding="utf-8")

    result = registry.call(
        "c1", "rename_file", {"source": ".env", "destination": "not_a_secret.txt"}, ctx)

    assert not result.ok
    assert not (ctx.workspace.project_root / "not_a_secret.txt").exists()


# ------------------------------------------------------------ registry itself

def test_unknown_tool_is_structured_not_an_exception(registry, ctx):
    result = registry.call("c1", "run_shell_command", {"cmd": "rm -rf /"}, ctx)

    assert not result.ok
    assert result.error_code == AgentErrorCode.TOOL_NOT_FOUND.value


def test_no_shell_or_exec_tool_is_registered(registry):
    forbidden_names = {"run_command", "shell", "exec", "terminal", "npm_install",
                       "curl", "wget", "python_exec", "eval"}
    registered_names = {entry["name"] for entry in registry.catalog()}
    assert forbidden_names.isdisjoint(registered_names)


def test_catalog_exposes_only_name_and_description(registry):
    for entry in registry.catalog():
        assert set(entry) == {"name", "description"}


def test_missing_required_argument(registry, ctx):
    result = registry.call("c1", "read_file", {}, ctx)
    assert not result.ok
    assert result.error_code == AgentErrorCode.TOOL_INVALID_ARGUMENTS.value
