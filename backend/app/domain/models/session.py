from pydantic import BaseModel, Field
from datetime import datetime, UTC
from typing import List, Optional
from enum import Enum
import uuid
from app.domain.models.event import PlanEvent, AgentEvent, StepEvent, StepStatus, PlanStatus
from app.domain.models.plan import Plan, ExecutionStatus
from app.domain.models.file import FileInfo


class SessionStatus(str, Enum):
    """Session status enum"""
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"


class SessionSummary(BaseModel):
    """Lightweight session model for list views (excludes heavy events/files)"""
    id: str
    user_id: str
    title: Optional[str] = None
    unread_message_count: int = 0
    latest_message: Optional[str] = None
    latest_message_at: Optional[datetime] = None
    status: SessionStatus = SessionStatus.PENDING
    is_shared: bool = False


class Session(BaseModel):
    """Session model"""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    user_id: str  # User ID that owns this session
    sandbox_id: Optional[str] = Field(default=None)  # Identifier for the sandbox environment
    agent_id: str
    task_id: Optional[str] = None
    title: Optional[str] = None
    unread_message_count: int = 0
    latest_message: Optional[str] = None
    latest_message_at: Optional[datetime] = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    events: List[AgentEvent] = []
    files: List[FileInfo] = []
    status: SessionStatus = SessionStatus.PENDING
    is_shared: bool = False  # Whether this session is shared publicly
    share_files: bool = False  # Explicit opt-in for public file metadata/URLs
    share_expires_at: Optional[datetime] = None

    def get_last_plan(self) -> Optional[Plan]:
        """Return the latest plan snapshot with later step events replayed.

        PlanEvent stores the plan structure while StepEvent stores mutable
        execution state. Resume/replay must combine both streams; returning
        only the last PlanEvent can resurrect a completed or waiting step as
        ``pending``.
        """
        plan_index: Optional[int] = None
        plan: Optional[Plan] = None
        for index in range(len(self.events) - 1, -1, -1):
            event = self.events[index]
            if isinstance(event, PlanEvent):
                plan_index = index
                plan = event.plan.model_copy(deep=True)
                if event.status == PlanStatus.COMPLETED:
                    plan.status = ExecutionStatus.COMPLETED
                break

        if plan is None or plan_index is None:
            return None

        steps_by_id = {step.id: step for step in plan.steps}
        for event in self.events[plan_index + 1 :]:
            if not isinstance(event, StepEvent):
                continue
            step = steps_by_id.get(event.step.id)
            if step is None:
                step = event.step.model_copy(deep=True)
                plan.steps.append(step)
                steps_by_id[step.id] = step
            else:
                # Preserve the latest execution metadata emitted by the
                # executor while retaining the plan's stable description.
                step.result = event.step.result
                step.error = event.step.error
                step.success = event.step.success
                step.attachments = list(event.step.attachments)

            if event.status == StepStatus.STARTED:
                step.status = ExecutionStatus.RUNNING
            elif event.status == StepStatus.COMPLETED:
                step.status = ExecutionStatus.COMPLETED
                step.success = True
            elif event.status == StepStatus.FAILED:
                step.status = ExecutionStatus.FAILED
                step.success = False

        return plan
