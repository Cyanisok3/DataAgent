"""
llm.py —— 接 DeepSeek（兼容 OpenAI API）

策略：不用 function calling 协议，而是在 system prompt 里告诉模型
"你必须返回 JSON 格式"，我们解析 JSON。
这样更简单可控，零基础好理解。

结构（职责分层，避免两个 chat 各自重复）：
  _resolve_context()   从当前轮 + 投影历史里取出 (用户问题, 最近工具结果)
  _build_messages()    组装 API 消息（derive：日志视角 → 模型视角）
  _completion()        唯一 API 调用入口（stream=False 一次性 / True 生成器）
  chat()               决策阶段：非流式（必须拿完整 JSON 才能解析 tool/args）
  chat_stream_final()  回答阶段：流式（人读的文本，逐字输出）

为什么是两个函数而不是一个：
  决策需要结构化 JSON → 必须等完整响应；回答需要逐字体验 → 必须流式。
  两者语义不同，但共享"组装 + 调用"，所以抽公共层，而不是合并成一个。
"""
import json
from openai import OpenAI

# 读 API key（从文件读，不硬编码）
with open("api_key.txt") as f:
    API_KEY = f.read().strip()

# DeepSeek 兼容 OpenAI 接口，只需换 base_url
client = OpenAI(
    api_key=API_KEY,
    base_url="https://api.deepseek.com/v1",  # DeepSeek 的 OpenAI 兼容端点
)

MODEL = "deepseek-flash"

# System prompt：告诉模型工具列表和输出格式
SYSTEM_PROMPT = """你是一个数据查询助手。你可以调用以下工具：

1. get_context(question: str) —— 根据用户问题，匹配相关的数据域、表和指标元数据
2. execute_sql(sql: str) —— 执行 SELECT SQL 查询数据库

规则：
- 收到用户问题后，先调 get_context 了解可用的表和指标
- 根据 get_context 返回的元数据，写正确的 SQL，再调 execute_sql
- 拿到 SQL 结果后，直接总结回答，不要再调工具
- 如果 execute_sql 返回以 ❌ 开头的错误，说明 SQL 有问题：
  根据错误信息和列名提示修正 SQL 后重试，最多重试 2 次；
  仍失败就用已有的信息直接回答用户，不要重复生成同样的 SQL

你必须严格按以下 JSON 格式回复（不要加任何其他文字、不要用 markdown 代码块）：
- 想调工具时：{"thought": "你的思考", "tool": "工具名", "args": {"参数名": "参数值"}}
- 想直接回答时：{"thought": "你的思考", "final": "最终回答"}
"""

# 最终回答阶段的 system prompt：纯总结，不要 JSON、不要思考过程
FINAL_SYSTEM_PROMPT = (
    "你是数据查询助手。根据工具返回的结果，用简洁清晰的语言总结回答用户，"
    "直接输出回答内容，不要加思考过程。"
)


# ---------- 公共层 ----------

def _resolve_context(messages: list[dict],
                     history: list[dict] | None) -> tuple[str, str | None]:
    """
    从当前轮消息 + 投影历史里取出 (用户问题, 最近工具结果)。
    工具结果优先看当前轮（ReAct 循环内刚调完），再看历史投影（跨轮追问）。
    """
    user_msg = next(
        m["content"] for m in reversed(messages) if m["role"] == "user"
    )
    tool_output = next(
        (m["content"] for m in reversed(messages) if m["role"] == "tool"), None
    )
    if tool_output is None and history:
        tool_output = next(
            (m["content"] for m in reversed(history) if m["role"] == "tool"), None
        )
    return user_msg, tool_output


def _history_text(history: list[dict] | None) -> str:
    """把投影骨架翻译成文本（供最终回答阶段拼上下文）"""
    if not history:
        return ""
    skeleton = [m for m in history if m["role"] in ("user", "assistant")]
    if not skeleton:
        return ""
    lines = [
        f"{'用户' if m['role'] == 'user' else '助手'}：{m['content']}"
        for m in skeleton
    ]
    return "之前的对话：\n" + "\n".join(lines) + "\n\n"


def _completion(api_messages: list[dict], stream: bool = False):
    """唯一 API 调用入口：stream=False 一次性返回；True 返回逐字生成器"""
    return client.chat.completions.create(
        model=MODEL,
        messages=api_messages,
        temperature=0,  # 贪心解码，输出稳定
        stream=stream,
    )


def _parse_json(content: str) -> dict:
    """解析模型返回的 JSON；容错：剥掉可能的 markdown 代码块"""
    content = content.strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    return json.loads(content)


# ---------- 两个语义不同的公开函数 ----------

def chat(messages: list[dict], history: list[dict] | None = None) -> dict:
    """
    决策阶段（非流式）：返回结构化 JSON，供 ReAct 循环解析工具调用。
    输入：当前轮消息 + 投影后的历史（可选）
    输出：{"thought":..., "tool":..., "args":...} 或 {"thought":..., "final":...}
    """
    user_msg, tool_output = _resolve_context(messages, history)

    # derive：投影骨架里的 user/assistant 多轮直传（合法角色）；
    # 工具结果不能以 role=tool 直传（OpenAI 协议要求 tool_call_id 配对），
    # 翻译成文本拼进当前 user_content——JSON 协议下的标准做法。
    api_messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        api_messages.extend(
            m for m in history if m["role"] in ("user", "assistant")
        )

    if tool_output:
        user_content = (
            f"用户问题：{user_msg}\n\n"
            f"上一步工具返回：\n{tool_output}\n\n"
            f"请基于以上信息决定下一步：调工具或直接回答。"
        )
    else:
        user_content = user_msg
    api_messages.append({"role": "user", "content": user_content})

    # JSON 解析带一次错误反馈重试：
    # 模型偶尔会输出非 JSON（典型：超短追问时直接回"好的"），
    # 把解析错误反馈给它，要求重新输出严格 JSON——agent 工程常规做法。
    for attempt in range(2):
        resp = _completion(api_messages)
        content = resp.choices[0].message.content
        try:
            return _parse_json(content)
        except json.JSONDecodeError:
            if attempt == 1:
                raise
            api_messages.append({
                "role": "user",
                "content": ("你上一次回复不是合法 JSON。请严格只输出以下两种格式之一，"
                            "不要任何其他文字或 markdown："
                            '{"thought": "思考", "tool": "工具名", "args": {...}} '
                            '或 {"thought": "思考", "final": "回答"}'),
            })


def chat_stream_final(user_message: str, tool_output: str,
                      history: list[dict] | None = None):
    """
    回答阶段（流式）：逐字生成最终回答，供前端实时渲染。
    不用 JSON 协议，直接生成自然语言。
    """
    api_messages = [
        {"role": "system", "content": FINAL_SYSTEM_PROMPT},
        {"role": "user",
         "content": f"{_history_text(history)}用户问题：{user_message}\n\n"
                    f"工具返回结果：\n{tool_output}"},
    ]
    for chunk in _completion(api_messages, stream=True):
        if chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


# 自测
if __name__ == "__main__":
    print("=== 测试：问'各区域销售额' ===")
    r = chat([{"role": "user", "content": "各区域销售额是多少？"}])
    print(json.dumps(r, ensure_ascii=False, indent=2))
