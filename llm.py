"""
llm.py —— 接 DeepSeek（兼容 OpenAI API）

策略：不用 function calling 协议，而是在 system prompt 里告诉模型
"你必须返回 JSON 格式"，我们解析 JSON。
这样更简单可控，零基础好理解。
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

你必须严格按以下 JSON 格式回复（不要加任何其他文字、不要用 markdown 代码块）：
- 想调工具时：{"thought": "你的思考", "tool": "工具名", "args": {"参数名": "参数值"}}
- 想直接回答时：{"thought": "你的思考", "final": "最终回答"}
"""


def chat(messages: list[dict]) -> dict:
    """
    输入：对话历史
    输出：{"thought":..., "tool":..., "args":...} 或 {"thought":..., "final":...}
    """
    # 提取用户原始问题和工具结果
    user_msg = next(
        m["content"] for m in reversed(messages) if m["role"] == "user"
    )

    # 构造 prompt：如果有工具结果，带上
    if messages[-1]["role"] == "tool":
        tool_output = messages[-1]["content"]
        user_content = (
            f"用户问题：{user_msg}\n\n"
            f"上一步工具返回：\n{tool_output}\n\n"
            f"请基于以上信息决定下一步：调工具或直接回答。"
        )
    else:
        user_content = user_msg

    # 调 DeepSeek API
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0,  # 贪心解码，输出稳定
    )

    # 解析模型返回的 JSON
    content = resp.choices[0].message.content.strip()

    # 容错：模型可能用 markdown 代码块包裹，剥掉
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]

    return json.loads(content)


# 自测
if __name__ == "__main__":
    print("=== 测试：问'各区域销售额' ===")
    r = chat([{"role": "user", "content": "各区域销售额是多少？"}])
    print(json.dumps(r, ensure_ascii=False, indent=2))
