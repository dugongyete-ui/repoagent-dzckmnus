from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import json
import yaml
from typing import List, Optional, Dict, Any
import os
from pathlib import Path
import asyncio
import logging
import sys

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Message(BaseModel):
    role: str
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Message]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False

class ChatCompletionResponse(BaseModel):
    #id: str
    #object: str
    #created: int
    #model: str
    choices: List[Dict[str, Any]]

def load_mock_data():
    # Get mock data filename from environment variable, default to default.yaml
    mock_file = os.getenv("MOCK_DATA_FILE", "default.yaml")
    mock_file_path = Path(__file__).parent / "mock_datas" / mock_file

    with open(mock_file_path, 'r', encoding='utf-8') as f:
        logger.info(f"Loading mock data from {mock_file}")
        if mock_file.endswith('.json'):
            return json.load(f)
        else:
            return yaml.safe_load(f)

current_index = 0

@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(request: ChatCompletionRequest):
    global current_index
    mock_data = load_mock_data()

    # Planner acknowledgement is a separate plain-text request. Do not advance
    # the canned sequence so the next call still receives the plan response.
    request_text = " ".join(
        (message.content or "")
        for message in request.messages
        if message.content
    ).lower()
    # A single process can serve multiple browser sessions. Reset at the
    # planner boundary so a previous task's tool/final response cannot be
    # consumed as the next task's plan.
    if "you are now creating a plan based on" in request_text:
        current_index = 0
        logger.info("Reset index at planner boundary")

    if "brief acknowledgement" in request_text or "plain natural language only" in request_text:
        ack_response = {
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Baik, saya akan menyiapkan file tersebut dan memverifikasi hasilnya.",
                },
            }],
        }
        if not request.stream:
            return ack_response

        async def ack_stream():
            chunk = {
                "id": "mock-ack",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": request.model,
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": ack_response["choices"][0]["message"]["content"]},
                    "finish_reason": None,
                }],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(ack_stream(), media_type="text/event-stream")
    if not mock_data:
        current_index = 0
        logger.error("No mock data available")
        raise HTTPException(status_code=500, detail="No mock data available")

    if len(request.messages) == 2 and current_index > 1:
        current_index = 0
        logger.info("Reset index to 0")
    
    delay = float(os.getenv("MOCK_DELAY", "1"))
    if delay > 0:
        logger.debug(f"Applying mock delay of {delay} seconds")
        await asyncio.sleep(delay)
    
    response = mock_data[current_index]
    current_index = (current_index + 1) % len(mock_data)
    logger.info(f"Returning mock response {current_index}/{len(mock_data)}")

    if not request.stream:
        return response

    # ChatOpenAI's `astream` expects Server-Sent Events. Keep the mock
    # deterministic while emitting the same wire shape as an OpenAI stream.
    async def event_stream():
        message = (response.get("choices") or [{}])[0].get("message") or {}
        delta = {"role": "assistant"}
        if message.get("content") is not None:
            delta["content"] = message.get("content")
        if message.get("tool_calls"):
            delta["tool_calls"] = message.get("tool_calls")
        chunk = {
            "id": "mock-completion",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": request.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        done = {
            "id": "mock-completion",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": request.model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(done)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
