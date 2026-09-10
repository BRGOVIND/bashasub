"""Workspace, file operations, limits and isolation."""


import pytest

from redstone.config import Limits
from redstone.workspace.files import (
    FileOperationError,
    FileTooLargeError,
    NotFoundError,
    ProjectTooLargeError,
    create_file,
    delete_file,
    is_secret_path,
    list_directory,
    list_files,
    project_size,
    read_file,
    rename_file,
    search_files,
    write_file,
)
from redstone.workspace.manager import WorkspaceError, WorkspaceManager
from redstone.workspace.paths import PathSecurityError


@pytest.fixture
def manager(tmp_path):
    return WorkspaceManager(tmp_path / "workspaces")


@pytest.fixture
def project(manager):
    workspace = manager.create("ws_test123")
    root = workspace.project_root
    (root / "src").mkdir()
    (root / "src" / "App.tsx").write_text("export default function App() {}\n", encoding="utf-8")
    (root / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
    return root


# ------------------------------------------------------------------ lifecycle

def test_create_and_get_workspace(manager):
    workspace = manager.create("ws_abc123")

    assert workspace.exists()
    assert workspace.project_root.is_dir()
    assert workspace.internal_root.is_dir()
    assert manager.get("ws_abc123").id == "ws_abc123"


def test_internal_state_is_a_sibling_of_the_project(manager):
    """Snapshots must not live where the agent can reach them."""
    workspace = manager.create("ws_abc123")

    assert workspace.internal_root.parent == workspace.root
    assert workspace.internal_root.parent != workspace.project_root
    with pytest.raises(PathSecurityError):
        from redstone.workspace.paths import resolve
        resolve(workspace.project_root, "../.redstone/snapshots")


def test_duplicate_workspace_is_rejected(manager):
    manager.create("ws_abc123")
    with pytest.raises(WorkspaceError):
        manager.create("ws_abc123")


def test_missing_workspace_raises(manager):
    with pytest.raises(WorkspaceError):
        manager.get("ws_missing")
    assert not manager.exists("ws_missing")


def test_destroy_removes_everything(manager):
    workspace = manager.create("ws_abc123")
    (workspace.project_root / "a.txt").write_text("x", encoding="utf-8")

    manager.destroy("ws_abc123")

    assert not workspace.root.exists()
    manager.destroy("ws_abc123")     # idempotent


@pytest.mark.parametrize(
    "bad_id",
    ["..", "../escape", "/etc", "a/b", "a\\b", "", "x", "ws id", "ws;id",
     "C:\\win", "\x00", "ws" + "x" * 200],
)
def test_malicious_workspace_ids_are_rejected(manager, bad_id):
    """Ids reach the filesystem, so they are a traversal vector."""
    with pytest.raises(WorkspaceError):
        manager.path_for(bad_id)


def test_list_ids(manager):
    manager.create("ws_one")
    manager.create("ws_two")

    assert manager.list_ids() == ("ws_one", "ws_two")


# ------------------------------------------------------------------ reading

def test_list_files(project):
    paths = [entry.path for entry in list_files(project)]

    assert paths == ["package.json", "src/App.tsx"]
    assert all(not entry.is_directory for entry in list_files(project))


def test_list_files_skips_node_modules(project):
    heavy = project / "node_modules" / "react"
    heavy.mkdir(parents=True)
    (heavy / "index.js").write_text("x", encoding="utf-8")

    assert "node_modules/react/index.js" not in [e.path for e in list_files(project)]


def test_list_directory(project):
    entries = list_directory(project, ".")
    names = [entry.path for entry in entries]

    assert "src" in names and "package.json" in names
    assert next(e for e in entries if e.path == "src").is_directory


def test_list_directory_rejects_escape(project):
    with pytest.raises(PathSecurityError):
        list_directory(project, "../..")


def test_read_file(project):
    assert read_file(project, "src/App.tsx").startswith("export default")


def test_read_missing_file(project):
    with pytest.raises(NotFoundError):
        read_file(project, "nope.txt")


def test_read_directory_is_an_error(project):
    with pytest.raises(FileOperationError):
        read_file(project, "src")


def test_read_rejects_oversized_file(project):
    (project / "big.txt").write_text("x" * 5000, encoding="utf-8")

    with pytest.raises(FileTooLargeError):
        read_file(project, "big.txt", Limits(max_read_size=1000))


def test_read_rejects_binary(project):
    (project / "logo.bin").write_bytes(b"\xff\xfe\x00\x01binary")

    with pytest.raises(FileOperationError):
        read_file(project, "logo.bin")


# ------------------------------------------------------------------ writing

def test_write_creates_parent_directories(project):
    entry = write_file(project, "src/components/Nav.tsx", "export const Nav = () => null\n")

    assert entry.path == "src/components/Nav.tsx"
    assert (project / "src" / "components" / "Nav.tsx").exists()


def test_write_overwrites(project):
    write_file(project, "package.json", '{"name":"changed"}\n')

    assert "changed" in read_file(project, "package.json")


def test_write_normalises_newlines(project):
    """Identical content must hash identically on every platform."""
    write_file(project, "a.txt", "one\r\ntwo\r\n")

    assert (project / "a.txt").read_bytes() == b"one\ntwo\n"


def test_write_rejects_oversized_content(project):
    with pytest.raises(FileTooLargeError):
        write_file(project, "big.txt", "x" * 2000, Limits(max_write_size=1000))


def test_write_rejects_non_string(project):
    with pytest.raises(FileOperationError):
        write_file(project, "a.txt", b"bytes")


def test_write_enforces_project_size_limit(project):
    with pytest.raises(ProjectTooLargeError):
        write_file(project, "big.txt", "x" * 900, Limits(max_project_size=500, max_write_size=10_000))


def test_write_enforces_file_count_limit(project):
    with pytest.raises(ProjectTooLargeError):
        write_file(project, "third.txt", "x", Limits(max_files=2))


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", "C:\\x.txt", "a/../../b"])
def test_write_cannot_escape(project, path):
    with pytest.raises(PathSecurityError):
        write_file(project, path, "pwned")


def test_create_file_refuses_existing(project):
    with pytest.raises(FileOperationError):
        create_file(project, "package.json", "{}")


def test_create_file_writes_new(project):
    create_file(project, "src/new.ts", "export const x = 1\n")

    assert read_file(project, "src/new.ts").startswith("export")


# ----------------------------------------------------------- delete / rename

def test_delete_file(project):
    assert delete_file(project, "package.json") == "package.json"
    assert not (project / "package.json").exists()


def test_delete_directory_recursively(project):
    delete_file(project, "src")

    assert not (project / "src").exists()


def test_delete_missing(project):
    with pytest.raises(NotFoundError):
        delete_file(project, "nope.txt")


@pytest.mark.parametrize("path", ["../../outside", "/etc/passwd", ".."])
def test_delete_cannot_escape(project, path):
    with pytest.raises(PathSecurityError):
        delete_file(project, path)


def test_rename_file(project):
    assert rename_file(project, "src/App.tsx", "src/Main.tsx") == "src/Main.tsx"
    assert (project / "src" / "Main.tsx").exists()
    assert not (project / "src" / "App.tsx").exists()


def test_rename_refuses_existing_destination(project):
    with pytest.raises(FileOperationError):
        rename_file(project, "src/App.tsx", "package.json")


@pytest.mark.parametrize(
    "source,destination",
    [("src/App.tsx", "../escaped.tsx"), ("../../secret", "a.txt"),
     ("src/App.tsx", "/tmp/escaped.tsx")],
)
def test_rename_cannot_escape_either_end(project, source, destination):
    with pytest.raises(PathSecurityError):
        rename_file(project, source, destination)


# ------------------------------------------------------------------ search

def test_search_finds_matches(project):
    hits = search_files(project, "export default")

    assert len(hits) == 1
    assert hits[0].path == "src/App.tsx" and hits[0].line_number == 1


def test_search_is_case_insensitive(project):
    assert search_files(project, "EXPORT DEFAULT")


def test_search_skips_secret_files(project):
    (project / ".env").write_text("AI_API_KEY=super-secret-value\n", encoding="utf-8")

    hits = search_files(project, "super-secret-value")

    assert hits == ()


def test_search_rejects_empty_query(project):
    with pytest.raises(FileOperationError):
        search_files(project, "  ")


def test_search_respects_max_results(project):
    write_file(project, "many.txt", "\n".join(["needle"] * 50))

    assert len(search_files(project, "needle", max_results=5)) == 5


# ------------------------------------------------------------------ secrets

@pytest.mark.parametrize(
    "path,secret",
    [
        (".env", True), (".env.production", True), ("src/.env.local", True),
        ("key.pem", True), ("id_rsa", True), ("credentials.json", True),
        (".npmrc", True), ("server.key", True),
        ("src/App.tsx", False), ("package.json", False), ("environment.ts", False),
    ],
)
def test_secret_path_detection(path, secret):
    assert is_secret_path(path) is secret


# ------------------------------------------------------------------ isolation

def test_one_workspace_cannot_read_another(manager):
    a = manager.create("ws_aaa111")
    b = manager.create("ws_bbb222")
    (b.project_root / "secret.txt").write_text("B ONLY", encoding="utf-8")

    for attempt in ("../../ws_bbb222/project/secret.txt", "../ws_bbb222/project/secret.txt"):
        with pytest.raises(PathSecurityError):
            read_file(a.project_root, attempt)

    with pytest.raises(PathSecurityError):
        manager.assert_isolated("ws_aaa111", b.project_root / "secret.txt")


def test_project_size(project):
    total, count = project_size(project)

    assert count == 2 and total > 0
