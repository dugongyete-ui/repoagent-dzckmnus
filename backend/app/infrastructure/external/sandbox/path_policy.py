"""Small, dependency-free sandbox path and command policy helpers."""

from __future__ import annotations

import re
import shlex
from pathlib import Path


class SandboxPathError(ValueError):
    """Raised when a path or command would escape the scoped sandbox."""


def resolve_under_root(path: str, root: Path) -> str:
    """Resolve *path* and require the result to remain below *root*.

    Absolute paths are permitted only when they already reside under the
    supplied root. Relative paths are resolved from the root. Symlink and
    traversal escapes are rejected after canonicalisation.
    """
    if not isinstance(path, str) or not path.strip():
        raise SandboxPathError("path must be a non-empty string")

    root_path = Path(root).expanduser().resolve()
    candidate = Path(path).expanduser()
    resolved = candidate.resolve() if candidate.is_absolute() else (root_path / candidate).resolve()

    if resolved != root_path and root_path not in resolved.parents:
        raise SandboxPathError(f"path escapes sandbox root: {path}")
    return str(resolved)


_SHELL_META = re.compile(r"[;&|<>`$(){}\n\r]")


def validate_command(command: str, root: Path) -> str:
    """Reject obvious shell escape primitives before a command reaches sandbox."""
    if not isinstance(command, str) or not command.strip():
        raise SandboxPathError("command must be a non-empty string")
    if _SHELL_META.search(command):
        raise SandboxPathError("shell metacharacters are not allowed")

    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise SandboxPathError("malformed shell command") from exc
    if not tokens:
        raise SandboxPathError("command must contain a program")

    for token in tokens[1:]:
        if token == "~" or token.startswith("~/"):
            raise SandboxPathError("home expansion is not allowed")
        if token == ".." or token.startswith("../") or "/../" in token:
            raise SandboxPathError("parent traversal is not allowed")
        if token.startswith("/"):
            # Absolute paths are only safe when they resolve under the scoped
            # root. URL arguments are not filesystem paths and remain allowed.
            resolve_under_root(token, root)

    return command
