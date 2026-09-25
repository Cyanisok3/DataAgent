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

from context import CONTEXT_TOOLS, KIND_CONTEXT, KIND_RESULT, tool_result_view
from llm import chat, chat_stream_final
from tools import TOOLS, ToolResult

MAX_ITERS = 10

# 模型可调用的全部工具名（统一管理，避免声明在 system prompt、
# 实现在 react_loop、注册在 tools.py 三处分散导致不一致）。
# read_result 不在 TOOLS 注册表中，因为它需要访问 full_history（外部状态），
# 不属于 tools.py 的纯函数层；但它的"名称"必须在这里统一声明，
# 这样工具名校验时 read_result 和其他工具一视同仁。
KNOWN_TOOL_NAMES: set[str] = set(TOOLS.keys()) | {"read_result"}


def run_react_stream(user_message: str, history: list[dict] | None = None,
                     full_history: list[dict] | None = None):
    """
    流式版：思考/工具调用是步骤级流式，最终回答是逐字流式
    history：投影后的历史（多轮上下文，含结果索引），传给 LLM
    full_history：完整日志（供 read_result 按需拉取历史查询结果）

    事件协议：
      thinking     模型思考（完整一段）
      tool_call    工具调用（name + input）
      tool_result  工具结果（name + input + output + is_error + error_type
                   + sql + elapsed_ms + invocation）
      text_chunk   最终回答逐字片段
      text         非流式兜底回答（max_iters / 异常）
      done         轮次终态（status + invocations）
    """
    messages = [{"role": "user", "content": user_message}]
    # 本轮成功执行的 SQL 依据：[(sql, result_text), ...]
    # 回答阶段传给模型，让它知道"查了什么、用了什么条件"
    sql_evidence: list[tuple[str, str]] = []
    invocation_idx = 0  # 本轮工具调用编号（从 1 开始）

    for i in range(MAX_ITERS):
        # 1. 想（一次性 JSON，因为要解析 tool/args）
        try:
            result = chat(messages, history)
        except Exception as e:
            yield {"type": "thinking",
                   "content": f"模型输出解析失败（{type(e).__name__}），已中止本轮"}
            yield {"type": "text",
                   "content": "抱歉，模型回复格式异常，请换个问法再试一次。"}
            yield {"type": "done", "status": "failed",
                   "error": f"parse_error: {type(e).__name__}",
                   "invocations": invocation_idx}
            return
        yield {"type": "thinking", "content": result["thought"]}

        # 2. 如果模型说要最终回答
        if "final" in result:
            # 最后一条事实性工具结果（统一视图），没有则为 None（纯追问场景）
            last_result = next(
                (m for m in reversed(messages)
                 if m["role"] == "tool" and m.get("kind") == KIND_RESULT),
                None
            )
            tool_output = (tool_result_view(last_result["content"], KIND_RESULT)
                           if last_result else None)
            # 逐字流式生成回答（带上本轮 SQL 依据 + 历史，追问时知道上文）
            for chunk in chat_stream_final(user_message, tool_output, history,
                                           sql_evidence):
                yield {"type": "text_chunk", "content": chunk}
            yield {"type": "done", "status": "completed",
                   "invocations": invocation_idx}
            return

        # 3. 做：调工具（统一调用入口：编号 + 耗时 + 异常兜底）
        tool_name = result["tool"]
        tool_args = result["args"]
        invocation_idx += 1
        yield {"type": "tool_call", "name": tool_name, "input": tool_args,
               "invocation": invocation_idx}

        t0 = time.time()
        # 工具名校验：统一用 KNOWN_TOOL_NAMES，避免 read_result 不在 TOOLS 中
        # 导致 KeyError 被吞成"执行异常"。未知工具返回清晰错误，模型可纠正。
        if tool_name not in KNOWN_TOOL_NAMES:
            tool_result = ToolResult(
                content=f"❌ 未知工具 '{tool_name}'。"
                        f"可用工具：{', '.join(sorted(KNOWN_TOOL_NAMES))}",
                is_error=True, error_type="unknown_tool")
        else:
            try:
                if tool_name == "read_result":
                    # L27：从**完整**历史中按 ID 拉取查询结果（不走 TOOLS 注册表，
                    # 因为需要访问 full_history——不属于 tools.py 的纯函数层）
                    rid = int(tool_args.get("result_id", 0))
                    found = next(
                        (m for m in (full_history or [])
                         if m["id"] == rid and m.get("kind") == KIND_RESULT),
                        None)
                    if found:
                        tool_result = ToolResult(
                            content=found["content"],
                            sql=found["content"].split("→ SQL: ")[1].split("]")[0]
                                if "→ SQL: " in found["content"] else None)
                    else:
                        tool_result = ToolResult(
                            content=f"❌ 未找到 result_id={rid} 的历史查询结果。"
                                    "请从索引列表中选择有效的 ID。",
                            is_error=True, error_type="not_found")
                else:
                    tool_result: ToolResult = TOOLS[tool_name]["fn"](**tool_args)
            except Exception as e:
                tool_result = ToolResult(
                    content=f"❌ 工具 {tool_name} 执行异常: {type(e).__name__}: {e}",
                    is_error=True, error_type="exception")
        elapsed_ms = round((time.time() - t0) * 1000)

        yield {"type": "tool_result", "name": tool_name,
               "input": tool_args, "output": tool_result.content,
               "is_error": tool_result.is_error,
               "error_type": tool_result.error_type,
               "sql": tool_result.sql,
               "elapsed_ms": elapsed_ms,
               "invocation": invocation_idx}

        # 收集本轮 SQL 依据（仅成功的 execute_sql；错误结果不进入依据）
        if tool_name == "execute_sql" and not tool_result.is_error:
            sql_evidence.append((tool_result.sql, tool_result.content))

        # 4. 塞回对话继续（带 kind：引导类 vs 事实类）
        kind = KIND_CONTEXT if tool_name in CONTEXT_TOOLS else KIND_RESULT
        messages.append({"role": "assistant",
                         "content": f"我决定调用 {tool_name}，参数 {tool_args}"})
        messages.append({"role": "tool", "content": tool_result.content,
                         "kind": kind})

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
