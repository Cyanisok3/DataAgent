"""HTTP 与 SSE 适配；同步工作线程独占会话直到执行及收尾完成。"""

import asyncio
import json
import queue
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from event_logger import usage_stats
from run_context import RunCancelled
from session_runner import run_session
from session_store import init_db


@asynccontextmanager
async def lifespan(app):
    init_db()
    yield


app = FastAPI(title="Data Agent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=50000)
    session_id: str = Field(
        default_factory=lambda: uuid4().hex, min_length=1, max_length=128
    )


async def events(req: ChatRequest, request: Request):
    channel: queue.Queue = queue.Queue(maxsize=64)
    cancel = threading.Event()
    finished = threading.Event()

    def publish(event):
        while not cancel.is_set():
            try:
                channel.put(event, timeout=0.1)
                return
            except queue.Full:
                continue
        raise RunCancelled("client_disconnected")

    def worker():
        try:
            run_session(req.session_id, req.message, publish, cancel)
        finally:
            finished.set()

    thread = threading.Thread(target=worker, name="dataagent-turn", daemon=True)
    thread.start()
    try:
        while not finished.is_set() or not channel.empty():
            if await request.is_disconnected():
                break
            try:
                event = channel.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.02)
                continue
            yield dict(event, session_id=req.session_id)
    finally:
        # 信号经模型流检查、SQLite progress handler 传播；锁由 worker 保持至收尾。
        cancel.set()


@app.get("/")
def root():
    return {"status": "ok", "endpoints": ["/chat", "/chat/stream", "/usage"]}


@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    answer, trace, status = "", [], "failed"
    error = "missing_terminal"
    async for event in events(req, request):
        if event["type"] in {"text", "text_chunk"}:
            answer += event["content"]
        elif event["type"] == "done":
            status, error = event["status"], event.get("error")
        else:
            trace.append(event)
    return {
        "answer": answer,
        "trace": trace,
        "status": status,
        "error": error,
        "session_id": req.session_id,
    }


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    async def stream():
        async for event in events(req, request):
            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/usage")
def usage(session_id: str):
    return usage_stats(session_id)
