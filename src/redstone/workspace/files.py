"""Controlled filesystem operations inside a workspace.

Every function here takes a workspace root and an untrusted relative path, and
routes it through :func:`redstone.workspace.paths.resolve` before touching
disk. No function in this module opens a path it was handed directly.

Limits are enforced before the write, not after, so an oversized file is never
briefly on disk. Results are structured and carry workspace-relative paths
only: an absolute host path must never escape into an API response or an AI
prompt.

Standard library only.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import Limits
from ..domain.models import FileEntry
from .paths import PathSecurityError, relative_to_workspace, resolve
from .safety import (
    UnsafeFileError,
    assert_safe_to_read,
    assert_safe_to_write,
    canonical_name,
    is_reparse_point,
    workspace_lock,
)

__all__ = [
    "FileOperationError",
    "FileTooLargeError",
    "ProjectTooLargeError",
    "NotFoundError",
    "SearchHit",
    "list_files",
    "list_directory",
    "read_file",
    "write_file",
    "create_file",
    "delete_file",
    "rename_file",
    "search_files",
    "project_size",
    "file_hash",
    "is_secret_path",
    "UnsafeFileError",
]

# Directories that are never listed, searched, snapshotted or sent to a model.
# node_modules and .git are excluded for size; the rest are excluded because
# they hold credentials.
IGNORED_DIRECTORIES = frozenset(
    {"node_modules", ".git", ".svn", "dist", "build", ".next", ".cache",
     "__pycache__", ".venv", "venv", ".redstone"}
)

# Never read into an AI prompt, never included in a snapshot. Matched on the
# file name, with content scanning handled separately: a filename check alone
# is not a secret detector.
SECRET_FILENAMES = frozenset(
    {".env", "credentials", "credentials.json", "secrets", "secrets.json",
     "id_rsa", "id_ed25519", ".npmrc", ".netrc"}
)
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
SECRET_PREFIXES = (".env.",)


class FileOperationError(Exception):
    """A filesystem operation failed for a reason safe to report."""


class FileTooLargeError(FileOperationError):
    pass


class ProjectTooLargeError(FileOperationError):
    pass


class NotFoundError(FileOperationError):
    pass


@dataclass(frozen=True, slots=True)
class SearchHit:
    path: str
    line_number: int
    line: str


def is_secret_path(relative_path: str) -> bool:
    """Whether a path looks like it holds credentials.

    Used to keep files out of AI prompts and snapshots. Deliberately generous:
    a false positive costs a file the model cannot read, a false negative can
    cost a credential.
    """
    name = relative_path.rsplit("/", 1)[-1].lower()
    return _is_secret_name(name)


def _is_secret_name(name: str) -> bool:
    # Trailing dot/space stripped because Windows opens "id_rsa." as "id_rsa".
    name = name.rstrip(" .").lower()
    if name in SECRET_FILENAMES:
        return True
    if name.endswith(SECRET_SUFFIXES):
        return True
    if any(name.startswith(prefix) for prefix in SECRET_PREFIXES):
        return True
    return False


def assert_not_secret(requested_relative: str, absolute: Path) -> None:
    """Refuse any operation on a credential file.

    Checks the requested name and the file's canonical on-disk name, so a
    Windows alias (trailing dot, or an 8.3 short name like ID_RSA~1) cannot be
    used to read a secret past the filename filter. Rejection, never redaction.
    """
    if is_secret_path(requested_relative):
        raise FileOperationError("access to secret files is not permitted")
    if _is_secret_name(canonical_name(absolute)):
        raise FileOperationError("access to secret files is not permitted")


def _modified(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def file_hash(path: Path) -> str:
    """SHA-256 of file contents, used to detect real change rather than claimed change."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _walk(root: Path, limits: Limits):
    """Yield (absolute_path, depth) for real files under root.

    Two rules that matter for security and correctness:

    * Reparse points (symlinks, Windows junctions) are never descended, so a
      junction planted in the project cannot make a walk enumerate, size, copy
      or delete an external tree. os.walk does not detect junctions on Windows,
      so they are pruned explicitly.
    * The ignore list (node_modules, build, dist, .git, …) applies **only at the
      project root**. Applying it at every depth silently dropped legitimate
      source folders such as src/build/ and src/components/dist/, which caused
      rollback to delete them. Nested occurrences are walked normally.
    """
    root = Path(root)
    root_depth = len(root.parts)

    for current, directories, filenames in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.parts) - root_depth

        if depth >= limits.max_directory_depth:
            directories[:] = []
            continue

        kept = []
        for name in directories:
            if depth == 0 and name in IGNORED_DIRECTORIES:
                continue
            if is_reparse_point(current_path / name):
                continue
            kept.append(name)
        directories[:] = kept

        for name in filenames:
            path = current_path / name
            if is_reparse_point(path):
                continue
            yield path, depth


def project_size(workspace_root: Path) -> tuple[int, int]:
    """(total bytes, file count) for the project, ignoring excluded trees."""
    total = 0
    count = 0
    limits = Limits()

    for path, _ in _walk(Path(workspace_root), limits):
        try:
            total += path.stat().st_size
            count += 1
        except OSError:
            continue

    return total, count


def list_files(workspace_root: Path, limits: Limits | None = None) -> tuple[FileEntry, ...]:
    """Every file in the project, as workspace-relative entries."""
    limits = limits or Limits()
    root = Path(workspace_root)
    entries: list[FileEntry] = []

    for path, _ in _walk(root, limits):
        if len(entries) >= limits.max_files:
            break
        try:
            entries.append(
                FileEntry(
                    path=relative_to_workspace(path, root),
                    is_directory=False,
                    size=path.stat().st_size,
                    modified_at=_modified(path),
                )
            )
        except (OSError, PathSecurityError):
            continue

    return tuple(sorted(entries, key=lambda entry: entry.path))


def list_directory(
    workspace_root: Path, relative_path: str = ".", limits: Limits | None = None
) -> tuple[FileEntry, ...]:
    """One directory level, files and subdirectories."""
    limits = limits or Limits()
    root = Path(workspace_root)

    if relative_path in (".", "", "./"):
        target = root
    else:
        target = resolve(root, relative_path).absolute

    if not target.exists():
        raise NotFoundError(f"'{relative_path}' does not exist")
    if not target.is_dir():
        raise FileOperationError(f"'{relative_path}' is not a directory")

    entries: list[FileEntry] = []
    for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
        if child.is_dir() and child.name in IGNORED_DIRECTORIES:
            continue
        try:
            entries.append(
                FileEntry(
                    path=relative_to_workspace(child, root),
                    is_directory=child.is_dir(),
                    size=0 if child.is_dir() else child.stat().st_size,
                    modified_at=_modified(child),
                )
            )
        except (OSError, PathSecurityError):
            continue

    return tuple(entries)


def read_file(
    workspace_root: Path, relative_path: str, limits: Limits | None = None
) -> str:
    """Read a text file, refusing anything over the read limit."""
    limits = limits or Limits()
    resolved = resolve(workspace_root, relative_path)
    path = resolved.absolute

    if not path.exists():
        raise NotFoundError(f"'{resolved.relative}' does not exist")
    if path.is_dir():
        raise FileOperationError(f"'{resolved.relative}' is a directory")

    # Never hand a credential file to a caller (and, later, to the AI agent).
    assert_not_secret(resolved.relative, path)
    # Never read through a hardlink whose data may live outside the workspace.
    assert_safe_to_read(path)

    size = path.stat().st_size
    if size > limits.max_read_size:
        raise FileTooLargeError(
            f"'{resolved.relative}' is {size} bytes, over the "
            f"{limits.max_read_size} byte read limit"
        )

    # Binary files are common in a project; refusing is clearer than returning
    # replacement characters the model would then try to edit.
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise FileOperationError(f"'{resolved.relative}' is not UTF-8 text") from None


def write_file(
    workspace_root: Path,
    relative_path: str,
    content: str,
    limits: Limits | None = None,
) -> FileEntry:
    """Create or overwrite a text file, enforcing size and project limits."""
    limits = limits or Limits()

    if not isinstance(content, str):
        raise FileOperationError("content must be a string")

    # Normalised to LF before anything else, so that the same logical content
    # produces the same bytes and therefore the same hash on every platform.
    # Changesets compare hashes, so a CRLF difference would otherwise show as a
    # modification that nobody made.
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    encoded = content.encode("utf-8")
    # Both limits apply: max_write_size bounds one request, max_file_size bounds
    # the resulting file. They are equal by default but configurable apart.
    if len(encoded) > limits.max_write_size:
        raise FileTooLargeError(
            f"content is {len(encoded)} bytes, over the "
            f"{limits.max_write_size} byte write limit"
        )
    if len(encoded) > limits.max_file_size:
        raise FileTooLargeError(
            f"content is {len(encoded)} bytes, over the "
            f"{limits.max_file_size} byte file-size limit"
        )

    resolved = resolve(workspace_root, relative_path)
    path = resolved.absolute
    root = Path(workspace_root)

    # A secret must not be writable-through either, and the size check + write
    # must be one critical section per workspace so concurrent writers cannot
    # race past max_project_size.
    with workspace_lock(root):
        assert_not_secret(resolved.relative, path)
        if path.exists():
            # An existing target must be a plain, single-linked, non-reparse
            # file, or the write could land on shared or external bytes.
            assert_safe_to_write(path)

        existing = path.stat().st_size if path.exists() and path.is_file() else 0
        total, count = project_size(root)

        if total - existing + len(encoded) > limits.max_project_size:
            raise ProjectTooLargeError(
                f"writing '{resolved.relative}' would exceed the "
                f"{limits.max_project_size} byte project limit"
            )
        if existing == 0 and count + 1 > limits.max_files:
            raise ProjectTooLargeError(f"project already holds {limits.max_files} files")

        path.parent.mkdir(parents=True, exist_ok=True)
        # Newline normalised so the same content produces the same bytes and the
        # same hash on every platform, which changesets depend on.
        path.write_text(content, encoding="utf-8", newline="\n")

        return FileEntry(
            path=resolved.relative,
            is_directory=False,
            size=len(encoded),
            modified_at=_modified(path),
        )


def create_file(
    workspace_root: Path,
    relative_path: str,
    content: str = "",
    limits: Limits | None = None,
) -> FileEntry:
    """Write a file that must not already exist."""
    resolved = resolve(workspace_root, relative_path)
    # Re-entrant lock: the exists-check and the write are one critical section,
    # so two concurrent creates cannot both pass the check.
    with workspace_lock(workspace_root):
        if resolved.absolute.exists():
            raise FileOperationError(f"'{resolved.relative}' already exists")
        return write_file(workspace_root, relative_path, content, limits)


def delete_file(workspace_root: Path, relative_path: str) -> str:
    """Delete a file, or a directory and its contents.

    A reparse point (junction/symlink) is removed as a *link* only, never
    recursed into, so deleting one cannot destroy the external tree it targets.
    """
    resolved = resolve(workspace_root, relative_path)
    path = resolved.absolute

    with workspace_lock(workspace_root):
        if not path.exists() and not is_reparse_point(path):
            raise NotFoundError(f"'{resolved.relative}' does not exist")

        if is_reparse_point(path):
            # Remove the link itself. os.rmdir unlinks a junction/dir-symlink
            # without touching its target; unlink handles a file symlink.
            try:
                os.rmdir(path)
            except OSError:
                path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    return resolved.relative


def rename_file(workspace_root: Path, source: str, destination: str) -> str:
    """Move a file. Both ends are resolved, so neither can point outside."""
    resolved_source = resolve(workspace_root, source)
    resolved_destination = resolve(workspace_root, destination)

    with workspace_lock(workspace_root):
        if not resolved_source.absolute.exists():
            raise NotFoundError(f"'{resolved_source.relative}' does not exist")
        # Do not move a secret, a hardlink or a reparse point: the destination
        # name would then hide unsafe content behind a benign path.
        assert_not_secret(resolved_source.relative, resolved_source.absolute)
        if resolved_source.absolute.is_file():
            assert_safe_to_write(resolved_source.absolute)
        elif is_reparse_point(resolved_source.absolute):
            raise UnsafeFileError("cannot rename a symlink or junction")
        if resolved_destination.absolute.exists():
            raise FileOperationError(f"'{resolved_destination.relative}' already exists")

        resolved_destination.absolute.parent.mkdir(parents=True, exist_ok=True)
        resolved_source.absolute.rename(resolved_destination.absolute)

    return resolved_destination.relative


def search_files(
    workspace_root: Path,
    query: str,
    *,
    max_results: int = 100,
    limits: Limits | None = None,
) -> tuple[SearchHit, ...]:
    """Plain substring search across project text files.

    Substring rather than regex: a model-supplied pattern is untrusted input,
    and a pathological regex is a denial-of-service vector.
    """
    limits = limits or Limits()

    if not isinstance(query, str) or not query.strip():
        raise FileOperationError("search query must be a non-empty string")

    root = Path(workspace_root)
    needle = query.lower()
    hits: list[SearchHit] = []

    for path, _ in _walk(root, limits):
        if len(hits) >= max_results:
            break
        try:
            relative = relative_to_workspace(path, root)
        except PathSecurityError:
            continue
        if is_secret_path(relative):
            continue
        try:
            if path.stat().st_size > limits.max_read_size:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for number, line in enumerate(text.splitlines(), start=1):
            if needle in line.lower():
                hits.append(SearchHit(relative, number, line.strip()[:400]))
                if len(hits) >= max_results:
                    break

    return tuple(hits)
