"""Low-level filesystem safety primitives shared by the workspace layer.

`resolve()` in ``paths`` guarantees a path *name* stays inside the workspace.
This module guards the things a name cannot express: that the file behind the
name is not a hardlink to data outside the workspace, that a directory is not a
reparse point (junction/symlink) leading elsewhere, that the real on-disk name
is not a Windows alias (8.3 short name, trailing dot) of something the caller
did not ask for, and that concurrent operations on one workspace do not race.

Standard library only. Windows behaviour is handled explicitly because
development and testing happen there; the same code is safe on POSIX, where the
Windows-specific branches simply do not trigger.
"""

from __future__ import annotations

import ctypes
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

__all__ = [
    "UnsafeFileError",
    "is_reparse_point",
    "has_extra_hardlinks",
    "canonical_name",
    "assert_safe_to_read",
    "assert_safe_to_write",
    "workspace_lock",
]

_IS_WINDOWS = os.name == "nt"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class UnsafeFileError(Exception):
    """A path resolved inside the workspace but the file itself is unsafe.

    Separate from PathSecurityError so callers can tell "the name escaped" from
    "the name was fine but the file is a hardlink/reparse trick". The message is
    safe to surface: it never contains a host path.
    """


def is_reparse_point(path: Path) -> bool:
    """True if `path` is a symlink, or (on Windows) a junction/reparse point.

    ``os.path.islink`` catches POSIX symlinks and Windows symlinks but not
    directory junctions, which are the common no-admin way to escape on Windows.
    The reparse-point attribute catches both.
    """
    try:
        if os.path.islink(path):
            return True
        if _IS_WINDOWS:
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs != -1 and (attrs & _FILE_ATTRIBUTE_REPARSE_POINT):
                return True
    except OSError:
        # A path we cannot stat is treated as suspicious.
        return True
    return False


def has_extra_hardlinks(path: Path) -> bool:
    """True if an existing regular file has more than one hard link.

    A second hard link means the same bytes are reachable under another name,
    which may live outside the workspace. We cannot tell from here where the
    other link is, so any link count above one is refused. Directories and
    missing paths return False (nothing to read/write through).
    """
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    return info.st_nlink > 1


def canonical_name(path: Path) -> str:
    """The file's true final component, defeating Windows aliases.

    ``id_rsa.`` and the 8.3 form ``ID_RSA~1`` both open ``id_rsa`` on Windows.
    Secret filtering on the *requested* name would miss them, so the secret
    check is applied to this canonical name as well. On POSIX the requested name
    is already canonical and this returns it unchanged.
    """
    if not _IS_WINDOWS:
        return Path(path).name

    buffer = ctypes.create_unicode_buffer(4096)
    length = ctypes.windll.kernel32.GetLongPathNameW(str(path), buffer, 4096)
    if length and length < 4096:
        return Path(buffer.value).name
    # File does not exist yet, or the call failed: fall back to a name with
    # Windows' silent trailing-dot/space stripping applied, so the caller still
    # sees what the OS would actually open.
    return Path(path).name.rstrip(" .")


def assert_safe_to_read(path: Path) -> None:
    """Refuse to read through a hardlink whose data may live outside."""
    if has_extra_hardlinks(path):
        raise UnsafeFileError("file has multiple hard links and cannot be read safely")


def assert_safe_to_write(path: Path) -> None:
    """Refuse to write through a hardlink or a reparse point.

    Writing through a hardlink modifies the shared bytes wherever the other
    link lives; writing through a reparse point writes to the target directory.
    New files (path does not exist yet) are always safe: resolve() already
    proved the *name* is inside, and there is no existing inode to hijack.
    """
    if is_reparse_point(path):
        raise UnsafeFileError("path is a symlink or junction and cannot be written")
    if has_extra_hardlinks(path):
        raise UnsafeFileError("file has multiple hard links and cannot be written safely")


# --- per-workspace locking ------------------------------------------------
#
# Consistency-sensitive operations (size-check-then-write, snapshot, restore)
# must not interleave within one workspace. A global lock would serialise every
# project, so locks are keyed by the workspace root. Re-entrant so that, e.g.,
# create_file -> write_file can nest without deadlock.

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.RLock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


@contextmanager
def workspace_lock(workspace_root: Path | str):
    """Hold the lock for one workspace for the duration of the block."""
    key = os.path.normcase(os.path.realpath(str(workspace_root)))
    lock = _lock_for(key)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
