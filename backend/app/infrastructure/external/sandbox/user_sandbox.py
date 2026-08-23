import logging
import os
import re
import shlex
from pathlib import Path
from typing import Optional, BinaryIO

from app.core.config import get_settings
from app.domain.models.tool_result import ToolResult
from app.domain.external.browser import Browser
from app.infrastructure.external.sandbox.path_policy import (
    SandboxPathError,
    resolve_under_root,
    validate_command,
)

logger = logging.getLogger(__name__)


class UserScopedSandbox:
    """Per-user sandbox facade with server-side filesystem enforcement.

    The underlying Replit sandbox is shared by the application process. This
    facade is therefore a security boundary for every file and shell method
    exposed to an agent: paths must resolve below the configured user's root,
    symlink/traversal escapes are rejected, and shell control operators are not
    accepted. The system prompt remains useful guidance, but is not relied on
    for authorization.
    """

    _USER_ID_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,128}$")

    def __init__(self, sandbox, user_id: str) -> None:
        if not isinstance(user_id, str) or not self._USER_ID_RE.fullmatch(user_id):
            raise ValueError("invalid user id for sandbox scope")
        if user_id in {".", ".."} or ".." in Path(user_id).parts:
            raise ValueError("invalid user id for sandbox scope")

        self._inner = sandbox
        self._user_id = user_id
        configured_root = getattr(sandbox, "user_root", None)
        if not configured_root:
            configured_root = get_settings().sandbox_user_root
        self._root = Path(configured_root).expanduser().resolve()
        self._user_home = str((self._root / user_id).resolve())
        self._upload_dir = str((Path(self._user_home) / "upload").resolve())

        # Defensive invariant: construction itself must never create a scope
        # outside the configured root.
        resolve_under_root(self._user_home, self._root)
        resolve_under_root(self._upload_dir, self._root)

    @property
    def user_home(self) -> str:
        return self._user_home

    @property
    def upload_dir(self) -> str:
        return self._upload_dir

    def _path(self, path: str) -> str:
        """Resolve a user-supplied path below this user's home."""
        return resolve_under_root(path, Path(self._user_home))

    def _glob(self, pattern: str) -> str:
        if not isinstance(pattern, str) or not pattern.strip():
            raise SandboxPathError("glob pattern must be a non-empty string")
        candidate = Path(pattern)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise SandboxPathError("glob pattern escapes sandbox root")
        return pattern

    async def setup_user_home(self) -> None:
        """Create the scoped home through the authenticated adapter contract."""
        setup = getattr(self._inner, "setup_user_home", None)
        if not callable(setup):
            raise RuntimeError("sandbox does not provide authenticated setup_user_home")

        try:
            created_home = await setup(self._user_id)
            if created_home and str(Path(created_home).resolve()) != self._user_home:
                raise RuntimeError("sandbox returned a mismatched user home")
            logger.info("UserScopedSandbox: home ready at %s", self._user_home)
        except Exception:
            logger.exception("UserScopedSandbox: failed to create home for user %s", self._user_id)
            raise

    async def ensure_sandbox(self) -> None:
        return await self._inner.ensure_sandbox()

    async def exec_command(self, session_id: str, exec_dir: str, command: str) -> ToolResult:
        scoped_dir = self._path(exec_dir or self._user_home)
        safe_command = validate_command(command, Path(self._user_home))
        return await self._inner.exec_command(session_id, scoped_dir, safe_command)

    async def view_shell(self, session_id: str, console: bool = False) -> ToolResult:
        return await self._inner.view_shell(session_id, console)

    async def wait_for_process(self, session_id: str, seconds: Optional[int] = None) -> ToolResult:
        return await self._inner.wait_for_process(session_id, seconds)

    async def write_to_process(self, session_id: str, input_text: str, press_enter: bool = True) -> ToolResult:
        return await self._inner.write_to_process(session_id, input_text, press_enter)

    async def kill_process(self, session_id: str) -> ToolResult:
        return await self._inner.kill_process(session_id)

    async def file_write(
        self,
        file: str,
        content: str,
        append: bool = False,
        leading_newline: bool = False,
        trailing_newline: bool = False,
        sudo: Optional[bool] = False,
    ) -> ToolResult:
        return await self._inner.file_write(
            self._path(file), content, append, leading_newline, trailing_newline, False
        )

    async def file_read(
        self,
        file: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        sudo: bool = False,
    ) -> ToolResult:
        return await self._inner.file_read(self._path(file), start_line, end_line, False)

    async def file_exists(self, path: str) -> ToolResult:
        return await self._inner.file_exists(self._path(path))

    async def file_delete(self, path: str) -> ToolResult:
        return await self._inner.file_delete(self._path(path))

    async def file_move(self, source: str, destination: str) -> ToolResult:
        return await self._inner.file_move(self._path(source), self._path(destination))

    async def file_copy(self, source: str, destination: str) -> ToolResult:
        return await self._inner.file_copy(self._path(source), self._path(destination))

    async def file_list(self, path: str) -> ToolResult:
        return await self._inner.file_list(self._path(path))

    async def file_replace(
        self, file: str, old_str: str, new_str: str, sudo: bool = False
    ) -> ToolResult:
        return await self._inner.file_replace(self._path(file), old_str, new_str, False)

    async def file_search(self, file: str, regex: str, sudo: bool = False) -> ToolResult:
        return await self._inner.file_search(self._path(file), regex, False)

    async def file_find(self, path: str, glob_pattern: str) -> ToolResult:
        return await self._inner.file_find(self._path(path), self._glob(glob_pattern))

    async def file_upload(
        self,
        file_data: BinaryIO,
        path: str,
        filename: Optional[str] = None,
    ) -> ToolResult:
        return await self._inner.file_upload(file_data, self._path(path), filename)

    async def file_download(self, path: str) -> BinaryIO:
        return await self._inner.file_download(self._path(path))

    async def destroy(self) -> bool:
        return await self._inner.destroy()

    async def get_browser(self) -> Browser:
        return await self._inner.get_browser()

    @property
    def id(self) -> str:
        return self._inner.id

    @property
    def cdp_url(self) -> str:
        return self._inner.cdp_url

    @property
    def vnc_url(self) -> str:
        return self._inner.vnc_url

    @classmethod
    async def create(cls):
        raise NotImplementedError(
            "Call ReplitSandbox.create() then wrap with UserScopedSandbox(sandbox, user_id)"
        )

    @classmethod
    async def get(cls, id: str):
        raise NotImplementedError(
            "Call ReplitSandbox.get(id) then wrap with UserScopedSandbox(sandbox, user_id)"
        )
