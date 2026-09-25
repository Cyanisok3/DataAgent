"""
llm.py —— 接 DeepSeek（兼容 OpenAI API）

策略：不用 function calling 协议，而是在 system prompt 里告诉模型
"你必须返回 JSON 格式"，我们解析 JSON。
这样更简单可控，零基础好理解。

结构（职责分层，避免两个 chat 各自重复）：
  _tool_chain()     本轮 ReAct 循环内多步工具结果拼接（L21）
  _history_text()   投影骨架 → 文本（最终回答阶段拼上下文）
  _completion()     唯一 API 调用入口（stream=False 一次性 / True 生成器）
  chat()            决策阶段：非流式（必须拿完整 JSON 才能解析 tool/args）
  chat_stream_final() 回答阶段：流式（人读的文本，逐字输出）
  summarize_history() 压缩器：旧段 → 摘要（保留事实数字）

为什么是两个函数而不是一个：
  决策需要结构化 JSON → 必须等完整响应；回答需要逐字体验 → 必须流式。
  两者语义不同，但共享"组装 + 调用"，所以抽公共层，而不是合并成一个。
"""
import json
import sqlite3
from datetime import date, timedelta
from openai import OpenAI

from context import (
    KIND_RESULT,
    SUMMARY_MAX_CHARS,
    estimate_tokens,
    tool_result_view,
)

# 读 API key（从文件读，不硬编码）
with open("api_key.txt") as f:
    API_KEY = f.read().strip()

# DeepSeek 兼容 OpenAI 接口，只需换 base_url
client = OpenAI(
    api_key=API_KEY,
    base_url="https://api.deepseek.com/v1",  # DeepSeek 的 OpenAI 兼容端点
)

MODEL = "deepseek-flash"

# 决策阶段拼"本轮工具链"时最多回看几条（细粒度工具单条很短，可多看几步）
TOOL_CHAIN_MAX_ITEMS = 8

# System prompt 模板：日期信息每次调用实时注入（对齐原版 AgentService.systemPrompt() 设计）
# 注意 f-string 里 JSON 的大括号要写成 {{ }} 转义
_SYSTEM_PROMPT_TEMPLATE = """你是一个数据查询助手。你可以调用以下工具：

1. get_domains() —— 获取所有可用数据域
2. get_tables(question) —— 根据问题列出相关表（只给表名+描述）
3. get_table_schema(table_name) —— 获取单表完整列信息（列名+业务含义）
4. get_metric_caliber(hint) —— 获取指标口径（计算表达式、时间字段、过滤条件）
5. execute_sql(sql) —— 执行 SELECT 查询
6. read_result(result_id) —— 按 ID 读取历史查询结果的完整内容（历史结果以索引形式提供，需要完整数据时调用）

当前日期信息（时区 Asia/Shanghai）：
- 今天是 {today}；昨天是 {yesterday}；明天是 {tomorrow}
- 数据库中的订单数据最新到 {data_end}

工作流程（严格遵守）：
1. 用 get_tables 找到相关表；写 SQL 前必须用 get_table_schema 确认列名与时间字段
2. 涉及销售额/订单量/客单价/活跃客户数/销量等指标时，先调 get_metric_caliber
   获取标准口径，严格使用其表达式与过滤条件，不要自己发明算法
3. 写 SQL，调 execute_sql；拿到结果后直接总结回答，不再调工具
4. "今天/昨天/最近 N 天/上周/本月"等相对日期，按上面的当前日期换算
   （可直接用 CURRENT_DATE 计算）
5. execute_sql 返回以 ❌ 开头的错误时：调 get_table_schema 核对列名后重写，
   不要重复同样的错误 SQL；重试 2 次仍失败就基于已有信息回答
6. 不要重复调用目的与参数完全相同的工具；已拿到所需信息就直接进入下一步
7. 历史查询结果以索引列表形式提供（含 ID、SQL 摘要、行数、预览）。
   追问历史数据时，先看索引找到对应 result_id，再调 read_result(result_id=ID)
   拉取完整结果，不要重新执行同样的 SQL

你必须严格按以下 JSON 格式回复（不要加任何其他文字、不要用 markdown 代码块）：
- 想调工具时：{{"thought": "你的思考", "tool": "工具名", "args": {{"参数名": "参数值"}}}}
- 想直接回答时：{{"thought": "你的思考", "final": "最终回答"}}
"""


def _latest_order_time() -> str:
    """从 business.db 查订单数据最新时间——让模型知道数据覆盖范围，
    就不会把历史静态数据误判成"没有最近数据"。"""
    try:
        conn = sqlite3.connect("business.db")
        row = conn.execute("SELECT MAX(ordered_at) FROM orders").fetchone()
        conn.close()
        return row[0] if row and row[0] else "未知"
    except Exception:
        return "未知"


def _build_system_prompt() -> str:
    """实时注入当前日期（对齐原版：每次构建 agent 时 LocalDate.now(Asia/Shanghai)）"""
    today = date.today()
    return _SYSTEM_PROMPT_TEMPLATE.format(
        today=today,
        yesterday=today - timedelta(days=1),
        tomorrow=today + timedelta(days=1),
        data_end=_latest_order_time(),
    )


def system_prompt_tokens() -> int:
    """当前系统提示词的 token 估算（/usage 水位条用；日期每天变化故实时算）。"""
    return estimate_tokens(_build_system_prompt())

# 最终回答阶段的 system prompt：纯总结，不要 JSON、不要思考过程
# L24 证据约束：事实与推测分开，趋势/原因类结论必须有计算或数据支撑
# L25 数值格式化：金额/比率/百分比统一小数位，避免 15 位小数
FINAL_SYSTEM_PROMPT = (
    "你是数据查询助手。根据已有的数据与计算结果，用简洁清晰的语言回答用户。\n"
    "严格遵守：\n"
    "1. 只陈述已经计算或数据中明确存在的事实，具体数字保持原样\n"
    "2. 增长、下降、反超、趋势等判断，必须有对应计算支撑（等长前期对比、同比、"
    "按月序列等）；没有计算就明确写“需补充数据验证”，不得直接断言\n"
    "3. 原因解释（促销、旺季、活动等）必须有事件数据支持，否则标注为推测或不写\n"
    "4. 回答中事实、推测、建议分开呈现。直接输出回答，不要思考过程。\n"
    "5. 数值格式化：金额保留 2 位小数，百分比保留 1-2 位小数，"
    "客单价/比率保留 2 位小数；不要输出超过 4 位的小数。"
)


# ---------- 公共层 ----------

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


def _tool_chain(messages: list[dict]) -> str:
    """本轮 ReAct 循环内工具结果拼接（L25 重写）：
    复用 context.tool_result_view（截断口径唯一），并检测完全重复的返回——
    审计中模型反复拿到相同片段却继续同样的调用，此处显式警告。"""
    tools = [m for m in messages if m["role"] == "tool"]
    seen: set[str] = set()
    lines: list[str] = []
    duplicate = False
    for m in tools[-TOOL_CHAIN_MAX_ITEMS:]:
        view = tool_result_view(m["content"], m.get("kind", KIND_RESULT))
        if view in seen:
            duplicate = True
            continue
        seen.add(view)
        lines.append(f"- {view}")
    if not lines:
        return ""
    out = "本轮工具已返回：\n" + "\n".join(lines)
    if duplicate:
        out += ("\n⚠ 有工具返回与之前完全相同（无新增信息）：不要重复同样的调用，"
                "请换参数/工具，或基于已有信息直接回答。")
    return out


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

    L21：决策上下文 = 本轮工具链（多步，_tool_chain）+ 历史投影里的事实结果
         （hist_tools）——追问时模型能看到旧数字，而不是只看到本轮最后一步。
    """
    user_msg = next(
        m["content"] for m in reversed(messages) if m["role"] == "user"
    )

    # derive：投影骨架里的 user/assistant 多轮直传（合法角色）；
    # 工具结果不能以 role=tool 直传（OpenAI 协议要求 tool_call_id 配对），
    # 翻译成文本拼进当前 user_content——JSON 协议下的标准做法。
    api_messages: list[dict] = [{"role": "system", "content": _build_system_prompt()}]
    if history:
        api_messages.extend(
            m for m in history if m["role"] in ("user", "assistant")
        )

    parts = []
    chain = _tool_chain(messages)
    if chain:
        parts.append(chain)
    # L27：历史中的 tool 消息是合并的结果索引列表（一条），直接展示文本
    hist_tools = [m["content"] for m in (history or []) if m["role"] == "tool"]
    if hist_tools:
        parts.append("历史查询结果索引：\n" + "\n".join(hist_tools))

    user_content = f"用户问题：{user_msg}"
    if parts:
        user_content += "\n\n" + "\n\n".join(parts)
    user_content += "\n\n请基于以上信息决定下一步：调工具或直接回答。"
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
                      history: list[dict] | None = None,
                      sql_evidence: list[tuple[str, str]] | None = None):
    """
    回答阶段（流式）：逐字生成最终回答，供前端实时渲染。
    不用 JSON 协议，直接生成自然语言。

    L22 P1-1：当前轮工具结果 + 历史投影里的工具事实都要带上——
    追问（本轮无工具调用）时，模型仍能看到上一轮的具体数字。

    L25：sql_evidence 是本轮成功执行的 [(sql, result_text), ...]，
    优先于 tool_output（结构化依据包含 SQL + 结果，解决"查过却无法确认口径"）。
    纯追问场景（无工具调用）时 sql_evidence 为空，回退到 tool_output。
    """
    parts = []
    if sql_evidence:
        lines = [f"SQL: {sql}\n结果: {result}"
                 for sql, result in sql_evidence]
        parts.append("本轮查询依据（实际执行的 SQL 与结果）：\n"
                     + "\n\n".join(lines))
    elif tool_output:
        parts.append(f"工具返回结果：\n{tool_output}")
    # L27：历史中的 tool 消息是合并的结果索引列表，直接展示
    hist_tools = [m["content"] for m in (history or []) if m["role"] == "tool"]
    if hist_tools:
        parts.append("历史查询结果索引：\n" + "\n".join(hist_tools))

    body = f"{_history_text(history)}用户问题：{user_message}\n\n"
    if parts:
        body += "\n\n".join(parts)

    api_messages = [
        {"role": "system", "content": FINAL_SYSTEM_PROMPT},
        {"role": "user", "content": body},
    ]
    for chunk in _completion(api_messages, stream=True):
        if chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


def summarize_history(messages: list[dict]) -> str:
    """把要压缩的旧段压成中文摘要（L25 强化保真）：
    原样保留所有数字/日期/表名/口径/结论；不保留过程性状态（等待授权等）；
    忽略思考与工具调用细节。失败/空返回 ""（压缩可跳过）。"""
    text = "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}：{m['content']}"
        for m in messages
    )
    resp = _completion([
        {"role": "system",
         "content": "你是对话压缩器。把下面的旧对话压成简洁中文摘要，严格遵守：\n"
                    "1. 原样保留所有具体数字、日期、表名、指标口径与最终结论\n"
                    "2. 不保留“等待授权/待确认/需继续”等过程性状态\n"
                    "3. 忽略思考过程与工具调用细节\n"
                    f"4. 摘要控制在 {SUMMARY_MAX_CHARS} 字以内\n"
                    "只输出摘要本身。"},
        {"role": "user", "content": text},
    ])
    return (resp.choices[0].message.content or "").strip()


# 自测
if __name__ == "__main__":
    print("=== 测试：问'各区域销售额' ===")
    r = chat([{"role": "user", "content": "各区域销售额是多少？"}])
    print(json.dumps(r, ensure_ascii=False, indent=2))
