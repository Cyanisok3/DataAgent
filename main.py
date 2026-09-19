"""
main.py —— FastAPI 入口（含 SSE 流式 + 会话持久化）

两个接口：
  POST /chat          → 非流式
  POST /chat/stream   → SSE 流式

请求体带 session_id，系统自动加载历史对话。
"""
import json
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from react_loop import run_react, run_react_stream
from session_store import init_db, save_message, load_messages
from db import init_db as init_business_db

app = FastAPI(title="Data Agent Demo", version="0.4")

# CORS：允许前端 3000 端口跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 启动时建表
@app.on_event("startup")
def startup():
    init_db()           # 会话消息表
    init_business_db() # 业务数据表（orders）


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"  # 默认会话，多轮对话时传同一个 id


@app.get("/")
def root():
    return {"status": "ok", "endpoints": ["/chat", "/chat/stream"]}


@app.post("/chat")
def chat(req: ChatRequest):
    # 1. 加载历史消息
    history = load_messages(req.session_id)
    # 2. 把当前用户消息加进去
    history.append({"role": "user", "content": req.message})
    # 3. 跑 ReAct
    result = run_react(req.message)  # 简化版：先不把历史传给 LLM
    # 4. 存到数据库
    save_message(req.session_id, "user", req.message)
    save_message(req.session_id, "assistant", result["answer"])
    return result


@app.post("/chat/stream")
def chat_stream(req: ChatRequest):
    """SSE 流式：逐步推送事件"""
    def event_generator():
        # 存用户消息
        save_message(req.session_id, "user", req.message)

        # 收集 assistant 的最终回答
        final_answer = ""

        for event in run_react_stream(req.message):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event["type"] == "text":
                final_answer = event["content"]

        # 存 assistant 回复
        if final_answer:
            save_message(req.session_id, "assistant", final_answer)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
