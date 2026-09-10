"""Snapshots, changesets and rollback.

A changeset is computed by comparing the filesystem before and after an
operation. It is never built from a model's account of what it changed: the
whole point is to report what actually happened, including changes the model
did not mention and changes it claimed but did not make.

Snapshots are a filesystem copy of the project tree into the workspace's
internal directory, which the agent cannot reach. Copying is chosen over a
git-backed implementation because it has no external dependency, no repository
state to corrupt, and restores are a directory swap. Projects are bounded by
``max_project_size``, so the cost is bounded too.

Secrets are excluded from snapshots, so a rollback can never resurrect a
credential and a snapshot can never become a place one hides.

Standard library only.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ..config import Limits
from ..domain.models import ChangeKind, ChangeSet, FileChange, Snapshot, new_id
from ..workspace.files import (
    IGNORED_DIRECTORIES,
    file_hash,
    is_secret_path,
    _walk,
)
from ..workspace.paths import relative_to_workspace

__all__ = ["SnapshotError", "FileState", "capture_state", "diff_states",
           "SnapshotStore"]


class SnapshotError(Exception):
    """A snapshot could not be created or restored."""


@dataclass(frozen=True, slots=True)
class FileState:
    """A hash of every project file, used as the basis for a changeset."""

    hashes: dict[str, str]
    sizes: dict[str, int]

    @property
    def paths(self) -> set[str]:
        return set(self.hashes)

    @property
    def total_bytes(self) -> int:
        return sum(self.sizes.values())


def capture_state(project_root: Path, limits: Limits | None = None) -> FileState:
    """Hash every project file. This is the ground truth a changeset compares."""
    limits = limits or Limits()
    root = Path(project_root)
    hashes: dict[str, str] = {}
    sizes: dict[str, int] = {}

    for path, _ in _walk(root, limits):
        try:
            relative = relative_to_workspace(path, root)
        except Exception:
            continue
        if is_secret_path(relative):
            continue
        try:
            hashes[relative] = file_hash(path)
            sizes[relative] = path.stat().st_size
        except OSError:
            continue

    return FileState(hashes=hashes, sizes=sizes)


def _count_lines(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return 0


def diff_states(
    before: FileState,
    after: FileState,
    project_root: Path,
    *,
    project_id: str,
    task_id: str | None = None,
    reason: str = "",
) -> ChangeSet:
    """Build a changeset from two captured states.

    Line counts are approximate: they report the size of the resulting file
    rather than a computed line-level diff. They are labelled as additions or
    deletions accordingly, and a caller wanting an exact diff should render one
    from the snapshot.
    """
    root = Path(project_root)
    changes: list[FileChange] = []

    for path in sorted(after.paths - before.paths):
        changes.append(
            FileChange(
                path=path,
                kind=ChangeKind.CREATED,
                additions=_count_lines(root / path),
                after_hash=after.hashes.get(path),
            )
        )

    for path in sorted(before.paths - after.paths):
        changes.append(
            FileChange(
                path=path,
                kind=ChangeKind.DELETED,
                before_hash=before.hashes.get(path),
            )
        )

    for path in sorted(before.paths & after.paths):
        if before.hashes[path] == after.hashes[path]:
            continue
        changes.append(
            FileChange(
                path=path,
                kind=ChangeKind.MODIFIED,
                additions=_count_lines(root / path),
                before_hash=before.hashes[path],
                after_hash=after.hashes[path],
            )
        )

    return ChangeSet(
        id=new_id("chg"),
        project_id=project_id,
        task_id=task_id,
        changes=tuple(changes),
        reason=reason,
    )


class SnapshotStore:
    """Snapshots for one workspace, stored where the agent cannot reach them."""

    def __init__(self, workspace_root: Path, project_root: Path,
                 limits: Limits | None = None) -> None:
        self.workspace_root = Path(workspace_root)
        self.project_root = Path(project_root)
        self.limits = limits or Limits()
        self.snapshots_root = self.workspace_root / ".redstone" / "snapshots"

    def _path_for(self, snapshot_id: str) -> Path:
        # Snapshot ids are generated here, but validated anyway: an id arriving
        # from an API path parameter must not be able to traverse.
        if not snapshot_id.replace("_", "").replace("-", "").isalnum():
            raise SnapshotError("invalid snapshot id")
        return self.snapshots_root / snapshot_id

    def create(self, project_id: str, label: str = "") -> Snapshot:
        """Copy the project tree, skipping ignored trees and secret files."""
        if not self.project_root.is_dir():
            raise SnapshotError("project directory does not exist")

        snapshot = Snapshot(id=new_id("snap"), project_id=project_id, label=label)
        destination = self._path_for(snapshot.id)
        destination.mkdir(parents=True, exist_ok=True)

        count = 0
        total = 0
        for source, _ in _walk(self.project_root, self.limits):
            relative = relative_to_workspace(source, self.project_root)
            if is_secret_path(relative):
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            count += 1
            total += source.stat().st_size

        return Snapshot(
            id=snapshot.id,
            project_id=project_id,
            label=label,
            file_count=count,
            total_bytes=total,
            created_at=snapshot.created_at,
        )

    def exists(self, snapshot_id: str) -> bool:
        return self._path_for(snapshot_id).is_dir()

    def list_ids(self) -> tuple[str, ...]:
        if not self.snapshots_root.is_dir():
            return ()
        return tuple(sorted(p.name for p in self.snapshots_root.iterdir() if p.is_dir()))

    def restore(self, snapshot_id: str) -> int:
        """Replace the project tree with the snapshot's contents.

        Ignored trees such as node_modules are left alone: they are derived
        from package.json rather than authored, and re-copying them would make
        rollback unusably slow. Everything the snapshot tracks is restored
        exactly, and tracked files absent from the snapshot are removed.
        """
        source = self._path_for(snapshot_id)
        if not source.is_dir():
            raise SnapshotError("snapshot not found")

        # Remove current tracked files, preserving ignored trees.
        for entry in list(self.project_root.iterdir()):
            if entry.name in IGNORED_DIRECTORIES:
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()

        restored = 0
        for path, _ in _walk(source, self.limits):
            relative = relative_to_workspace(path, source)
            target = self.project_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            restored += 1

        return restored

    def delete(self, snapshot_id: str) -> None:
        path = self._path_for(snapshot_id)
        if path.is_dir():
            shutil.rmtree(path)
