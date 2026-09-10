"""Adversarial tests for the workspace path boundary.

Every case here is an escape attempt. If any of them starts passing, untrusted
code can read or write outside its workspace, so these are the tests that must
never be weakened to make something else convenient.
"""

import os
from pathlib import Path

import pytest

from redstone.workspace.paths import (
    PathSecurityError,
    is_within,
    relative_to_workspace,
    resolve,
)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspaces" / "ws-abc123" / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "App.tsx").write_text("export default function App() {}", encoding="utf-8")
    (root / "package.json").write_text("{}", encoding="utf-8")

    # A sibling that must remain unreachable, and a secret outside the tree.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SHOULD-NEVER-BE-READ", encoding="utf-8")
    (tmp_path / "workspaces" / "ws-other").mkdir(parents=True)
    (tmp_path / "workspaces" / "ws-other" / "other.txt").write_text("other", encoding="utf-8")

    return root


# ------------------------------------------------------------------ accepted

@pytest.mark.parametrize(
    "path,expected",
    [
        ("package.json", "package.json"),
        ("src/App.tsx", "src/App.tsx"),
        ("./src/App.tsx", "src/App.tsx"),
        ("src//App.tsx", "src/App.tsx"),
        ("src/./App.tsx", "src/App.tsx"),
        ("  src/App.tsx  ", "src/App.tsx"),
        ("does/not/exist/yet.ts", "does/not/exist/yet.ts"),
        ("a.b.c.txt", "a.b.c.txt"),
        ("файл.txt", "файл.txt"),
        ("ഫയൽ.tsx", "ഫയൽ.tsx"),
    ],
)
def test_legitimate_paths_resolve(workspace, path, expected):
    result = resolve(workspace, path)

    assert result.relative == expected
    assert is_within(result.absolute, workspace)


def test_backslashes_are_normalised(workspace):
    """Windows-style separators must be understood, not silently mishandled."""
    assert resolve(workspace, "src\\App.tsx").relative == "src/App.tsx"


def test_resolved_path_is_absolute_and_inside(workspace):
    result = resolve(workspace, "src/App.tsx")

    assert result.absolute.is_absolute()
    assert result.absolute.read_text(encoding="utf-8").startswith("export default")


# ----------------------------------------------------------- parent traversal

@pytest.mark.parametrize(
    "path",
    [
        "..",
        "../",
        "../secret.txt",
        "../../outside/secret.txt",
        "../../../etc/passwd",
        "../../../../etc/shadow",
        "../../../../../../../../etc/passwd",
        "src/../../outside/secret.txt",
        "src/../../../etc/passwd",
        "./../../outside/secret.txt",
        "..\\..\\outside\\secret.txt",
        "..\\../outside/secret.txt",
        "src/subdir/../../../outside/secret.txt",
        "../ws-other/other.txt",
    ],
)
def test_parent_traversal_is_rejected(workspace, path):
    with pytest.raises(PathSecurityError):
        resolve(workspace, path)


def test_traversal_that_lands_back_inside_is_still_rejected(workspace):
    """'src/../package.json' stays inside, but '..' is refused on principle.

    Collapsing '..' is where traversal bugs live, because our normalisation and
    the OS's can disagree. A project path never needs it.
    """
    with pytest.raises(PathSecurityError):
        resolve(workspace, "src/../package.json")


# --------------------------------------------------------------- absolute

@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/",
        "/home/user/.ssh/id_rsa",
        "C:\\Windows\\System32\\config\\SAM",
        "C:/Windows/System32",
        "c:\\windows",
        "D:\\data\\file.txt",
        "\\\\server\\share\\file.txt",
        "//server/share/file.txt",
        "\\\\?\\C:\\Windows",
    ],
)
def test_absolute_drive_and_unc_paths_are_rejected(workspace, path):
    with pytest.raises(PathSecurityError):
        resolve(workspace, path)


def test_absolute_path_to_inside_the_workspace_is_still_rejected(workspace):
    """Even a correct absolute path is refused; callers pass relative paths."""
    with pytest.raises(PathSecurityError):
        resolve(workspace, str(workspace / "package.json"))


# ------------------------------------------------------------- byte tricks

@pytest.mark.parametrize(
    "path",
    [
        "file\x00.txt",
        "\x00",
        "src/\x00/App.tsx",
        "safe.txt\x00../../../etc/passwd",
    ],
)
def test_null_bytes_are_rejected(workspace, path):
    """NUL truncates paths in C-level calls, so the opened file can differ."""
    with pytest.raises(PathSecurityError):
        resolve(workspace, path)


@pytest.mark.parametrize("path", ["file\n.txt", "file\r.txt", "a\tb.txt", "\x1b[31m.txt"])
def test_control_characters_are_rejected(workspace, path):
    with pytest.raises(PathSecurityError):
        resolve(workspace, path)


@pytest.mark.parametrize(
    "path",
    [
        "%2e%2e/%2e%2e/etc/passwd",
        "%2E%2E/secret",
        "src%2fApp.tsx",
        "src%5c..%5csecret",
        "file%00.txt",
    ],
)
def test_percent_encoded_separators_are_rejected(workspace, path):
    """We never decode paths, but a layer below us might. Refusing is cheaper."""
    with pytest.raises(PathSecurityError):
        resolve(workspace, path)


# ------------------------------------------------------------------ symlinks

def _link_dir(link: Path, target: Path) -> bool:
    """Create a directory link at `link` pointing at `target`.

    Prefers a real symlink. On Windows that needs administrator rights or
    Developer Mode, so it falls back to a directory junction, which needs
    neither and which os.path.realpath resolves identically. Without this
    fallback the symlink-escape tests silently skip on Windows, leaving the
    most important defence in this module unverified.
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass

    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return True
    except Exception:
        return False


def test_symlink_escaping_the_workspace_is_rejected(workspace, tmp_path):
    """The authoritative check runs after links are resolved."""
    if not _link_dir(workspace / "escape", tmp_path / "outside"):
        pytest.skip("cannot create directory links in this environment")

    # Sanity: the link really does reach the secret, so the test has teeth.
    assert (workspace / "escape" / "secret.txt").read_text(encoding="utf-8") == "SHOULD-NEVER-BE-READ"

    with pytest.raises(PathSecurityError):
        resolve(workspace, "escape/secret.txt")


def test_nested_link_escape_is_rejected(workspace, tmp_path):
    hop = tmp_path / "hop"
    if not _link_dir(hop, tmp_path / "outside"):
        pytest.skip("cannot create directory links in this environment")
    if not _link_dir(workspace / "nested", hop):
        pytest.skip("cannot create directory links in this environment")

    with pytest.raises(PathSecurityError):
        resolve(workspace, "nested/secret.txt")


def test_link_that_stays_inside_is_allowed(workspace):
    if not _link_dir(workspace / "alias", workspace / "src"):
        pytest.skip("cannot create directory links in this environment")

    assert is_within(resolve(workspace, "alias/App.tsx").absolute, workspace)


def test_is_within_follows_links(workspace, tmp_path):
    if not _link_dir(workspace / "escape", tmp_path / "outside"):
        pytest.skip("cannot create directory links in this environment")

    assert not is_within(workspace / "escape", workspace)


def test_writing_through_an_escaping_link_is_blocked(workspace, tmp_path):
    """The end-to-end consequence: no write lands outside the workspace."""
    if not _link_dir(workspace / "escape", tmp_path / "outside"):
        pytest.skip("cannot create directory links in this environment")

    with pytest.raises(PathSecurityError):
        resolve(workspace, "escape/planted.txt")

    assert not (tmp_path / "outside" / "planted.txt").exists()


# --------------------------------------------------------- reserved / limits

@pytest.mark.parametrize(
    "path", ["NUL", "nul", "CON", "con.txt", "PRN", "AUX", "COM1", "LPT1", "src/NUL"]
)
def test_windows_device_names_are_rejected(workspace, path):
    """Opening these writes to a device, not a file."""
    with pytest.raises(PathSecurityError):
        resolve(workspace, path)


def test_overlong_path_is_rejected(workspace):
    with pytest.raises(PathSecurityError):
        resolve(workspace, "a/" * 800 + "file.txt")


def test_overlong_component_is_rejected(workspace):
    with pytest.raises(PathSecurityError):
        resolve(workspace, "x" * 300 + ".txt")


@pytest.mark.parametrize("path", ["", "   ", ".", "./", "/"])
def test_empty_or_rootlike_paths_are_rejected(workspace, path):
    with pytest.raises(PathSecurityError):
        resolve(workspace, path)


@pytest.mark.parametrize("bad", [None, 42, b"bytes", Path("x"), ["a"]])
def test_non_string_input_is_rejected(workspace, bad):
    with pytest.raises(PathSecurityError):
        resolve(workspace, bad)


# ------------------------------------------------------- cross-workspace

def test_one_workspace_cannot_reach_another(workspace, tmp_path):
    other = tmp_path / "workspaces" / "ws-other"

    for attempt in ("../ws-other/other.txt", "../../workspaces/ws-other/other.txt"):
        with pytest.raises(PathSecurityError):
            resolve(workspace, attempt)

    assert not is_within(other / "other.txt", workspace)


# ---------------------------------------------------------- error hygiene

def test_error_messages_do_not_disclose_host_paths(workspace):
    """A rejection must not hand back the server's filesystem layout."""
    for attempt in ("../../../etc/passwd", "/etc/passwd", "C:\\Windows"):
        with pytest.raises(PathSecurityError) as caught:
            resolve(workspace, attempt)

        message = str(caught.value)
        assert str(workspace) not in message
        assert "etc" not in message.lower() or "escapes" in message.lower()
        assert os.sep + os.sep not in message


# ------------------------------------------------------ relative_to_workspace

def test_relative_to_workspace_reports_portable_paths(workspace):
    assert relative_to_workspace(workspace / "src" / "App.tsx", workspace) == "src/App.tsx"
    assert relative_to_workspace(workspace, workspace) == "."


def test_relative_to_workspace_rejects_outside(workspace, tmp_path):
    with pytest.raises(PathSecurityError):
        relative_to_workspace(tmp_path / "outside" / "secret.txt", workspace)


def test_relative_output_never_contains_backslashes(workspace):
    assert "\\" not in resolve(workspace, "src\\App.tsx").relative
