"""同步执行器持有会话锁；HTTP 仅传递取消信号与已落库事件。"""

import threading
from contextlib import closing

from compaction import maybe_compress
from datasource import CURRENT_SOURCE, DataSource
from llm import ContextLengthExceeded
from react_loop import run_react_stream
from run_context import CURRENT_RUN, RunCancelled, RunContext
from session_store import (
    begin_turn,
    finish_turn,
    load_messages,
    load_results,
    mark_cancelling,
    save_event,
)

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def run_session(sid: str, message: str, publish, cancel: threading.Event,
                *, source: DataSource | None = None, run: RunContext | None = None):
    with _guard:
        lock = _locks.setdefault(sid, threading.Lock())
    while not lock.acquire(timeout=0.1):
        if cancel.is_set():
            return
    run = run or RunContext(session_id=sid, cancel=cancel)
    run.session_id, run.cancel = sid, cancel
    source_token = CURRENT_SOURCE.set(source or CURRENT_SOURCE.get())
    token = CURRENT_RUN.set(run)
    answer, terminal = "", None
    try:
        run.check()
        run.turn = begin_turn(sid, message)
        results = load_results(sid)
        maybe_compress(sid, run.turn, message, results)
        history = load_messages(sid)
        with closing(run_react_stream(message, history, results)) as stream:
            for event in stream:
                run.check()
                if event["type"] in {"text", "text_chunk"}:
                    answer += event["content"]
                if event["type"] == "done":
                    terminal = event
                    break
                save_event(sid, run.turn, event)
                publish(event)
        terminal = terminal or {
            "type": "done",
            "status": "failed",
            "error": "missing_terminal",
        }
    except RunCancelled:
        terminal = {
            "type": "done",
            "status": "cancelled",
            "error": "client_disconnected",
        }
    except Exception as exc:  # noqa: BLE001 — 保留部分回答，失败不可伪装完成
        error = (
            "context_length_exceeded"
            if isinstance(exc, ContextLengthExceeded)
            else type(exc).__name__
        )
        terminal = {"type": "done", "status": "failed", "error": error}
    finally:
        try:
            if run.turn:
                if cancel.is_set():
                    mark_cancelling(sid, run.turn)
                terminal = terminal or {
                    "type": "done",
                    "status": "failed",
                    "error": "runner_interrupted",
                }
                if cancel.is_set():
                    terminal = {
                        "type": "done",
                        "status": "cancelled",
                        "error": "client_disconnected",
                    }
                finish_turn(
                    sid,
                    run.turn,
                    terminal["status"],
                    answer,
                    terminal.get("error"),
                    terminal.get("mode", "answer"),
                    evidence=terminal,
                )
                if not cancel.is_set():
                    publish(terminal)
        finally:
            CURRENT_RUN.reset(token)
            CURRENT_SOURCE.reset(source_token)
            lock.release()
