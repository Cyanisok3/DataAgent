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
from session_store import (
    init_db, save_message, load_messages, project_history,
    KIND_CONTEXT, KIND_RESULT,
)
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
    # 空消息校验：不落库、不调 LLM（省一次 API 调用）
    if not req.message.strip():
        return {"answer": "请先输入想问的问题。", "trace": []}
    # 1. 加载全量日志 + 投影出"该给模型看什么"（多轮上下文骨架）
    projected = project_history(load_messages(req.session_id))
    # 2. 跑 ReAct（历史骨架传给 LLM，模型记得之前聊过什么）
    result = run_react(req.message, projected)
    # 3. 本次对话追加进日志（只追加，永不删）
    save_message(req.session_id, "user", req.message)
    save_message(req.session_id, "assistant", result["answer"])
    return result


@app.post("/chat/stream")
def chat_stream(req: ChatRequest):
    """SSE 流式：逐步推送事件"""
    def event_generator():
        # 空消息校验：直接返回友好提示，不调 LLM
        if not req.message.strip():
            yield ("data: " + json.dumps(
                {"type": "text", "content": "请先输入想问的问题。"},
                ensure_ascii=False) + "\n\n")
            return
        # 投影：旧历史给模型看；新用户消息稍后再追加进日志
        projected = project_history(load_messages(req.session_id))
        # 存用户消息（写入日志）
        save_message(req.session_id, "user", req.message)

        # 收集 assistant 的最终回答
        final_answer = ""

        for event in run_react_stream(req.message, projected):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event["type"] == "text_chunk":
                final_answer += event["content"]   # 逐字拼接
            elif event["type"] == "tool_result":
                # 工具结果落库，并声明 kind（判断前置到写入时）：
                #   get_context 元数据 = 引导性（context），消费完即弃
                #   execute_sql 结果 = 事实性（result），可被追问引用
                kind = KIND_CONTEXT if event["name"] == "get_context" \
                    else KIND_RESULT
                save_message(req.session_id, "tool", event["output"], kind)
            elif event["type"] == "text":          # max_iters 兜底分支
                final_answer = event["content"]

        # 存 assistant 回复
        if final_answer:
            save_message(req.session_id, "assistant", final_answer)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
