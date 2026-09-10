"""Changesets, snapshots and rollback, plus the event bus."""

import pytest

from redstone.changes.snapshots import (
    SnapshotError,
    SnapshotStore,
    capture_state,
    diff_states,
)
from redstone.domain.models import ChangeKind, EventType
from redstone.events.bus import EventBus
from redstone.workspace.files import delete_file, rename_file, write_file
from redstone.workspace.manager import WorkspaceManager


@pytest.fixture
def workspace(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspaces")
    ws = manager.create("ws_changes1")
    write_file(ws.project_root, "src/App.tsx", "export default function App() {}\n")
    write_file(ws.project_root, "package.json", '{"name":"demo"}\n')
    return ws


@pytest.fixture
def store(workspace):
    return SnapshotStore(workspace.root, workspace.project_root)


# ---------------------------------------------------------------- changesets

def test_no_changes_produces_an_empty_changeset(workspace):
    before = capture_state(workspace.project_root)
    after = capture_state(workspace.project_root)

    changeset = diff_states(before, after, workspace.project_root, project_id="prj_1")

    assert changeset.is_empty
    assert changeset.changes == ()


def test_created_file_is_detected(workspace):
    before = capture_state(workspace.project_root)
    write_file(workspace.project_root, "src/Nav.tsx", "export const Nav = () => null\n")
    after = capture_state(workspace.project_root)

    changeset = diff_states(before, after, workspace.project_root, project_id="prj_1")

    created = changeset.of_kind(ChangeKind.CREATED)
    assert [c.path for c in created] == ["src/Nav.tsx"]
    assert created[0].after_hash and created[0].before_hash is None


def test_modified_file_is_detected_by_content_not_mtime(workspace):
    before = capture_state(workspace.project_root)
    write_file(workspace.project_root, "package.json", '{"name":"renamed"}\n')
    after = capture_state(workspace.project_root)

    changeset = diff_states(before, after, workspace.project_root, project_id="prj_1")
    modified = changeset.of_kind(ChangeKind.MODIFIED)

    assert [c.path for c in modified] == ["package.json"]
    assert modified[0].before_hash != modified[0].after_hash


def test_rewriting_identical_content_is_not_a_change(workspace):
    """Hashes, not timestamps. Touching a file is not modifying it."""
    before = capture_state(workspace.project_root)
    write_file(workspace.project_root, "package.json", '{"name":"demo"}\n')
    after = capture_state(workspace.project_root)

    assert diff_states(before, after, workspace.project_root, project_id="prj_1").is_empty


def test_deleted_file_is_detected(workspace):
    before = capture_state(workspace.project_root)
    delete_file(workspace.project_root, "package.json")
    after = capture_state(workspace.project_root)

    changeset = diff_states(before, after, workspace.project_root, project_id="prj_1")

    assert [c.path for c in changeset.of_kind(ChangeKind.DELETED)] == ["package.json"]


def test_rename_shows_as_delete_plus_create(workspace):
    before = capture_state(workspace.project_root)
    rename_file(workspace.project_root, "src/App.tsx", "src/Main.tsx")
    after = capture_state(workspace.project_root)

    changeset = diff_states(before, after, workspace.project_root, project_id="prj_1")
    paths = set(changeset.paths)

    assert paths == {"src/App.tsx", "src/Main.tsx"}


def test_changeset_reports_the_real_filesystem_not_a_claim(workspace):
    """A changeset is derived from disk, so unannounced edits still appear."""
    before = capture_state(workspace.project_root)
    write_file(workspace.project_root, "sneaky.txt", "written without being announced\n")
    after = capture_state(workspace.project_root)

    changeset = diff_states(
        before, after, workspace.project_root, project_id="prj_1", reason="unrelated"
    )

    assert "sneaky.txt" in changeset.paths


def test_secret_files_are_excluded_from_state(workspace):
    (workspace.project_root / ".env").write_text("AI_API_KEY=abc\n", encoding="utf-8")

    assert ".env" not in capture_state(workspace.project_root).paths


# ----------------------------------------------------------------- snapshots

def test_snapshot_records_real_counts(workspace, store):
    snapshot = store.create("prj_1", label="before edit")

    assert snapshot.file_count == 2
    assert snapshot.total_bytes > 0
    assert store.exists(snapshot.id)
    assert snapshot.id in store.list_ids()


def test_snapshot_lives_outside_the_agent_reachable_project(workspace, store):
    store.create("prj_1")

    assert store.snapshots_root.is_dir()
    assert not str(store.snapshots_root).startswith(str(workspace.project_root))


def test_snapshot_excludes_secrets(workspace, store):
    (workspace.project_root / ".env").write_text("AI_API_KEY=super-secret\n", encoding="utf-8")

    snapshot = store.create("prj_1")

    copied = list((store.snapshots_root / snapshot.id).rglob("*"))
    assert not any(p.name == ".env" for p in copied)
    assert not any("super-secret" in p.read_text(encoding="utf-8", errors="ignore")
                   for p in copied if p.is_file())


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", "x;y"])
def test_snapshot_ids_are_validated(store, bad):
    with pytest.raises(SnapshotError):
        store.restore(bad)


def test_restoring_a_missing_snapshot_raises(store):
    with pytest.raises(SnapshotError):
        store.restore("snap_doesnotexist")


# ------------------------------------------------------------------ rollback

def test_rollback_restores_modified_content(workspace, store):
    snapshot = store.create("prj_1")
    write_file(workspace.project_root, "src/App.tsx", "BROKEN\n")

    store.restore(snapshot.id)

    text = (workspace.project_root / "src" / "App.tsx").read_text(encoding="utf-8")
    assert text == "export default function App() {}\n"


def test_rollback_removes_files_created_after_the_snapshot(workspace, store):
    snapshot = store.create("prj_1")
    write_file(workspace.project_root, "src/Added.tsx", "export const A = 1\n")

    store.restore(snapshot.id)

    assert not (workspace.project_root / "src" / "Added.tsx").exists()


def test_rollback_restores_deleted_files(workspace, store):
    snapshot = store.create("prj_1")
    delete_file(workspace.project_root, "package.json")

    store.restore(snapshot.id)

    assert (workspace.project_root / "package.json").exists()


def test_rollback_returns_the_project_to_an_identical_state(workspace, store):
    """The strongest check: hashes before and after a round trip must match."""
    original = capture_state(workspace.project_root)
    snapshot = store.create("prj_1")

    write_file(workspace.project_root, "src/App.tsx", "changed\n")
    write_file(workspace.project_root, "extra.txt", "extra\n")
    delete_file(workspace.project_root, "package.json")

    store.restore(snapshot.id)

    assert capture_state(workspace.project_root).hashes == original.hashes


def test_rollback_preserves_node_modules(workspace, store):
    """Derived trees are not authored, and re-copying them makes rollback unusable."""
    modules = workspace.project_root / "node_modules" / "react"
    modules.mkdir(parents=True)
    (modules / "index.js").write_text("module.exports = {}\n", encoding="utf-8")

    snapshot = store.create("prj_1")
    write_file(workspace.project_root, "src/App.tsx", "changed\n")
    store.restore(snapshot.id)

    assert (modules / "index.js").exists()


def test_rollback_only_touches_its_own_project(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspaces")
    a = manager.create("ws_aaa")
    b = manager.create("ws_bbb")
    write_file(a.project_root, "a.txt", "A\n")
    write_file(b.project_root, "b.txt", "B ORIGINAL\n")

    store_a = SnapshotStore(a.root, a.project_root)
    snapshot = store_a.create("prj_a")
    write_file(a.project_root, "a.txt", "A CHANGED\n")
    store_a.restore(snapshot.id)

    assert (b.project_root / "b.txt").read_text(encoding="utf-8") == "B ORIGINAL\n"


def test_snapshot_delete(store):
    snapshot = store.create("prj_1")
    store.delete(snapshot.id)

    assert not store.exists(snapshot.id)


# --------------------------------------------------------------------- events

def test_publish_and_subscribe():
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)

    bus.publish(EventType.BUILD_STARTED, project_id="prj_1", payload={"step": "typecheck"})

    assert len(seen) == 1
    assert seen[0].type is EventType.BUILD_STARTED
    assert seen[0].payload == {"step": "typecheck"}


def test_unsubscribe():
    bus = EventBus()
    seen = []
    stop = bus.subscribe(seen.append)
    stop()

    bus.publish(EventType.BUILD_STARTED)

    assert seen == []


def test_a_broken_subscriber_does_not_stop_the_others():
    bus = EventBus()
    seen = []
    bus.subscribe(lambda event: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.subscribe(seen.append)

    bus.publish(EventType.AGENT_STARTED)

    assert len(seen) == 1


@pytest.mark.parametrize(
    "key", ["api_key", "API_KEY", "authorization", "token", "secret", "prompt", "content"]
)
def test_forbidden_payload_keys_are_stripped(key):
    """A careless publish must not stream a credential or a prompt to browsers."""
    bus = EventBus()

    event = bus.publish(EventType.AGENT_STARTED, payload={key: "sensitive-value", "ok": 1})

    assert key not in event.payload
    assert "sensitive-value" not in str(event.payload)
    assert event.payload["ok"] == 1


def test_long_payload_values_are_truncated():
    bus = EventBus()

    event = bus.publish(EventType.RUNTIME_ERROR, payload={"message": "x" * 5000})

    assert len(event.payload["message"]) <= 501


def test_history_is_filterable_and_bounded():
    bus = EventBus(history_size=3)
    for _ in range(5):
        bus.publish(EventType.FILE_CHANGED, project_id="prj_1")
    bus.publish(EventType.FILE_CHANGED, project_id="prj_2")

    assert len(bus.history()) == 3
    assert len(bus.history(project_id="prj_2")) == 1


def test_event_serialises_safely():
    bus = EventBus()
    event = bus.publish(EventType.PREVIEW_READY, project_id="prj_1", payload={"path": "/p/1"})

    data = event.to_dict()

    assert data["type"] == "preview.ready"
    assert data["payload"] == {"path": "/p/1"}
    assert isinstance(data["created_at"], str)
