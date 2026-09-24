"""
react_loop.py —— ReAct 循环（流式版，事件流唯一输出协议）

L21 清理：删掉非流式 run_react（/chat 聚合也走 run_react_stream），
事件流是唯一输出形态——前端实时看到"想了什么、调了什么工具"。
"""
from llm import chat
from llm import chat_stream_final
from tools import TOOLS

MAX_ITERS = 10


def run_react_stream(user_message: str, history: list[dict] | None = None):
    """
    流式版：思考/工具调用是步骤级流式，最终回答是逐字流式
    history：投影后的历史骨架（多轮上下文），传给 LLM
    """
    messages = [{"role": "user", "content": user_message}]

    for i in range(MAX_ITERS):
        # 1. 想（一次性 JSON，因为要解析 tool/args）
        try:
            result = chat(messages, history)
        except Exception as e:
            yield {"type": "thinking",
                   "content": f"模型输出解析失败（{type(e).__name__}），已中止本轮"}
            yield {"type": "text",
                   "content": "抱歉，模型回复格式异常，请换个问法再试一次。"}
            return
        yield {"type": "thinking", "content": result["thought"]}

        # 2. 如果模型说要最终回答
        if "final" in result:
            # 拿到工具结果（最后一条 tool 消息）
            tool_output = next(
                (m["content"] for m in reversed(messages) if m["role"] == "tool"),
                None
            )
            # 逐字流式生成回答（带上历史骨架，追问时知道上文）
            for chunk in chat_stream_final(user_message, tool_output, history):
                yield {"type": "text_chunk", "content": chunk}
            return

        # 3. 做：调工具（异常兜底，转成可读结果，不中断流）
        tool_name = result["tool"]
        tool_args = result["args"]
        yield {"type": "tool_call", "name": tool_name, "input": tool_args}

        try:
            tool_fn = TOOLS[tool_name]["fn"]
            tool_output = tool_fn(**tool_args)
        except Exception as e:
            tool_output = f"❌ 工具 {tool_name} 执行异常: {type(e).__name__}: {e}"

        yield {"type": "tool_result", "name": tool_name, "output": tool_output}

        # 4. 塞回对话继续
        messages.append({"role": "assistant", "content": f"我决定调 {tool_name}"})
        messages.append({"role": "tool", "content": tool_output})

    yield {"type": "text", "content": f"已达最大思考轮数（{MAX_ITERS}）"}



# 自测
if __name__ == "__main__":
    import json
    print("=== 流式版测试：逐事件打印 ===")
    for event in run_react_stream("帮我查一下各区域的销售额"):
        print(json.dumps(event, ensure_ascii=False))
