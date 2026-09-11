"""Secure path resolution.

This is the filesystem security boundary. Every path that originates outside
the trusted backend — from the API, from the coding agent, from a tool call,
from a model's output — must pass through :func:`resolve` before it touches
the filesystem. There is no second line of defence behind this module.

The threat is that untrusted input names a file outside the workspace. That can
be attempted with parent traversal, an absolute path, a Windows drive or UNC
path, mixed separators, a NUL byte, a percent-encoded separator, or a symlink
that points outside. All of them are rejected here, and containment is verified
against the *real* (symlink-resolved) path rather than the textual one.

Standard library only.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PathSecurityError",
    "resolve",
    "relative_to_workspace",
    "is_within",
]


class PathSecurityError(Exception):
    """A path was rejected because it could escape the workspace.

    The message is safe to return to a caller: it names the rule that was
    broken, never the resolved host path, which would disclose the layout of
    the server's filesystem.
    """


# Windows drive prefix, e.g. "C:", "c:/", "C:\\".
_DRIVE = re.compile(r"^[A-Za-z]:")

# Percent-encoded separators and dots. We never URL-decode a path ourselves, so
# these would be harmless literal characters here — but a component further
# down the stack might decode them, and a filename containing them is never
# legitimate in a project. Rejecting is cheap; a double-decode bug is not.
_ENCODED = re.compile(r"%(?:2e|2f|5c|00)", re.IGNORECASE)

# Reserved device names on Windows. Opening one of these writes to a device
# rather than a file, so they must never be reachable as a project path.
_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)

MAX_PATH_LENGTH = 1024
MAX_COMPONENT_LENGTH = 255


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """A path proven to lie inside its workspace."""

    absolute: Path
    relative: str
    workspace_root: Path


def _real(path: Path) -> Path:
    """Fully resolve symlinks, without requiring the path to exist.

    ``os.path.realpath`` resolves every symlink in the path, including ones in
    parent directories, and normalises the result. Unlike ``Path.resolve()`` on
    some versions it does not raise for a path that does not exist yet, which
    matters because we resolve paths for files we are about to create.
    """
    return Path(os.path.realpath(str(path)))


def _same_or_under(child: Path, parent: Path) -> bool:
    """True when `child` is `parent` or lies beneath it.

    Compared with ``os.path.normcase`` because Windows paths are
    case-insensitive; a plain string comparison would let ``/Workspace/..``
    differ from ``/workspace/..`` and defeat the check.
    """
    child_norm = os.path.normcase(str(child))
    parent_norm = os.path.normcase(str(parent))

    if child_norm == parent_norm:
        return True

    return child_norm.startswith(parent_norm.rstrip(os.sep) + os.sep)


def is_within(candidate: Path, workspace_root: Path) -> bool:
    """Whether `candidate` really lies inside `workspace_root`."""
    return _same_or_under(_real(candidate), _real(workspace_root))


def resolve(workspace_root: Path | str, user_path: str) -> ResolvedPath:
    """Resolve `user_path` against `workspace_root`, or raise.

    `user_path` is untrusted. It must be relative, must not escape, and must not
    name a device. The returned absolute path is safe to open.
    """
    if not isinstance(user_path, str):
        raise PathSecurityError(f"path must be a string, got {type(user_path).__name__}")

    root = _real(Path(workspace_root))

    raw = user_path.strip()
    if not raw:
        raise PathSecurityError("path must not be empty")

    if len(raw) > MAX_PATH_LENGTH:
        raise PathSecurityError(f"path exceeds {MAX_PATH_LENGTH} characters")

    # NUL truncates the path in many C-level filesystem calls, so a name like
    # "safe.txt\x00../../etc/passwd" can open something other than it appears to.
    if "\x00" in raw:
        raise PathSecurityError("path must not contain a null byte")

    if any(ord(character) < 32 for character in raw):
        raise PathSecurityError("path must not contain control characters")

    if _ENCODED.search(raw):
        raise PathSecurityError("path must not contain percent-encoded separators")

    # Normalise separators before any structural check, so that "..\\..\\x" is
    # examined as "../../x" rather than sliding past a check that only knows "/".
    candidate = raw.replace("\\", "/")

    # UNC paths reach other machines entirely.
    if candidate.startswith("//"):
        raise PathSecurityError("UNC paths are not allowed")

    if candidate.startswith("/"):
        raise PathSecurityError("absolute paths are not allowed")

    if _DRIVE.match(candidate):
        raise PathSecurityError("drive-qualified paths are not allowed")

    parts = [part for part in candidate.split("/") if part not in ("", ".")]

    for part in parts:
        # Checked first and specifically: ".." consists entirely of dots, so
        # the trailing-dot/space rule below would also catch it, but with a
        # misleading message ("must not end with a dot or space") instead of
        # naming what actually happened. Same rejection either way; this is
        # about error clarity, not security.
        if part == "..":
            raise PathSecurityError("parent traversal ('..') is not allowed")
        if len(part) > MAX_COMPONENT_LENGTH:
            raise PathSecurityError(
                f"path component exceeds {MAX_COMPONENT_LENGTH} characters"
            )
        # A colon introduces an NTFS alternate data stream ("a.txt:hidden"),
        # which hides bytes from size accounting, snapshots and change
        # detection. Windows forbids ':' in real filenames anyway.
        if ":" in part:
            raise PathSecurityError("path component must not contain ':'")
        # Windows silently strips a trailing dot or space, so "id_rsa." opens
        # "id_rsa" and slips past secret filtering. Neither is a legitimate
        # component name, so both are refused on every platform.
        if part != part.rstrip(" ."):
            raise PathSecurityError("path component must not end with a dot or space")
        # The extension is stripped because "NUL.txt" still names the device.
        if part.split(".")[0].lower() in _RESERVED:
            raise PathSecurityError(f"'{part}' is a reserved device name")

    if not parts:
        raise PathSecurityError("path must name a file or directory")

    # ".." is rejected outright (in the per-component loop above) rather than
    # collapsed. Collapsing is where traversal bugs live: it invites
    # disagreement between our normalisation and the operating system's. A
    # project path never legitimately needs "..".

    joined = root.joinpath(*parts)
    real = _real(joined)

    # The authoritative check. Everything above is early rejection for clearer
    # errors; this is what actually guarantees containment, because it runs
    # after every symlink in the chain has been resolved.
    if not _same_or_under(real, root):
        raise PathSecurityError("path escapes the workspace")

    return ResolvedPath(
        absolute=real,
        relative="/".join(parts),
        workspace_root=root,
    )


def relative_to_workspace(absolute: Path, workspace_root: Path) -> str:
    """Express an absolute path relative to its workspace, with '/' separators.

    Used whenever a path is reported outward, so that host filesystem layout
    never appears in an API response, a log line, or an AI prompt.
    """
    root = _real(Path(workspace_root))
    real = _real(Path(absolute))

    if not _same_or_under(real, root):
        raise PathSecurityError("path escapes the workspace")

    relative = os.path.relpath(str(real), str(root))
    return "." if relative == "." else relative.replace("\\", "/")
