import asyncio
from pathlib import Path

import pytest

from app.domain.models.event import ErrorEvent, MessageEvent, ToolEvent, ToolStatus
from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.models.session import Session, SessionStatus
from app.infrastructure.external.sandbox.path_policy import (
    SandboxPathError,
    resolve_under_root,
    validate_command,
)
from app.interfaces.schemas.event import EventMapper, MessageSSEEvent, ToolSSEEvent


def test_user_root_rejects_traversal_and_host_paths(tmp_path: Path):
    root = (tmp_path / "users" / "user-a").resolve()
    root.mkdir(parents=True)

    assert resolve_under_root("notes.txt", root).endswith("/user-a/notes.txt")
    with pytest.raises(SandboxPathError):
        resolve_under_root("../user-b/secret.txt", root)
    with pytest.raises(SandboxPathError):
        resolve_under_root("/etc/passwd", root)


def test_command_policy_allows_scoped_commands_and_rejects_escape_primitives(tmp_path: Path):
    root = (tmp_path / "user-a").resolve()
    root.mkdir()

    assert validate_command("python3 notes.py", root) == "python3 notes.py"
    assert validate_command("curl https://example.com", root) == "curl https://example.com"
    for command in ("cat /etc/passwd", "cat ../secret", "cat ~/secret", "cat x; id", "cat x | grep y"):
        with pytest.raises(SandboxPathError):
            validate_command(command, root)


def test_session_resume_reconstructs_step_state_from_events():
    plan = Plan(steps=[Step(id="1", description="work")])
    session = Session(
        user_id="user-a",
        agent_id="agent-a",
        status=SessionStatus.WAITING,
        events=[
            {"type": "plan", "plan": plan, "status": "created"},
            {"type": "step", "step": {"id": "1", "description": "work", "status": "running"}, "status": "started"},
        ],
    )
    restored = session.get_last_plan()
    assert restored is not None
    assert restored.steps[0].status == ExecutionStatus.RUNNING


def test_public_event_mapper_redacts_sensitive_fields():
    async def run():
        user_message = MessageEvent(role="user", message="secret prompt", attachments=[])
        tool_event = ToolEvent(
            tool_call_id="real-call",
            tool_name="shell",
            function_name="exec_command",
            function_args={"command": "cat /etc/passwd"},
            status=ToolStatus.CALLED,
            function_result="root:x:0:0",
        )
        public_user = await EventMapper.public_event_to_sse_event(user_message)
        public_tool = await EventMapper.public_event_to_sse_event(tool_event)
        assert isinstance(public_user, MessageSSEEvent)
        assert public_user.data.content == "[User message redacted]"
        assert isinstance(public_tool, ToolSSEEvent)
        assert public_tool.data.args == {}
        assert public_tool.data.content is None
        assert public_tool.data.tool_call_id != "real-call"

    asyncio.run(run())


def test_public_error_is_generic():
    async def run():
        event = await EventMapper.public_event_to_sse_event(ErrorEvent(error="secret traceback"))
        assert event.data.error == "The agent encountered an internal error."

    asyncio.run(run())


def test_session_files_public_access_requires_opt_in_and_safe_serializer(monkeypatch):
    from app.domain.models.file import FileInfo
    from app.interfaces.api.session_routes import get_session_files
    from app.interfaces.schemas.file import FileInfoResponse

    files = [FileInfo(
        file_id="file-1",
        filename="report.txt",
        content_type="text/plain",
        size=12,
        user_id="owner-1",
        metadata={"user_id": "owner-1", "storage_uri": "secret://internal", "contentType": "text/plain"},
    )]

    class FakeAgentService:
        def __init__(self, shared, share_files):
            self.session = type("SharedSession", (), {
                "is_shared": shared,
                "share_files": share_files,
                "files": files,
            })()

        async def get_shared_session(self, session_id):
            return self.session if self.session.is_shared else None

        async def get_shared_session_files(self, session_id):
            return self.session.files

        async def get_session_files(self, session_id, user_id):
            assert user_id == "owner-1"
            return self.session.files

    async def safe_file_response(file_info):
        return FileInfoResponse(
            file_id=file_info.file_id,
            filename=file_info.filename,
            content_type=file_info.content_type,
            size=file_info.size,
            metadata={"contentType": file_info.content_type},
            file_url="/safe-signed-url",
        )

    monkeypatch.setattr(FileInfoResponse, "from_file_info", staticmethod(safe_file_response))

    async def run():
        no_opt_in = await get_session_files("s-1", None, FakeAgentService(True, False))
        assert no_opt_in.data == []

        public = await get_session_files("s-1", None, FakeAgentService(True, True))
        assert public.data[0].file_url == "/safe-signed-url"
        assert public.data[0].metadata == {"contentType": "text/plain"}
        assert "storage_uri" not in public.data[0].metadata

        owner = await get_session_files("s-1", type("User", (), {"id": "owner-1"})(), FakeAgentService(False, False))
        assert owner.data == files

    asyncio.run(run())


def test_execution_prompts_format_with_scoped_user_home():
    from app.domain.services.prompts.execution import EXECUTION_PROMPT, SUMMARIZE_PROMPT

    user_home = "/tmp/dzeck-users-local/local_admin"
    execution = EXECUTION_PROMPT.format(
        step="Read one file",
        message="test",
        plan='{"steps":[{"id":"1","description":"Read one file","status":"pending"}]}',
        completed_steps="(none)",
        attachments="[]",
        language="English",
        user_home=user_home,
    )
    summary = SUMMARIZE_PROMPT.format(user_home=user_home)

    assert user_home in execution
    assert user_home in summary
    assert "/tmp/extract.py" not in execution
    assert "/tmp/extracted_content.txt" not in execution
    assert "/home/runner/summary_" not in summary
    assert "interface Response {" in summary


def test_replit_sandbox_exposes_configured_user_root():
    from app.infrastructure.external.sandbox.replit_sandbox import ReplitSandbox

    sandbox = ReplitSandbox.__new__(ReplitSandbox)
    sandbox._user_root = Path("/tmp/dzeck-users-local").resolve()
    assert sandbox.user_root == "/tmp/dzeck-users-local"


def test_user_scoped_sandbox_rejects_legacy_admin_setup_fallback(tmp_path):
    from app.infrastructure.external.sandbox.user_sandbox import UserScopedSandbox

    class LegacyAdapter:
        user_root = str(tmp_path)

        async def _run_admin_cmd(self, command):
            raise AssertionError("legacy admin shell must not be called")

    scoped = UserScopedSandbox(LegacyAdapter(), "user-a")
    with pytest.raises(RuntimeError, match="authenticated setup_user_home"):
        asyncio.run(scoped.setup_user_home())


def test_planner_acknowledgement_falls_back_to_non_streaming_completion():
    from langchain.messages import AIMessage
    from app.domain.models.message import Message
    from app.domain.services.agents.planner import PlannerAgent
    from app.domain.models.event import MessageChunkEvent, MessageEvent

    class EmptyStreamingModel:
        async def astream(self, context):
            if False:
                yield None

        async def ainvoke(self, context):
            return AIMessage(content="Saya akan membantu membuatnya terlebih dahulu.")

    planner = PlannerAgent.__new__(PlannerAgent)
    planner._model = EmptyStreamingModel()

    async def no_previous_files():
        return []

    planner._get_previous_file_names = no_previous_files

    async def run():
        return [event async for event in planner.acknowledge(Message(message="Buat website"))]

    events = asyncio.run(run())
    assert isinstance(events[0], MessageChunkEvent)
    assert events[0].content.startswith("Saya akan membantu")
    assert events[0].done is False
    assert events[1].done is True
    assert isinstance(events[2], MessageEvent)
    assert events[2].message == events[0].content


def test_execution_forwards_real_tool_events():
    from app.domain.services.agents.execution import ExecutionAgent

    agent = ExecutionAgent.__new__(ExecutionAgent)

    async def fake_execute(_content):
        yield ToolEvent(
            tool_call_id="call-1",
            tool_name="file",
            function_name="file_write",
            function_args={"file": "/tmp/x", "content": "ok"},
            status=ToolStatus.CALLING,
        )
        yield ToolEvent(
            tool_call_id="call-1",
            tool_name="file",
            function_name="file_write",
            function_args={"file": "/tmp/x", "content": "ok"},
            function_result={"success": True},
            status=ToolStatus.CALLED,
        )
        yield MessageEvent(message='{"success": true, "result": "ok"}')

    agent.execute = fake_execute

    class ParserStub:
        async def aparse_with_prompt(self, _text, _prompt):
            return {"success": True, "result": "ok"}

    agent._json_output_parser = ParserStub()
    step = Step(id="1", description="Use file_write", status=ExecutionStatus.RUNNING)

    async def collect():
        return [event async for event in agent._handle_execution_events(step, "do work")]

    events = asyncio.run(collect())
    assert [event.type for event in events] == ["tool", "tool"]
    assert events[0].status == ToolStatus.CALLING
    assert events[1].status == ToolStatus.CALLED
    assert step.success is True


def test_tool_wrapper_drops_unexpected_arguments(monkeypatch):
    from app.domain.models.tool_result import ToolResult
    from app.domain.services.tools.file import FileToolkit

    class SandboxStub:
        async def file_list(self, path):
            assert path == "/tmp/safe"
            return ToolResult(success=True, message="listed")

    toolkit = FileToolkit(object())
    toolkit.sandbox = SandboxStub()
    tool = toolkit.get_tool("file_list_dir")
    assert tool is not None

    async def run():
        return await tool.ainvoke({
            "id": "call-1",
            "args": {"path": "/tmp/safe", "?": None},
        })

    result = asyncio.run(run())
    assert result.name == "file_list_dir"
    assert "listed" in result.content


def test_planner_empty_update_removes_obsolete_pending_steps():
    from app.domain.services.agents.planner import PlannerAgent

    planner = PlannerAgent.__new__(PlannerAgent)

    class EmptyUpdateParser:
        async def aparse_with_prompt(self, _text, _prompt):
            return {"steps": []}

    planner._json_output_parser = EmptyUpdateParser()

    async def fake_execute(_message):
        yield MessageEvent(message='{"steps": []}')

    planner.execute = fake_execute
    plan = Plan(steps=[
        Step(id="1", description="create structure", status=ExecutionStatus.COMPLETED, success=True),
        Step(id="2", description="write files"),
        Step(id="3", description="verify files"),
    ])

    async def run():
        return [event async for event in planner.update_plan(plan, plan.steps[0])]

    events = asyncio.run(run())
    assert len(events) == 1
    assert [item.id for item in plan.steps] == ["1"]
    assert plan.get_next_step() is None


def test_step_message_and_plan_status_are_preserved_in_sse():
    from app.interfaces.schemas.event import MessageSSEEvent, PlanSSEEvent

    async def run():
        step_message = await EventMapper.event_to_sse_event(
            MessageEvent(message="Menyiapkan file", step_id="step-1")
        )
        assert isinstance(step_message, MessageSSEEvent)
        assert step_message.data.step_id == "step-1"
        assert step_message.data.final is False

        final_message = await EventMapper.event_to_sse_event(
            MessageEvent(message="Selesai", final=True)
        )
        assert isinstance(final_message, MessageSSEEvent)
        assert final_message.data.final is True

        completed_plan = Plan(
            steps=[Step(id="step-1", description="work", status=ExecutionStatus.COMPLETED)],
            status=ExecutionStatus.COMPLETED,
        )
        plan_event = await EventMapper.event_to_sse_event(
            __import__("app.domain.models.event", fromlist=["PlanEvent"]).PlanEvent(
                plan=completed_plan,
                status=__import__("app.domain.models.event", fromlist=["PlanStatus"]).PlanStatus.COMPLETED,
            )
        )
        assert isinstance(plan_event, PlanSSEEvent)
        assert plan_event.data.status == ExecutionStatus.COMPLETED

    asyncio.run(run())


def test_user_scoped_sandbox_enforces_all_delegated_paths_and_commands(tmp_path: Path):
    from app.domain.models.tool_result import ToolResult
    from app.infrastructure.external.sandbox.user_sandbox import UserScopedSandbox

    class RecordingAdapter:
        user_root = str(tmp_path / "users")

        def __init__(self):
            self.calls = []

        async def setup_user_home(self, user_id):
            home = Path(self.user_root) / user_id
            home.mkdir(parents=True, exist_ok=True)
            self.calls.append(("setup_user_home", user_id))
            return str(home)

        async def file_write(self, file, content, append=False, leading_newline=False, trailing_newline=False, sudo=False):
            self.calls.append(("file_write", file, sudo))
            return ToolResult(success=True, data={"path": file})

        async def file_move(self, source, destination):
            self.calls.append(("file_move", source, destination))
            return ToolResult(success=True)

        async def file_copy(self, source, destination):
            self.calls.append(("file_copy", source, destination))
            return ToolResult(success=True)

        async def exec_command(self, session_id, exec_dir, command):
            self.calls.append(("exec_command", session_id, exec_dir, command))
            return ToolResult(success=True)

    async def run():
        adapter = RecordingAdapter()
        scoped = UserScopedSandbox(adapter, "user-a")
        await scoped.setup_user_home()
        await scoped.file_write("notes.txt", "ok", sudo=True)
        absolute = str(Path(adapter.user_root) / "user-a" / "absolute.txt")
        await scoped.file_write(absolute, "ok")
        await scoped.file_move("notes.txt", "moved.txt")
        await scoped.file_copy("moved.txt", "copied.txt")
        await scoped.exec_command("shell-a", scoped.user_home, "python3 notes.py")

        with pytest.raises(SandboxPathError):
            await scoped.file_write("../user-b/secret.txt", "no")
        with pytest.raises(SandboxPathError):
            await scoped.file_write("/etc/passwd", "no")
        with pytest.raises(SandboxPathError):
            await scoped.file_move("notes.txt", "../user-b/secret.txt")
        with pytest.raises(SandboxPathError):
            await scoped.file_copy("../user-b/secret.txt", "copy.txt")
        with pytest.raises(SandboxPathError):
            await scoped.exec_command("shell-a", scoped.user_home, "cat notes.txt; id")
        with pytest.raises(SandboxPathError):
            await scoped.exec_command("shell-a", "/etc", "cat /etc/passwd")

        assert ("setup_user_home", "user-a") in adapter.calls
        assert ("file_write", str(Path(adapter.user_root) / "user-a" / "notes.txt"), False) in adapter.calls
        assert not any(call[0] == "file_write" and call[2] is True for call in adapter.calls)
        assert all("user-b" not in call for call in adapter.calls if isinstance(call, tuple))

    asyncio.run(run())


def test_agent_service_file_and_shell_views_enforce_scopes():
    from app.application.services.agent_service import AgentService
    from app.domain.models.event import ToolEvent, ToolStatus
    from app.domain.models.tool_result import ToolResult

    class Repo:
        async def find_by_id_and_user_id(self, session_id, user_id):
            return Session(
                id=session_id,
                user_id=user_id,
                agent_id="agent-a",
                sandbox_id="sandbox-a",
                events=[ToolEvent(
                    tool_call_id="call-a",
                    tool_name="shell",
                    function_name="shell_exec",
                    function_args={"id": "owned-shell", "exec_dir": "/tmp", "command": "true"},
                    status=ToolStatus.CALLED,
                )],
            )

    class Adapter:
        user_root = "/tmp/dzeck-users-local"
        id = "sandbox-a"
        cdp_url = ""
        vnc_url = ""

        async def view_shell(self, session_id, console=False):
            return ToolResult(success=True, data={"session_id": session_id, "output": "ok", "console": []})

        async def file_read(self, file, start_line=None, end_line=None, sudo=False):
            assert "/user-a/" in file
            return ToolResult(success=True, data={"content": "ok", "file": file})

        async def setup_user_home(self, user_id):
            return str(Path(self.user_root) / user_id)

    class AdapterClass:
        @classmethod
        async def get(cls, sandbox_id):
            return Adapter()

    service = AgentService.__new__(AgentService)
    service._session_repository = Repo()
    service._sandbox_cls = AdapterClass

    async def run():
        result = await service.file_view("s-1", "notes.txt", "user-a")
        assert result.content == "ok"
        await service.shell_view("s-1", "owned-shell", "user-a")
        with pytest.raises(RuntimeError, match="Shell session not found"):
            await service.shell_view("s-1", "foreign-shell", "user-a")
        with pytest.raises(SandboxPathError):
            await service.file_view("s-1", "/etc/passwd", "user-a")

    asyncio.run(run())


def test_clear_unread_message_count_is_owner_scoped():
    from app.application.services.agent_service import AgentService

    class Repo:
        def __init__(self):
            self.updates = []

        async def find_by_id_and_user_id(self, session_id, user_id):
            if user_id != "owner-a":
                return None
            return Session(id=session_id, user_id=user_id, agent_id="agent-a")

        async def update_unread_message_count_for_user(self, session_id, user_id, count):
            self.updates.append((session_id, user_id, count))

    repo = Repo()
    service = AgentService.__new__(AgentService)
    service._session_repository = repo

    async def run():
        with pytest.raises(RuntimeError, match="Session not found"):
            await service.clear_unread_message_count("session-a", "intruder")
        assert repo.updates == []

        await service.clear_unread_message_count("session-a", "owner-a")
        assert repo.updates == [("session-a", "owner-a", 0)]

    asyncio.run(run())


def test_dynamic_mcp_tool_is_callable_and_observable_through_base_agent():
    from app.domain.services.agents.base import BaseAgent
    from app.domain.services.tools.mcp import MCPToolkit
    from app.domain.models.tool_result import ToolResult
    from langchain.messages import AIMessage, ToolMessage

    class FakeManager:
        def __init__(self):
            self.calls = []

        async def call_tool(self, tool_name, arguments):
            self.calls.append((tool_name, arguments))
            return ToolResult(success=True, data={"echo": arguments["text"]})

    toolkit = MCPToolkit()
    toolkit.name = "mcp"
    toolkit._tools = [{
        "type": "function",
        "function": {
            "name": "mcp_demo_echo",
            "description": "Echo text through the demo MCP server",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    }]
    toolkit.manager = FakeManager()
    toolkit._initialized = True

    agent = BaseAgent.__new__(BaseAgent)
    agent.toolkits = [toolkit]
    agent.max_retries = 0
    agent.retry_interval = 0
    agent.max_iterations = 2
    agent.format = None
    agent._agent_id = "agent-a"

    dynamic_tool = agent.get_tool("mcp_demo_echo")
    assert dynamic_tool is not None

    async def run():
        direct = await dynamic_tool.ainvoke({
            "id": "direct-call",
            "name": "mcp_demo_echo",
            "args": {"text": "direct"},
        })
        assert isinstance(direct, ToolMessage)
        assert direct.tool_call_id == "direct-call"
        assert direct.artifact.success is True

        responses = iter([
            AIMessage(content="", tool_calls=[{
                "name": "mcp_demo_echo",
                "args": {"text": "agent"},
                "id": "agent-call",
                "type": "tool_call",
            }]),
            AIMessage(content="MCP selesai."),
        ])

        async def fake_ask(_request, _format=None):
            return next(responses)

        agent.ask = fake_ask

        async def fake_ask_with_messages(_messages, _format=None):
            return next(responses)

        agent.ask_with_messages = fake_ask_with_messages
        events = [event async for event in agent.execute("gunakan MCP")]
        tool_events = [event for event in events if isinstance(event, ToolEvent)]
        assert [event.status for event in tool_events] == [ToolStatus.CALLING, ToolStatus.CALLED]
        assert tool_events[0].tool_name == "mcp"
        assert tool_events[1].tool_call_id == "agent-call"
        assert toolkit.manager.calls == [
            ("mcp_demo_echo", {"text": "direct"}),
            ("mcp_demo_echo", {"text": "agent"}),
        ]

    asyncio.run(run())


def test_refresh_token_rotation_replay_revokes_family_and_blacklist_fails_closed(monkeypatch):
    from app.application.services.token_service import TokenService
    from app.domain.models.user import User, UserRole

    class MemoryRedis:
        def __init__(self):
            self.values = {}

        async def setex(self, key, _ttl, value):
            self.values[key] = value

        async def get(self, key):
            return self.values.get(key)

        async def exists(self, key):
            return 1 if key in self.values else 0

        async def set(self, key, value, ex=None, nx=False):
            if nx and key in self.values:
                return None
            self.values[key] = value
            return True

        async def delete(self, key):
            self.values.pop(key, None)

    class RedisHolder:
        def __init__(self, client):
            self.client = client

    redis_client = MemoryRedis()
    monkeypatch.setattr(
        "app.application.services.token_service.get_redis",
        lambda: RedisHolder(redis_client),
    )
    service = TokenService.__new__(TokenService)
    service.settings = type("Settings", (), {
        "jwt_secret_key": "unit-test-jwt-secret-key-with-at-least-32-chars",
        "jwt_algorithm": "HS256",
        "jwt_refresh_token_expire_days": 7,
        "jwt_access_token_expire_minutes": 30,
    })()
    user = User(
        id="user-a",
        fullname="User A",
        email="a@example.com",
        role=UserRole.USER,
        is_active=True,
    )

    async def run():
        original = service.create_refresh_token(user)
        assert await service.register_refresh_token(original) is True
        original_payload = service.verify_token(original)
        replacement = service.create_refresh_token(user, original_payload["family_id"])

        assert await service.rotate_refresh_token(original) == original
        assert await service.register_refresh_token(replacement) is True
        assert await service.rotate_refresh_token(original) is None
        assert await redis_client.get(
            f"refresh:family:{original_payload['family_id']}"
        ) == "revoked"
        assert await service.rotate_refresh_token(replacement) is None

        class BrokenRedis:
            async def exists(self, _key):
                raise ConnectionError("redis unavailable")

        monkeypatch.setattr(
            "app.application.services.token_service.get_redis",
            lambda: RedisHolder(BrokenRedis()),
        )
        assert await service.async_is_blacklisted("some-token") is True

    asyncio.run(run())


def test_auth_service_refresh_returns_rotated_refresh_token():
    from app.application.services.auth_service import AuthService
    from app.domain.models.user import User, UserRole

    user = User(
        id="user-a",
        fullname="User A",
        email="a@example.com",
        role=UserRole.USER,
        is_active=True,
    )

    class UserRepo:
        async def get_user_by_id(self, user_id):
            return user if user_id == user.id else None

    class TokenStub:
        def __init__(self):
            self.created = []
            self.registered = []

        def verify_token(self, token):
            assert token == "old-refresh"
            return {"sub": "user-a", "type": "refresh", "family_id": "family-a"}

        async def rotate_refresh_token(self, token):
            assert token == "old-refresh"
            return token

        def create_access_token(self, _user):
            return "new-access"

        def create_refresh_token(self, _user, family_id=None):
            self.created.append(family_id)
            return "new-refresh"

        async def register_refresh_token(self, token):
            self.registered.append(token)
            return True

    token_service = TokenStub()
    service = AuthService.__new__(AuthService)
    service.user_repository = UserRepo()
    service.token_service = token_service

    async def run():
        result = await service.refresh_access_token("old-refresh")
        assert result.access_token == "new-access"
        assert result.refresh_token == "new-refresh"
        assert token_service.created == ["family-a"]
        assert token_service.registered == ["new-refresh"]

    asyncio.run(run())


def test_share_files_policy_and_expiry_are_enforced():
    from datetime import datetime, timedelta, UTC
    from app.application.services.agent_service import AgentService

    class Repo:
        def __init__(self):
            self.updated = None
            self.shared = Session(id="s-1", user_id="owner-a", agent_id="agent-a", is_shared=True)

        async def find_by_id_and_user_id(self, session_id, user_id):
            return self.shared if user_id == "owner-a" else None

        async def update_shared_status(self, session_id, is_shared, share_files=False, share_expires_at=None):
            self.updated = (session_id, is_shared, share_files, share_expires_at)

        async def find_by_id(self, session_id):
            return self.shared

    repo = Repo()
    service = AgentService.__new__(AgentService)
    service._session_repository = repo

    async def run():
        await service.share_session("s-1", "owner-a", share_files=True, expires_in_minutes=60)
        assert repo.updated[0:3] == ("s-1", True, True)
        assert repo.updated[3] is not None
        assert repo.updated[3] > datetime.now(UTC)

        repo.shared.share_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await service.get_shared_session("s-1") is None
        repo.shared.share_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        assert await service.get_shared_session("s-1") is repo.shared

        with pytest.raises(ValueError, match="between 5 minutes"):
            await service.share_session("s-1", "owner-a", expires_in_minutes=2)

    asyncio.run(run())
