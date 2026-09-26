"""
main.py —— FastAPI 入口（SSE 流式 + 会话持久化 + 预算化压缩）

三个接口：
  POST /chat          → 非流式（聚合 run_react_stream 的事件）
  POST /chat/stream   → SSE 流式
  POST /usage         → 前端水位条（投影用量构成）

L24 重构：本文件回归纯 HTTP 层——
  投影（context.py）与压缩编排（compaction.py）各自独立成模块。
L21：_session_pipeline 唯一编排——/chat 与 /chat/stream 走同一套
     （投影 → next_turn → 落 user → 流式事件 → 落 tool/assistant → 后台压缩），
     两个入口保存的事件完全一致（P1-2）。
L22：per-session 压缩锁（防并发双摘要）+ 压缩移入后台 daemon 线程（不阻塞 SSE）。
"""
import json
import threading

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from react_loop import run_react_stream
from context import project_history
from session_store import (
    init_db, save_message, load_messages, next_turn, usage_stats,
    latest_llm_usage,
)
from compaction import maybe_compress
from db import init_db as init_business_db
from llm import system_prompt_tokens
from event_logger import log_tool_result, log_invocation

app = FastAPI(title="Data Agent", version="0.8")

# CORS：允许前端 3000 端口跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# L28：同会话请求串行化锁（防止两个请求交错写 turn / 消息）
_session_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _get_session_lock(session_id: str) -> threading.Lock:
    """获取指定会话的锁（不存在则创建，线程安全）。"""
    with _locks_guard:
        if session_id not in _session_locks:
            _session_locks[session_id] = threading.Lock()
        return _session_locks[session_id]


# 启动时建表
@app.on_event("startup")
def startup():
    init_db()           # 会话消息表
    init_business_db()  # 业务数据表（orders）


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"  # 默认会话，多轮对话时传同一个 id


def _session_pipeline(session_id: str, message: str):
    """
    唯一编排（L21）：所有请求都走这一条流水线，事件流是唯一输出协议。
      projected  = 请求开始时的投影（不含本轮）
      turn       = next_turn：user 开新轮，本轮 tool/assistant 沿用
      落库顺序：user → tool（按 kind 声明）→ assistant（流式拼接）
      压缩：事件流结束前启动后台 daemon 线程（SSE 立即关闭，L22 P2-2）

    L28：同会话串行化（调用方持锁）；try/finally 保证客户端断开时
         assistant 消息和压缩不丢失；run_react_stream 传 session_id/turn
         供 LLM usage 记录关联。
    """
    full_history = load_messages(session_id)
    projected = project_history(full_history)
    turn = next_turn(session_id)
    save_message(session_id, "user", message, turn=turn)

    final_answer = ""
    cancelled = False
    try:
        for event in run_react_stream(message, projected, full_history,
                                       session_id=session_id, turn=turn):
            if event["type"] == "text_chunk":
                final_answer += event["content"]
            elif event["type"] == "tool_result":
                # L29：落库与控制台日志抽到 event_logger.py，main.py 只编排
                log_tool_result(event, session_id, turn)
                log_invocation(event)
            elif event["type"] == "text":          # max_iters 兜底分支
                final_answer = event["content"]
            elif event["type"] == "done":
                # 轮次终态日志（不进 trace，不进 SSE 之外的存储）
                print(f"[轮次结束] status={event['status']} "
                      f"invocations={event.get('invocations', 0)} "
                      f"session={session_id}")
            yield event
    except GeneratorExit:
        # L28：客户端断开（SSE 连接关闭）——记录 cancelled，finally 仍会落库
        cancelled = True
        print(f"[轮次结束] status=cancelled (client disconnected) "
              f"session={session_id}")
        raise
    finally:
        # L28：try/finally 保证——即使客户端断开，assistant 消息和压缩也不丢失
        if final_answer:
            save_message(session_id, "assistant", final_answer, turn=turn)
        if cancelled:
            print(f"[清理] 客户端断开，已落库 assistant（{len(final_answer)} 字）"
                  f" session={session_id}")
        # 压缩交给后台线程：事件流立即结束 → SSE 连接马上关闭（daemon 不拦进程退出）
        threading.Thread(target=maybe_compress, args=(session_id,), daemon=True).start()


@app.get("/")
def root():
    return {"status": "ok", "endpoints": ["/chat", "/chat/stream", "/usage"]}


@app.post("/chat")
def chat(req: ChatRequest):
    """非流式：聚合 _session_pipeline 的全部事件，返回 answer + trace"""
    if not req.message.strip():
        return {"answer": "请先输入想问的问题。", "trace": []}
    answer, trace = "", []
    # L28：同会话串行化——防止两个请求交错写 turn/消息
    with _get_session_lock(req.session_id):
        for event in _session_pipeline(req.session_id, req.message):
            if event["type"] == "text_chunk":
                answer += event["content"]
            elif event["type"] == "text":
                answer = event["content"]
            if event["type"] not in ("text_chunk", "done"):
                trace.append(event)
    return {"answer": answer, "trace": trace}


@app.post("/chat/stream")
def chat_stream(req: ChatRequest):
    """SSE 流式：逐步推送事件（与 /chat 同一条流水线，事件完全一致）"""
    def event_generator():
        # 空消息校验：直接返回友好提示，不调 LLM
        if not req.message.strip():
            yield ("data: " + json.dumps(
                {"type": "text", "content": "请先输入想问的问题。"},
                ensure_ascii=False) + "\n\n")
            return
        # L28：同会话串行化
        with _get_session_lock(req.session_id):
            for event in _session_pipeline(req.session_id, req.message):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/usage")
def usage(session_id: str = "default"):
    """前端水位条：token 用量构成。
    L28：同时返回投影估算（参考）和最近一次真实 LLM usage（权威）。
    真实 usage 来自 llm_requests 表，不再是重新投影的估算值。"""
    stats = usage_stats(session_id)
    system_tokens = system_prompt_tokens()
    stats["system_tokens"] = system_tokens
    stats["projected_tokens"] += system_tokens
    # L28：最近一次真实 LLM 调用的 usage（替代"重算投影"作为权威用量）
    real = latest_llm_usage(session_id)
    stats["last_llm_call"] = real
    if real and real.get("total_tokens"):
        stats["actual_total_tokens"] = real["total_tokens"]
    return stats
