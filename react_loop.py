"""
react_loop.py —— ReAct 循环（流式版）

对应 L2 学的，但现在不是攒完结果再返回，
而是每一步 yield 一个事件——前端实时看到"想了什么、调了什么工具"。
"""
from llm import chat
from tools import TOOLS

MAX_ITERS = 5


def run_react(user_message: str) -> dict:
    """非流式版：攒完所有步骤再一次性返回（保留给 /chat 用）"""
    messages = [{"role": "user", "content": user_message}]
    trace = []

    for i in range(MAX_ITERS):
        result = chat(messages)
        trace.append({"type": "thinking", "content": result["thought"]})

        if "final" in result:
            trace.append({"type": "text", "content": result["final"]})
            return {"answer": result["final"], "trace": trace}

        tool_name = result["tool"]
        tool_args = result["args"]
        trace.append({"type": "tool_call", "name": tool_name, "input": tool_args})

        tool_fn = TOOLS[tool_name]["fn"]
        tool_output = tool_fn(**tool_args)

        trace.append({"type": "tool_result", "name": tool_name, "output": tool_output})

        messages.append({"role": "assistant", "content": f"我决定调 {tool_name}"})
        messages.append({"role": "tool", "content": tool_output})

    return {
        "answer": f"抱歉，已达最大思考轮数（{MAX_ITERS}）",
        "trace": trace,
    }


def run_react_stream(user_message: str):
    """
    流式版：每一步 yield 一个事件（generator）
    对应 L5 学的 ChatStreamEvent——前端实时看到过程
    """
    messages = [{"role": "user", "content": user_message}]

    for i in range(MAX_ITERS):
        # 1. 想
        result = chat(messages)
        yield {"type": "thinking", "content": result["thought"]}

        # 2. 有最终答案就返回
        if "final" in result:
            yield {"type": "text", "content": result["final"]}
            return

        # 3. 做：调工具
        tool_name = result["tool"]
        tool_args = result["args"]
        yield {"type": "tool_call", "name": tool_name, "input": tool_args}

        tool_fn = TOOLS[tool_name]["fn"]
        tool_output = tool_fn(**tool_args)

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
