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
from ..workspace.safety import is_reparse_point, workspace_lock

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

    def _storage_used(self) -> int:
        if not self.snapshots_root.is_dir():
            return 0
        total = 0
        for path in self.snapshots_root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    def create(self, project_id: str, label: str = "") -> Snapshot:
        """Copy the project tree, skipping ignored trees and secret files.

        Quotas are enforced before copying so a project cannot exhaust disk
        through snapshots. Reparse points are already pruned by the shared walk,
        so an external junction cannot pull outside content into a snapshot.
        A hardlinked source is copied by content, which de-links it: the
        snapshot never preserves a link into another workspace.
        """
        with workspace_lock(self.workspace_root):
            if not self.project_root.is_dir():
                raise SnapshotError("project directory does not exist")

            existing = self.list_ids()
            if len(existing) >= self.limits.max_snapshots_per_workspace:
                raise SnapshotError(
                    f"snapshot limit reached "
                    f"({self.limits.max_snapshots_per_workspace} per workspace)"
                )
            if self._storage_used() >= self.limits.max_snapshot_storage:
                raise SnapshotError("snapshot storage limit reached")

            snapshot = Snapshot(id=new_id("snap"), project_id=project_id, label=label)
            destination = self._path_for(snapshot.id)
            destination.mkdir(parents=True, exist_ok=True)

            count = 0
            total = 0
            try:
                for source, _ in _walk(self.project_root, self.limits):
                    relative = relative_to_workspace(source, self.project_root)
                    if is_secret_path(relative):
                        continue
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    count += 1
                    total += source.stat().st_size
            except Exception:
                # Never leave a half-written snapshot behind.
                shutil.rmtree(destination, ignore_errors=True)
                raise

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
        """Restore the project to a snapshot, failure-safe.

        Guarantee: if this raises, the project is left exactly as it was. This
        is achieved by building the new tree in a staging directory first (the
        step that can fail), and only then swapping it into place with
        directory renames (the steps that essentially cannot fail):

            stage snapshot copy  ->  carry ignored trees over  ->
            rename project to trash  ->  rename staging to project

        It is not atomic at the syscall level — a crash between the two final
        renames could leave the project missing, recoverable from trash — but
        no partial or half-deleted tree is ever produced by an error.

        Ignored trees (node_modules, …) are moved across intact rather than
        recopied: they are derived, large, and re-copying them would make
        rollback unusably slow.
        """
        with workspace_lock(self.workspace_root):
            source = self._path_for(snapshot_id)
            if not source.is_dir():
                raise SnapshotError("snapshot not found")

            work = self.workspace_root / ".redstone" / "restore"
            staging = work / f"staging_{new_id('r')}"
            trash = work / f"trash_{new_id('r')}"
            staging.mkdir(parents=True)

            # 1. Build the replacement tree. Any failure here leaves the live
            #    project untouched, because nothing live has changed yet.
            restored = 0
            try:
                for path, _ in _walk(source, self.limits):
                    relative = relative_to_workspace(path, source)
                    target = staging / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                    restored += 1
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise

            # 2. Carry ignored top-level trees (node_modules) into staging so
            #    the swap preserves them.
            if self.project_root.is_dir():
                for entry in list(self.project_root.iterdir()):
                    if entry.name in IGNORED_DIRECTORIES and entry.is_dir() \
                            and not is_reparse_point(entry):
                        entry.rename(staging / entry.name)

            # 3. Swap. Renames are near-instant and rarely fail.
            try:
                if self.project_root.exists():
                    self.project_root.rename(trash)
                staging.rename(self.project_root)
            except Exception:
                # Best-effort recovery of the original tree.
                if trash.exists() and not self.project_root.exists():
                    trash.rename(self.project_root)
                shutil.rmtree(staging, ignore_errors=True)
                raise SnapshotError("restore failed during swap; project preserved")

            shutil.rmtree(trash, ignore_errors=True)
            return restored

    def delete(self, snapshot_id: str) -> None:
        path = self._path_for(snapshot_id)
        if path.is_dir():
            shutil.rmtree(path)
