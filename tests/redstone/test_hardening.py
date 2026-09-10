"""Regression tests for the Phase 2A.1 hardening pass.

Every test here reproduces a vulnerability the Phase 2A audit proved, using the
real filesystem (real hardlinks, real Windows junctions, real threads). No
mocks. OS-specific cases skip with a reason where the feature is unavailable.
"""

import os
import threading
from pathlib import Path

import pytest

from redstone.config import Limits
from redstone.changes.snapshots import SnapshotError, SnapshotStore
from redstone.workspace import files as F
from redstone.workspace.manager import WorkspaceManager
from redstone.workspace.paths import PathSecurityError, resolve
from redstone.workspace.safety import UnsafeFileError

_WINDOWS = os.name == "nt"


@pytest.fixture
def env(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("VICTIM", encoding="utf-8")
    ws = WorkspaceManager(tmp_path / "workspaces").create("ws_h1")
    (ws.project_root / "a.txt").write_text("inside\n", encoding="utf-8")
    return tmp_path, outside, ws


def _junction(link: Path, target: Path) -> bool:
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


# -------------------------------------------------------------- hardlinks

def test_hardlink_read_escape_is_blocked(env):
    _, outside, ws = env
    try:
        os.link(outside / "victim.txt", ws.project_root / "hard.txt")
    except OSError:
        pytest.skip("hardlinks not supported here")

    with pytest.raises((UnsafeFileError, F.FileOperationError)):
        F.read_file(ws.project_root, "hard.txt")


def test_hardlink_write_escape_is_blocked_and_outside_unchanged(env):
    _, outside, ws = env
    try:
        os.link(outside / "victim.txt", ws.project_root / "hard.txt")
    except OSError:
        pytest.skip("hardlinks not supported here")

    with pytest.raises((UnsafeFileError, F.FileOperationError)):
        F.write_file(ws.project_root, "hard.txt", "PWNED\n")

    assert (outside / "victim.txt").read_text(encoding="utf-8") == "VICTIM"


def test_hardlink_rename_is_blocked(env):
    _, outside, ws = env
    try:
        os.link(outside / "victim.txt", ws.project_root / "hard.txt")
    except OSError:
        pytest.skip("hardlinks not supported here")

    with pytest.raises((UnsafeFileError, F.FileOperationError)):
        F.rename_file(ws.project_root, "hard.txt", "moved.txt")


def test_hardlink_in_nested_directory_is_blocked(env):
    _, outside, ws = env
    (ws.project_root / "src").mkdir()
    try:
        os.link(outside / "victim.txt", ws.project_root / "src" / "hard.txt")
    except OSError:
        pytest.skip("hardlinks not supported here")

    with pytest.raises((UnsafeFileError, F.FileOperationError)):
        F.read_file(ws.project_root, "src/hard.txt")


def test_deleting_a_hardlink_leaves_the_outside_file(env):
    """Deleting a hardlink removes only that name; the target survives."""
    _, outside, ws = env
    try:
        os.link(outside / "victim.txt", ws.project_root / "hard.txt")
    except OSError:
        pytest.skip("hardlinks not supported here")

    F.delete_file(ws.project_root, "hard.txt")

    assert (outside / "victim.txt").read_text(encoding="utf-8") == "VICTIM"
    assert not (ws.project_root / "hard.txt").exists()


def test_snapshot_delinks_a_hardlink(env):
    """A snapshot copies content, so it never preserves a link out of the tree."""
    _, outside, ws = env
    try:
        os.link(outside / "victim.txt", ws.project_root / "hard.txt")
    except OSError:
        pytest.skip("hardlinks not supported here")

    store = SnapshotStore(ws.root, ws.project_root)
    snap = store.create("prj")
    copied = store.snapshots_root / snap.id / "hard.txt"

    # The copy, if made, is independent: editing it must not touch outside.
    if copied.exists():
        assert os.stat(copied).st_nlink == 1


# --------------------------------------------------------- alternate streams

@pytest.mark.parametrize(
    "path",
    ["a.txt:hidden", "src/a.txt:hidden", "a.txt:stream:name", "A.TXT:Secret"],
)
def test_ntfs_alternate_data_streams_are_rejected(env, path):
    _, _, ws = env
    with pytest.raises(PathSecurityError):
        resolve(ws.project_root, path)


# ------------------------------------------------------ windows aliases

@pytest.mark.parametrize(
    "path",
    [
        "id_rsa.",            # trailing dot on final component
        "file.txt.",
        "sub./a.txt",         # trailing dot on an interior component
        "sub /a.txt",         # trailing space on an interior component
    ],
)
def test_trailing_dot_or_space_components_are_rejected(env, path):
    _, _, ws = env
    with pytest.raises(PathSecurityError):
        resolve(ws.project_root, path)


def test_trailing_space_on_the_whole_path_is_stripped_safely(env):
    """A trailing space on the last component is removed by the outer strip,
    resolving to the intended file, and the secret filter still catches it."""
    _, _, ws = env
    (ws.project_root / "notes.txt").write_text("ok\n", encoding="utf-8")

    assert resolve(ws.project_root, "notes.txt ").relative == "notes.txt"

    (ws.project_root / "id_rsa").write_text("PRIVATE", encoding="utf-8")
    with pytest.raises(F.FileOperationError):
        F.read_file(ws.project_root, "id_rsa ")


# ------------------------------------------------------------- secret reads

@pytest.mark.parametrize(
    "name,content",
    [
        (".env", "AI_API_KEY=real"),
        (".env.production", "TOKEN=real"),
        ("credentials.json", "AWS=real"),
        ("id_rsa", "PRIVATE"),
        ("server.key", "PRIVATE"),
        ("app.pem", "PRIVATE"),
    ],
)
def test_secret_files_cannot_be_read(env, name, content):
    _, _, ws = env
    (ws.project_root / name).write_text(content, encoding="utf-8")

    with pytest.raises(F.FileOperationError):
        F.read_file(ws.project_root, name)


def test_secret_read_is_rejected_not_redacted(env):
    """The safe behaviour is refusal; content must never be returned at all."""
    _, _, ws = env
    (ws.project_root / ".env").write_text("AI_API_KEY=abc", encoding="utf-8")

    try:
        result = F.read_file(ws.project_root, ".env")
    except F.FileOperationError:
        return
    assert False, f"secret was returned instead of refused: {result!r}"


def test_secret_8_3_alias_is_blocked(env):
    _, _, ws = env
    if not _WINDOWS:
        pytest.skip("8.3 aliases are Windows-only")
    import ctypes

    (ws.project_root / "credentials.json").write_text("AWS=real", encoding="utf-8")
    buffer = ctypes.create_unicode_buffer(512)
    ctypes.windll.kernel32.GetShortPathNameW(
        str(ws.project_root / "credentials.json"), buffer, 512
    )
    short = Path(buffer.value).name
    if short.lower() == "credentials.json":
        pytest.skip("8.3 short names disabled on this volume")

    with pytest.raises(F.FileOperationError):
        F.read_file(ws.project_root, short)


def test_case_variant_secret_is_blocked(env):
    _, _, ws = env
    (ws.project_root / ".env").write_text("AI_API_KEY=abc", encoding="utf-8")

    # On case-insensitive filesystems the alias opens the same file.
    with pytest.raises(F.FileOperationError):
        F.read_file(ws.project_root, ".ENV")


# --------------------------------------------------------------- junctions

def test_project_size_does_not_walk_a_junction(env):
    _, outside, ws = env
    (outside / "big1.txt").write_text("x" * 1000, encoding="utf-8")
    if not _junction(ws.project_root / "j", outside):
        pytest.skip("cannot create directory links here")

    _, count = F.project_size(ws.project_root)

    assert count == 1  # only a.txt; the junction contents are not counted


def test_list_files_does_not_follow_a_junction(env):
    _, outside, ws = env
    if not _junction(ws.project_root / "j", outside):
        pytest.skip("cannot create directory links here")

    paths = [e.path for e in F.list_files(ws.project_root)]

    assert not any("victim" in p for p in paths)


def test_snapshot_ignores_a_junction(env):
    _, outside, ws = env
    if not _junction(ws.project_root / "j", outside):
        pytest.skip("cannot create directory links here")

    store = SnapshotStore(ws.root, ws.project_root)
    snap = store.create("prj")  # must not raise

    assert not (store.snapshots_root / snap.id / "j").exists()


# ------------------------------------------------------------- rollback

@pytest.fixture
def store(tmp_path):
    ws = WorkspaceManager(tmp_path / "workspaces").create("ws_r1")
    for rel in ("package.json", "src/App.tsx", "src/build/gen.ts",
                "src/components/dist/out.ts", "src/foo/.cache/c.ts"):
        p = ws.project_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"// {rel}\n", encoding="utf-8")
    return ws, SnapshotStore(ws.root, ws.project_root)


@pytest.mark.parametrize(
    "rel",
    ["src/build/gen.ts", "src/components/dist/out.ts", "src/foo/.cache/c.ts"],
)
def test_rollback_preserves_nested_source_that_looks_generated(store, rel):
    ws, snap_store = store
    snap = snap_store.create("prj")

    ws.project_root.joinpath("src/App.tsx").write_text("changed\n", encoding="utf-8")
    snap_store.restore(snap.id)

    assert (ws.project_root / rel).read_text(encoding="utf-8") == f"// {rel}\n"


def test_root_level_node_modules_is_preserved_on_rollback(store):
    ws, snap_store = store
    modules = ws.project_root / "node_modules" / "react"
    modules.mkdir(parents=True)
    (modules / "index.js").write_text("module.exports={}\n", encoding="utf-8")

    snap = snap_store.create("prj")
    ws.project_root.joinpath("src/App.tsx").write_text("changed\n", encoding="utf-8")
    snap_store.restore(snap.id)

    assert (modules / "index.js").exists()


# ------------------------------------------------------- atomic restore

def test_failed_restore_leaves_the_project_intact(tmp_path, monkeypatch):
    ws = WorkspaceManager(tmp_path / "workspaces").create("ws_a1")
    for rel in ("package.json", "src/App.tsx"):
        p = ws.project_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"// {rel}\n", encoding="utf-8")
    store = SnapshotStore(ws.root, ws.project_root)
    snap = store.create("prj")
    ws.project_root.joinpath("src/App.tsx").write_text("edited\n", encoding="utf-8")

    # Force the staging copy to fail partway through.
    import redstone.changes.snapshots as snaps
    original = snaps.shutil.copy2
    calls = {"n": 0}

    def flaky(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("simulated disk failure")
        return original(src, dst, *a, **k)

    monkeypatch.setattr(snaps.shutil, "copy2", flaky)

    with pytest.raises(OSError):
        store.restore(snap.id)

    # Original working tree survives, with the edit still in place.
    assert ws.project_root.joinpath("package.json").exists()
    assert ws.project_root.joinpath("src/App.tsx").read_text(encoding="utf-8") == "edited\n"


# --------------------------------------------------------- snapshot quota

def test_snapshot_count_quota_is_enforced(tmp_path):
    ws = WorkspaceManager(tmp_path / "workspaces").create("ws_q1")
    (ws.project_root / "a.txt").write_text("x\n", encoding="utf-8")
    store = SnapshotStore(ws.root, ws.project_root, Limits(max_snapshots_per_workspace=3))

    for _ in range(3):
        store.create("prj")
    with pytest.raises(SnapshotError):
        store.create("prj")


def test_snapshot_storage_quota_is_enforced(tmp_path):
    ws = WorkspaceManager(tmp_path / "workspaces").create("ws_q2")
    (ws.project_root / "a.txt").write_text("x" * 5000, encoding="utf-8")
    store = SnapshotStore(ws.root, ws.project_root, Limits(max_snapshot_storage=1000))

    store.create("prj")  # first is allowed; the check is before-the-fact
    with pytest.raises(SnapshotError):
        store.create("prj")


# ------------------------------------------------------------- locking

def test_concurrent_writes_respect_the_project_limit(tmp_path):
    ws = WorkspaceManager(tmp_path / "workspaces").create("ws_l1")
    limits = Limits(max_project_size=12_000, max_write_size=10_000, max_file_size=10_000)
    barrier = threading.Barrier(16)

    def writer(i):
        barrier.wait()
        try:
            F.write_file(ws.project_root, f"f{i}.txt", "x" * 2_000, limits)
        except Exception:
            pass

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total, _ = F.project_size(ws.project_root)
    assert total <= 12_000


def test_two_workspaces_are_independent(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspaces")
    a = manager.create("ws_ia")
    b = manager.create("ws_ib")

    def fill(ws):
        for i in range(20):
            F.write_file(ws.project_root, f"f{i}.txt", "y\n")

    ta = threading.Thread(target=fill, args=(a,))
    tb = threading.Thread(target=fill, args=(b,))
    ta.start(); tb.start(); ta.join(); tb.join()

    assert len(F.list_files(a.project_root)) == 20
    assert len(F.list_files(b.project_root)) == 20


# --------------------------------------------------------- size accounting

def test_max_file_size_is_enforced_independently(tmp_path):
    ws = WorkspaceManager(tmp_path / "workspaces").create("ws_s1")
    # write limit generous, file-size limit tight
    limits = Limits(max_write_size=1_000_000, max_file_size=1000)

    with pytest.raises(F.FileTooLargeError):
        F.write_file(ws.project_root, "big.txt", "x" * 2000, limits)
