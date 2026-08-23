from typing import AsyncGenerator, Optional, List
from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.models.file import FileInfo
from app.domain.models.message import Message, VisionImage
from app.domain.services.agents.base import BaseAgent
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.services.prompts.system import SYSTEM_PROMPT
from app.domain.services.prompts.execution import (
    EXECUTION_SYSTEM_PROMPT,
    EXECUTION_PROMPT,
    SUMMARIZE_PROMPT,
    SUMMARIZE_STREAM_PROMPT,
)
from app.domain.models.event import (
    BaseEvent,
    StepEvent,
    StepStatus,
    ErrorEvent,
    MessageEvent,
    MessageChunkEvent,
    DoneEvent,
    ToolEvent,
    ToolStatus,
    WaitEvent,
)
from app.domain.services.tools.base import BaseToolkit
from langchain.messages import HumanMessage as LCHumanMessage
import json
import logging

logger = logging.getLogger(__name__)


class ExecutionAgent(BaseAgent):
    """
    Execution agent — responsible for executing a single plan step using tools,
    then summarising the full task result for the user.
    """

    name: str = "execution"
    system_prompt: str = SYSTEM_PROMPT + EXECUTION_SYSTEM_PROMPT
    format: Optional[str] = None

    def __init__(
        self,
        agent_id: str,
        agent_repository: AgentRepository,
        tools: List[BaseToolkit],
    ):
        super().__init__(
            agent_id=agent_id,
            agent_repository=agent_repository,
            tools=tools,
        )

    def _build_vision_content(self, text: str, images: List[VisionImage]) -> list:
        content = [{"type": "text", "text": text}]
        for img in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{img.content_type};base64,{img.data}"},
            })
        return content

    async def _handle_execution_events(
        self, step: Step, content
    ) -> AsyncGenerator[BaseEvent, None]:
        """Forward model/tool events and update step state from the final response.

        Tool choice, ordering, and narration remain decisions of the model. This
        handler only translates the model's terminal response into step state; the
        outer execute_step emits the single terminal StepEvent.
        """
        async for event in self.execute(content):
            if isinstance(event, ErrorEvent):
                logger.debug("Step %s tool error: %s", step.id, event.error)
            elif isinstance(event, MessageEvent):
                step.status = ExecutionStatus.COMPLETED
                parsed_response = await self._parse_json(event.message)
                if parsed_response is None:
                    step.success = False
                    step.result = event.message or "No result returned."
                    step.error = "LLM returned a non-JSON response."
                    return
                if isinstance(parsed_response, list):
                    step.success = True
                    step.result = json.dumps(parsed_response, ensure_ascii=False)
                    return
                try:
                    new_step = Step.model_validate(parsed_response)
                except Exception as val_err:
                    logger.warning("Step validation failed; keeping raw result: %s", val_err)
                    step.success = True
                    step.result = json.dumps(parsed_response, ensure_ascii=False)
                    return
                step.success = new_step.success
                step.result = new_step.result
                step.attachments = new_step.attachments
                return
            elif isinstance(event, ToolEvent):
                if event.function_name == "message_ask_user":
                    if event.status == ToolStatus.CALLING:
                        yield MessageEvent(
                            message=event.function_args.get("text", ""),
                            step_id=step.id,
                        )
                    elif event.status == ToolStatus.CALLED:
                        yield WaitEvent()
                        return
                    continue
                if (
                    event.function_name == "message_notify_user"
                    and event.status == ToolStatus.CALLING
                ):
                    raw_att = event.function_args.get("attachments")
                    if raw_att:
                        att_list = [raw_att] if isinstance(raw_att, str) else list(raw_att)
                        att_list = [p for p in att_list if p]
                        if att_list:
                            yield MessageEvent(
                                message=event.function_args.get("text", ""),
                                attachments=[FileInfo(file_path=p) for p in att_list],
                                step_id=step.id,
                            )
                            continue
            yield event

    async def execute_step(
        self, plan: Plan, step: Step, message: Message
    ) -> AsyncGenerator[BaseEvent, None]:
        user_home = getattr(self, "user_home", None) or getattr(
            self, "_user_home", None
        ) or "/home/runner"
        prompt = EXECUTION_PROMPT.format(
            step=step.description,
            message=message.message,
            attachments="\\n".join(message.attachments),
            language=plan.language,
            user_home=user_home,
        )

        vision_content = None
        if message.vision_images:
            vision_content = self._build_vision_content(prompt, message.vision_images)

        step.status = ExecutionStatus.RUNNING
        yield StepEvent(status=StepStatus.STARTED, step=step)

        content = vision_content if vision_content else prompt
        try:
            async for event in self._handle_execution_events(step, content):
                yield event
                if isinstance(event, WaitEvent):
                    return
        except Exception as exc:
            error_text = str(exc).lower()
            if vision_content and any(
                marker in error_text
                for marker in ("image", "vision", "multimodal", "unsupported", "invalid request", "400")
            ):
                logger.warning("Model rejected image content; retrying text-only: %s", exc)
                async for event in self._handle_execution_events(step, prompt):
                    yield event
                    if isinstance(event, WaitEvent):
                        return
            else:
                raise

        step.status = ExecutionStatus.COMPLETED
        yield StepEvent(status=StepStatus.COMPLETED, step=step)

    def _extract_text_from_json(self, text: str) -> str:
        """
        If the LLM wrapped its streaming response in a JSON object
        (e.g. {"result": "..."} or {"message": "..."}), unwrap and return the
        inner text so the frontend receives clean Markdown, not raw JSON.
        """
        clean = text.strip()
        if clean.startswith("```"):
            import re
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", clean)
            if m:
                clean = m.group(1).strip()
        if not clean.startswith("{"):
            return text
        try:
            parsed = json.loads(clean)
            if isinstance(parsed, dict):
                extracted = parsed.get("result") or parsed.get("message")
                if extracted and isinstance(extracted, str):
                    return extracted
        except (json.JSONDecodeError, ValueError):
            pass
        return text

    async def _decide_and_create_summary_file(
        self,
        summary_text: str,
        context: list,
    ) -> List[FileInfo]:
        """
        Ask the LLM (without modifying memory) whether this task involved
        internet research. If yes, write the summary as a .md file directly
        via the sandbox and return its FileInfo.
        """
        from app.domain.services.tools.file import FileToolkit

        file_toolkit = next(
            (tk for tk in self.toolkits if isinstance(tk, FileToolkit)), None
        )
        if not file_toolkit:
            return []

        DECIDE_PROMPT = (
            "Answer ONLY in compact JSON, no extra text.\n"
            "Was the task you just completed an internet research or information-gathering task "
            "(web browsing, search results, Wikipedia, news articles, any data fetched from online URLs)?\n"
            'If YES: {"research":true,"filename":"summary_<topic>.md"} '
            "— use a short descriptive topic name, ASCII-safe, no spaces, same language root as the task.\n"
            'If NO:  {"research":false,"filename":""}'
        )
        decide_context = context + [LCHumanMessage(content=DECIDE_PROMPT)]

        try:
            response = await self._model.ainvoke(decide_context)
            raw = response.content if isinstance(response.content, str) else ""
            raw = raw.strip()
            if raw.startswith("```"):
                raw = "\n".join(raw.split("\n")[1:]).rstrip("`").strip()

            data = json.loads(raw)
            if not data.get("research") or not data.get("filename"):
                logger.debug("Summary file skipped: not a research task")
                return []

            filename = str(data["filename"]).strip().lstrip("/")
            filename = filename.replace("..", "").replace("/", "_")
            if not filename.endswith(".md"):
                filename += ".md"

            sandbox_home = getattr(
                file_toolkit.sandbox, "_sandbox_home", "/home/runner"
            )
            sandbox_path = f"{sandbox_home}/{filename}"

            await file_toolkit.sandbox.file_write(
                file=sandbox_path,
                content=summary_text,
                append=False,
                leading_newline=False,
                trailing_newline=True,
                sudo=False,
            )
            logger.info("Research summary .md saved: %s", sandbox_path)
            return [FileInfo(file_path=sandbox_path)]

        except Exception as exc:
            logger.warning("Could not create summary .md file: %s", exc)
            return []

    async def summarize(self) -> AsyncGenerator[BaseEvent, None]:
        await self._ensure_memory()
        context = list(self.memory.get_messages())

        stream_context = context + [LCHumanMessage(content=SUMMARIZE_STREAM_PROMPT)]

        full_text = ""
        try:
            async for chunk in self._model.astream(stream_context):
                token = chunk.content if isinstance(chunk.content, str) else ""
                if token:
                    full_text += token

            if full_text:
                clean_text = self._extract_text_from_json(full_text)
                # Emit in small chunks for a smooth progressive typing effect
                _CHUNK = 5
                for _i in range(0, len(clean_text), _CHUNK):
                    yield MessageChunkEvent(
                        content=clean_text[_i : _i + _CHUNK], done=False
                    )
                yield MessageChunkEvent(content="", done=True)

                # Let the LLM decide whether a .md summary file is appropriate
                attachments = await self._decide_and_create_summary_file(
                    clean_text, context
                )
                yield MessageEvent(
                    message=clean_text,
                    attachments=attachments if attachments else None,
                )
            return

        except Exception as e:
            logger.warning(
                f"Streaming summarize failed, falling back to JSON mode: {e}"
            )

        # Fallback: JSON-based summarize
        async for event in self.execute(SUMMARIZE_PROMPT):
            if isinstance(event, MessageEvent):
                logger.debug(f"Execution agent summary: {event.message}")
                parsed_response = await self._parse_json(event.message)
                if parsed_response is None:
                    logger.warning(
                        "Summarize fallback returned non-JSON, using raw message"
                    )
                    yield MessageEvent(message=event.message)
                    continue
                msg_obj = Message.model_validate(parsed_response)
                attachments = [
                    FileInfo(file_path=fp) for fp in msg_obj.attachments
                ]
                yield MessageEvent(message=msg_obj.message, attachments=attachments)
                continue
            yield event
