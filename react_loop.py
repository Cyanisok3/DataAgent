"""有限 ReAct 循环：模型自主选工具与 SQL，代码校验动作及证据引用。"""

import time

from pydantic import ValidationError

from context import serialize
from llm import ContextLengthExceeded, OutputBudgetExhausted, chat, chat_stream_final
from run_context import CURRENT_RUN, RunCancelled
from tools import TOOLS, ToolResult

MAX_ITERS = 12


def _invoke_tool(name: str, args: dict, results: dict) -> ToolResult:
    if name not in TOOLS:
        return ToolResult(content=f"未知工具 {name}", error_type="unknown_tool")
    if "results" in args:
        return ToolResult(
            content="results 由会话注入，不能由模型覆盖", error_type="schema_error"
        )
    try:
        injected = (
            {"results": results} if name in {"read_result", "list_results"} else {}
        )
        return TOOLS[name]["fn"](**args, **injected)
    except RunCancelled:
        raise
    except ValidationError as exc:
        return ToolResult(
            content=serialize(exc.errors(include_input=False, include_url=False)),
            error_type="schema_error",
        )
    except Exception as exc:  # noqa: BLE001 — 工具边界
        return ToolResult(
            content=f"{type(exc).__name__}: {exc}", error_type="execution"
        )


def _answer_column_error(action, results) -> dict | None:
    """最终查询返回列必须与模型声明的答案列一致；不通过返回反馈事件让其重写。"""
    final_id = action.get("final_query_id")
    declared = [str(c).lower() for c in action.get("answer_columns", [])]
    if final_id is None or not declared:
        return None
    actual = [str(c).lower() for c in results[final_id].get("columns", [])]
    if declared == actual:
        return None
    return {
        "type": "tool_result", "name": "answer_guard",
        "input": {}, "output": (
            f"最终查询返回列 {actual} 与问题要求的答案列 {declared} 不符。"
            "请重写最终 SQL：只 SELECT 问题直接要求的列，去掉计数、姓名、关联键等辅助列，"
            "再用新的 final_query_id 与 answer_columns 重新作答。"),
        "result": None, "is_error": True, "error_type": "answer_columns_mismatch",
        "sql": None, "query": None, "cached": False,
        "invocation": 0, "elapsed_ms": 0,
    }


def _answer(action, user_message, history, results, messages, invocations):
    ids = action["evidence_ids"]
    if len(ids) != len(set(ids)) or any(id_ not in results for id_ in ids):
        yield {"type": "done", "status": "failed", "error": "invalid_evidence_ids"}
        return
    evidence = [results[id_] for id_ in ids]
    final_id = action.get("final_query_id")
    if final_id is not None and (final_id not in ids or not results[final_id].get("sql")):
        yield {"type": "done", "status": "failed", "error": "invalid_final_query_id"}
        return
    yield {
        "type": "answer_selected", "mode": action["mode"],
        "evidence_ids": ids, "final_query_id": final_id,
    }
    for chunk in chat_stream_final(
        user_message, evidence, history, messages, action["mode"]
    ):
        yield {"type": "text_chunk", "content": chunk}
    yield {
        "type": "done",
        "status": "completed",
        "mode": action["mode"],
        "evidence_ids": ids,
        "final_query_id": final_id,
        "invocations": invocations,
    }


def run_react_stream(user_message: str, history=None, results=None, **_):
    messages = [{"role": "user", "content": user_message}]
    results = dict(results or {})
    cache: dict[str, ToolResult] = {}
    invocations = 0
    try:
        for _step in range(MAX_ITERS):
            run = CURRENT_RUN.get()
            if run:
                run.check()
            action = chat(messages, history, results)
            yield {"type": "action", "action": action}
            yield {"type": "thinking", "content": action["thought"]}
            if "evidence_ids" in action:
                column_error = _answer_column_error(action, results)
                if column_error:
                    yield column_error
                    messages.append(
                        dict(column_error, role="tool", content=column_error["output"]))
                    continue
                yield from _answer(
                    action, user_message, history, results, messages, invocations
                )
                return
            name, args = action["tool"], action["args"]
            invocations += 1
            yield {
                "type": "tool_call",
                "name": name,
                "input": args,
                "invocation": invocations,
            }
            key = serialize([name, args])
            started = time.monotonic()
            # 只复用无会话变化的查询/元数据工具；分页索引需反映本轮新结果。
            reusable = name not in {"read_result", "list_results"}
            cached = reusable and key in cache
            result = cache[key] if cached else _invoke_tool(name, args, results)
            if reusable:
                cache[key] = result
            if result.result and name == "execute_sql" and not cached:
                result.result.update(
                    question=user_message, turn=run.turn if run else 0,
                    calibers=[{"input": m["input"], "content": m["content"]} for m in messages
                              if m.get("name") == "get_metric_caliber" and not m.get("is_error")])
                results[result.result["result_id"]] = result.result
                result.content = serialize(result.result)
            event = {
                "type": "tool_result",
                "name": name,
                "input": args,
                "output": result.content,
                "result": result.result,
                "is_error": result.is_error,
                "error_type": result.error_type,
                "sql": result.sql,
                "query": result.query,
                "cached": cached,
                "invocation": invocations,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
            yield event
            messages.append(dict(event, role="tool", content=result.content))
        yield {
            "type": "done",
            "status": "failed",
            "error": "max_iters",
            "invocations": invocations,
        }
    except RunCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — 轮次统一终态
        error = (
            "context_length_exceeded"
            if isinstance(exc, ContextLengthExceeded)
            else type(exc).__name__
        )
        yield {
            "type": "error",
            "error": error,
            "content": "决策输出预算耗尽，未得到完整动作；本轮未执行格式修复。"
            if isinstance(exc, OutputBudgetExhausted)
            else "本轮未完成，请检查错误类型或缩小问题范围。",
        }
        yield {"type": "done", "status": "failed", "error": error}
