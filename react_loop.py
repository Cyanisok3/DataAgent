"""
react_loop.py —— ReAct 循环（流式版，事件流唯一输出协议）

事件流是唯一输出形态——前端实时看到"想了什么、调了什么工具、返回了什么"。

L24 审计修复：
  - 工具消息带 kind（引导类/事实类），投影层据此决定是否跨轮保留
  - tool_result 事件携带 input（参数），落日志后可完整审计
  - 最终流式回答带上统一视图的工具结果（与决策阶段同口径）

L25：工具返回 ToolResult（结构化）；回答阶段传递本轮全部 SQL 依据
  （sql + 结果），解决"查过却无法确认时间范围/口径"。

L26：统一调用入口（编号+耗时）；done 终态事件；tool_result 携带
  is_error/error_type/sql/elapsed_ms，供落库标记错误结果与实际执行 SQL。

L27：结果索引 + 按需读取。run_react_stream 接收 full_history（完整日志），
  模型可调 read_result(result_id=ID) 从历史中拉取完整查询结果，
  替代"投影最后 N 条完整结果"的硬选择。
"""
import time
from typing import Annotated

from pydantic import ConfigDict, Field, ValidationError, validate_call

from context import CONTEXT_TOOLS, KIND_CONTEXT, KIND_ERROR, KIND_RESULT
from llm import chat, chat_stream_final
from tools import TOOLS, ToolResult

MAX_ITERS = 10

# read_result 由此处注入会话快照；其他工具保留现有函数注册表。
KNOWN_TOOL_NAMES: set[str] = set(TOOLS.keys()) | {"read_result"}


@validate_call(config=ConfigDict(strict=True))
def _read_result(result_id: Annotated[int, Field(gt=0)], *, history: list[dict]) -> ToolResult:
    """只读当前会话的结果快照；旧文本原样保留，不从 header 猜拆 SQL。"""
    found = next((m for m in history if m["id"] == result_id
                  and m.get("role") == "tool" and m.get("kind") == KIND_RESULT), None)
    if found:
        return ToolResult(content=found["content"])
    return ToolResult(content=f"未找到 result_id={result_id}，请使用当前会话索引中的 ID。",
                      is_error=True, error_type="not_found")


def _invoke_tool(name: str, args: dict, history: list[dict]) -> ToolResult:
    """唯一调用边界：参数校验失败不执行工具，所有工具错误有明确类型。"""
    if name not in KNOWN_TOOL_NAMES:
        return ToolResult(content=f"未知工具 {name}，可用工具：{sorted(KNOWN_TOOL_NAMES)}",
                          is_error=True, error_type="unknown_tool")
    try:
        if name == "read_result":
            # history 是代码注入的会话快照，不允许模型传入或覆盖。
            if "history" in args:
                return ToolResult(content="read_result 只接受 result_id。",
                                  is_error=True, error_type="schema_error")
            return _read_result(**args, history=history)
        return TOOLS[name]["fn"](**args)
    except ValidationError as e:
        return ToolResult(content=f"工具参数不合法：{e.errors(include_input=False, include_url=False)}",
                          is_error=True, error_type="schema_error")
    except Exception as e:  # noqa: BLE001 — 工具执行边界必须返回结构化错误
        return ToolResult(content=f"工具 {name} 执行异常：{type(e).__name__}: {e}",
                          is_error=True, error_type="exception")


def _stream_answer(user_message, history, evidence, invocation_idx):
    """回答只消费成功证据；流中断保留已发送片段并给出失败终态。"""
    try:
        for chunk in chat_stream_final(user_message, None, history, evidence):
            yield {"type": "text_chunk", "content": chunk}
    except Exception as e:  # noqa: BLE001 — 流式回答失败必须产生终态
        yield {"type": "done", "status": "failed", "error": f"final_error: {type(e).__name__}: {e}",
               "invocations": invocation_idx}
        return
    yield {"type": "done", "status": "completed", "invocations": invocation_idx}


def run_react_stream(user_message: str, history: list[dict] | None = None,
                     full_history: list[dict] | None = None,
                     session_id: str | None = None, turn: int = 0):
    """输出既有事件协议；history 为投影，full_history 为本会话只读结果快照。"""
    messages = [{"role": "user", "content": user_message}]
    # 成功查询或读取的依据；旧记录的 SQL 保留在原文中，不猜补结构化字段。
    sql_evidence: list[tuple[str | None, str]] = []
    invocation_idx = 0  # 本轮工具调用编号（从 1 开始）

    for i in range(MAX_ITERS):
        # 1. 想（一次性 JSON，因为要解析 tool/args）
        try:
            result = chat(messages, history, session_id=session_id, turn=turn)
        except Exception as e:  # noqa: BLE001 — 模型调用边界统一收尾
            yield {"type": "thinking",
                   "content": f"模型决策失败（{type(e).__name__}），已中止本轮"}
            yield {"type": "text",
                   "content": "抱歉，本轮未能完成模型决策，请稍后重试或缩小问题范围。"}
            yield {"type": "done", "status": "failed",
                   "error": f"decision_error: {type(e).__name__}: {e}",
                   "invocations": invocation_idx}
            return
        yield {"type": "thinking", "content": result["thought"]}

        # 2. 如果模型说要最终回答
        if "final" in result:
            yield from _stream_answer(user_message, history, sql_evidence, invocation_idx)
            return

        # 3. 做：调工具（统一调用入口：编号 + 耗时 + 异常兜底）
        tool_name = result["tool"]
        tool_args = result["args"]
        invocation_idx += 1
        yield {"type": "tool_call", "name": tool_name, "input": tool_args,
               "invocation": invocation_idx}

        t0 = time.perf_counter()
        tool_result = _invoke_tool(tool_name, tool_args, full_history or [])
        elapsed_ms = round((time.perf_counter() - t0) * 1000)

        yield {"type": "tool_result", "name": tool_name,
               "input": tool_args, "output": tool_result.content,
               "full_output": tool_result.full_content,
               "is_error": tool_result.is_error,
               "error_type": tool_result.error_type,
               "sql": tool_result.sql,
               "elapsed_ms": elapsed_ms,
               "invocation": invocation_idx}

        content = tool_result.full_content or tool_result.content
        # 历史读取与新查询走同一证据通道，保留全部已留档行，不仅前 20 行预览。
        if tool_name in ("execute_sql", "read_result") and not tool_result.is_error:
            sql_evidence.append((tool_result.sql, content))

        # 4. 塞回对话继续（带 kind：引导类 vs 事实类）
        kind = KIND_CONTEXT if tool_name in CONTEXT_TOOLS else KIND_RESULT
        messages.append({"role": "tool", "content": content, "name": tool_name,
                         "input": tool_args, "kind": KIND_ERROR if tool_result.is_error else kind})

    yield {"type": "text",
           "content": f"已达最大思考轮数（{MAX_ITERS}），未能得出最终结论。"}
    yield {"type": "done", "status": "max_iters",
           "invocations": invocation_idx}



# 自测
if __name__ == "__main__":
    import json
    print("=== 流式版测试：逐事件打印 ===")
    for event in run_react_stream("帮我查一下各门店的销售额"):
        print(json.dumps(event, ensure_ascii=False))
