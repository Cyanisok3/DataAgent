"""
context.py —— 投影层（纯函数，不碰任何 IO）

视图/存储分离：
  存储（session_store.py）管"事实怎么存"（只追加日志）
  投影（本文件）管"给模型看什么"（确定性规则过滤/裁剪）

本文件全是纯函数，零内部 import；session_store / compaction 反向依赖这里。
消息分类契约（KIND_*）、工具结果视图、token 估算也集中在这里。

L27：结果索引 + 按需读取（替代"最近 N 条完整结果"硬选择）
  - 历史 kind=result 不再投影完整内容，改为一条合并的精简索引列表
  - 每条索引 ~100 字（ID + SQL 摘要 + 行数 + 前 2 行预览），30 条也才 3000 字
  - 模型需要完整数据时调 read_result(result_id=ID) 按需拉取
  - 预算计算只算对话（事实结果不占实时投影预算），可保留更多轮
"""
# ─── 窗口与预算常量 ────────────────────────────────────────
CONTEXT_WINDOW_TOKENS = 256_000   # deepseek-flash 上下文窗口（换模型时改这里）
WATERMARK_RATIO = 0.8               # 安全系数：水位 = 窗口 × 0.8
WATERMARK_TOKENS = int(CONTEXT_WINDOW_TOKENS * WATERMARK_RATIO)  # 204k

MIN_TURNS = 1               # 保底：预算再紧张也至少保留最近 1 个完整轮

# 结果索引（L27）：最多保留多少条历史查询结果的精简索引
RESULT_INDEX_MAX = 30

# 单条内容字符上限（兜底防爆；细粒度工具的返回天然远小于这些值）
RESULT_MAX_CHARS = 2000     # execute_sql 数据结果（read_result 拉取时兜底截断）
CONTEXT_MAX_CHARS = 1500    # 引导类工具结果（schema/口径/表清单）
DIALOGUE_MAX_CHARS = 4000   # user/assistant 对话
SUMMARY_MAX_CHARS = 800     # 摘要消息

# 消息分类（写日志时声明，投影时按规则过滤）
KIND_CHAT = "chat"        # 对话消息（user/assistant）
KIND_CONTEXT = "context"  # 引导性元数据：消费完即弃，不进跨轮投影
KIND_RESULT = "result"    # 事实性查询结果（execute_sql 成功）：可被追问引用
KIND_ERROR = "error"      # 工具执行失败（SQL 错误/护栏拒绝）：不占事实名额，留档审计

# 哪些工具的返回属于引导类（其余工具默认事实类）
CONTEXT_TOOLS = {
    "get_domains", "get_tables", "get_table_schema", "get_metric_caliber",
    "read_result",  # L27：按需读取的历史结果，消费完即弃
}


# ─── token 估算与截断（纯函数）──────────────────────────────

def estimate_tokens(text: str) -> int:
    """近似 token 数。水位只需要安全余量，不必精确：
    CJK 字符约 1 token/字；其余文本约 4 字符/token。"""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk + max(1, (len(text) - cjk) // 4)


def truncate_sentence(text: str, max_chars: int) -> str:
    """在句子/行边界截断（不把句子切一半）；找不到合适边界才硬切。"""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for sep in ("\n", "。", "！", "？", "；", ". ", "; ", ", "):
        i = cut.rfind(sep)
        if i >= max_chars // 2:
            return cut[:i + 1].rstrip()
    return cut


def tool_result_view(content: str, kind: str) -> str:
    """单条工具结果的投影形态（决策链与跨轮投影的唯一入口，L25 收敛）：
    引导类按 CONTEXT_MAX_CHARS、事实类按 RESULT_MAX_CHARS 兜底截断。"""
    limit = CONTEXT_MAX_CHARS if kind == KIND_CONTEXT else RESULT_MAX_CHARS
    if len(content) > limit:
        return (truncate_sentence(content, limit)
                + f"\n…（仅保留开头，完整内容存于日志，共 {len(content)} 字）")
    return content


def _dialogue_view(m: dict) -> str:
    """对话消息投影形态（句子边界兜底截断）"""
    c = m["content"]
    if len(c) > DIALOGUE_MAX_CHARS:
        return truncate_sentence(c, DIALOGUE_MAX_CHARS) \
            + f"\n…（已截断，完整内容共 {len(c)} 字）"
    return c


def build_result_index(history: list[dict]) -> str:
    """从历史中提取 kind=result 的消息，生成精简索引列表（L27）。
    每条：#id [turn=N]: SQL前60字 (行数) 预览: 前2行
    索引本身很小（每条~100字），30条也才3000字——替代"最后4条完整结果"。
    模型需要完整数据时调 read_result(result_id=ID) 按需拉取。"""
    results = [m for m in history
               if m["role"] == "tool" and m.get("kind") == KIND_RESULT
               and not m.get("replaced_by")]
    if not results:
        return ""
    lines = ["[历史查询结果索引 — 需要完整数据时调用 read_result(result_id=ID)]"]
    for m in results[-RESULT_INDEX_MAX:]:
        content = m["content"]
        # 提取实际执行 SQL（header 中 "→ SQL: " 之后到 "]" 之前）
        sql = ""
        sql_start = content.find("→ SQL: ")
        if sql_start >= 0:
            sql_end = content.find("]", sql_start)
            if sql_end >= 0:
                sql = content[sql_start + 7:sql_end][:60]
        if not sql:
            sql = content[:60].replace("\n", " ")
        # 提取行数
        rows_info = ""
        if "Total rows:" in content:
            rs = content.find("Total rows:")
            re = content.find("\n", rs)
            if re >= 0:
                rows_info = content[rs:re]
        # 提取前 2 行数据预览（以 { 开头的行）
        data_lines = [l for l in content.split("\n") if l.startswith("{")][:2]
        preview = " | ".join(data_lines)[:80]
        lines.append(f"#{m['id']} [turn={m.get('turn', '?')}]: {sql} "
                     f"({rows_info}) 预览: {preview}")
    return "\n".join(lines)


# ─── 同口径预算分配 ────────────────────────────────────────

def _budget_split(history: list[dict]) -> tuple:
    """
    返回 (summaries, rounds, factual_by_turn, budget_after_summary)
      summaries       有效摘要（is_summary=1 且未被吸收）
      rounds          对话按 turn 分组（未吸收、非摘要的 user/assistant）
      factual_by_turn {turn: [kind=result 消息]}（事实按轮绑定）
      budget          扣完摘要后的剩余 token 预算
    """
    summaries = [m for m in history
                 if m.get("is_summary") and not m.get("replaced_by")]
    dialogue = [m for m in history
                if m["role"] in ("user", "assistant")
                and not m.get("is_summary") and not m.get("replaced_by")]
    factual = [m for m in history
               if m["role"] == "tool" and m.get("kind") == KIND_RESULT
               and not m.get("replaced_by")]

    budget = WATERMARK_TOKENS - sum(
        estimate_tokens(truncate_sentence(s["content"], SUMMARY_MAX_CHARS))
        for s in summaries)

    by_turn: dict[int, list[dict]] = {}
    for m in dialogue:
        by_turn.setdefault(m.get("turn", 0), []).append(m)
    rounds = [by_turn[t] for t in sorted(by_turn)]

    factual_by_turn: dict[int, list[dict]] = {}
    for m in factual:
        factual_by_turn.setdefault(m.get("turn", 0), []).append(m)

    return summaries, rounds, factual_by_turn, budget


def _round_cost(r: list[dict]) -> int:
    """一轮的投影成本（L27：事实结果不投影完整内容，只算对话）。
    结果索引是一条合并消息，成本固定且很小，不计入每轮成本。"""
    return sum(estimate_tokens(_dialogue_view(m)) for m in r)


def _fit_rounds(rounds: list[list[dict]],
                budget: int,
                min_rounds: int = MIN_TURNS) -> tuple:
    """从最新轮往回装：预算不足停；保底至少 min_rounds 个完整轮。
    返回 (保留[正序], 丢弃[正序])"""
    kept, used = [], 0
    for r in reversed(rounds):
        cost = _round_cost(r)
        if len(kept) < min_rounds or used + cost <= budget:
            kept.append(r)
            used += cost
        else:
            break
    kept.reverse()
    dropped = rounds[:len(rounds) - len(kept)]
    return kept, dropped


# ─── 投影与压缩判定（公开 API）──────────────────────────────

def project_history(history: list[dict]) -> list[dict]:
    """
    投影：从完整日志选出"该给模型看什么"（确定性规则，非智能判断）

      1. 摘要（观点）：有效摘要前置，不可丢
      2. 对话：按 token 预算从新往回装（至少 MIN_TURNS 个完整轮）
      3. 结果索引（L27）：所有 kind=result 的历史查询生成精简索引列表，
         合并为一条 tool 消息；模型需要完整数据时调 read_result(result_id=ID)

    旧消息不删除、不修改——只是不投影。
    """
    summaries, rounds, _, budget = _budget_split(history)
    kept_rounds, _ = _fit_rounds(rounds, budget)

    view: list[dict] = []
    for s in summaries:
        view.append({"role": "assistant",
                     "content": "[历史摘要] "
                                + truncate_sentence(s["content"], SUMMARY_MAX_CHARS)})
    for r in kept_rounds:
        for m in r:
            view.append({"role": m["role"], "content": _dialogue_view(m)})

    # 结果索引（替代之前的"最后 N 条完整结果"）
    index = build_result_index(history)
    if index:
        view.append({"role": "tool", "content": index})

    return view


def find_compressible(history: list[dict]) -> list[dict]:
    """
    找"该折叠成摘要的旧段"（压缩触发点）：与 project_history 共享
    _budget_split / _fit_rounds（同口径）——投影装不下的旧轮就是压缩目标。
    段内容 = 丢弃的旧轮对话 + 旧轮绑定的事实 + 旧摘要（摘要模型看得见真数字）。
    """
    summaries, rounds, factual_by_turn, budget = _budget_split(history)
    _, dropped = _fit_rounds(rounds, budget)
    if not dropped:
        return []

    segment: list[dict] = []
    for r in dropped:
        segment.extend(r)
    dropped_turns = {r[0].get("turn", 0) for r in dropped}
    for m in history:
        if (m["role"] == "tool" and m.get("kind") == KIND_RESULT
                and m.get("turn") in dropped_turns and not m.get("replaced_by")):
            segment.append(m)
    segment.extend(summaries)
    return segment
