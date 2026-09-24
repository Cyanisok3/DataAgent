"""
context.py —— 投影层（纯函数，不碰任何 IO）

视图/存储分离：
  存储（session_store.py）管"事实怎么存"（只追加日志）
  投影（本文件）管"给模型看什么"（确定性规则过滤/裁剪）

本文件全是纯函数，零内部 import；session_store / compaction 反向依赖这里。
消息分类契约（KIND_*）、工具结果视图、token 估算也集中在这里。

L25 审计修复：
  - 水位按 token 计量，对齐模型真实窗口（deepseek-flash = 1M token），
    压缩只在投影真实越过水位时触发（旧版 6000 字符 ≈ 窗口 0.5%，属过早优化）
  - _round_cost 与 project_history 同口径（只算实际会投影的事实条数）
  - 截断在句子边界进行；工具结果视图收敛为单一函数（llm 复用）
"""
# ─── 窗口与预算常量 ────────────────────────────────────────
CONTEXT_WINDOW_TOKENS = 256_000   # deepseek-flash 上下文窗口（换模型时改这里）
WATERMARK_RATIO = 0.8               # 安全系数：水位 = 窗口 × 0.8
WATERMARK_TOKENS = int(CONTEXT_WINDOW_TOKENS * WATERMARK_RATIO)  # 204k

MIN_TURNS = 1               # 保底：预算再紧张也至少保留最近 1 个完整轮
MAX_TOOL_RESULTS = 4        # 事实性结果最多投影几条（支持多 SQL 联合/追问）

# 单条内容字符上限（兜底防爆；细粒度工具的返回天然远小于这些值）
RESULT_MAX_CHARS = 2000     # execute_sql 数据结果
CONTEXT_MAX_CHARS = 1500    # 引导类工具结果（schema/口径/表清单）
DIALOGUE_MAX_CHARS = 4000   # user/assistant 对话
SUMMARY_MAX_CHARS = 800     # 摘要消息

# 消息分类（写日志时声明，投影时按规则过滤）
KIND_CHAT = "chat"        # 对话消息（user/assistant）
KIND_CONTEXT = "context"  # 引导性元数据：消费完即弃，不进跨轮投影
KIND_RESULT = "result"    # 事实性查询结果（execute_sql）：可被追问引用

# 哪些工具的返回属于引导类（其余工具默认事实类）
CONTEXT_TOOLS = {
    "get_domains", "get_tables", "get_table_schema", "get_metric_caliber",
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


def _round_cost(r: list[dict], factual_by_turn: dict[int, list[dict]]) -> int:
    """一轮的投影成本（与 project_history 同口径，L25 修复虚高）：
    该轮对话 + 该轮事实中【实际会投影的最后 MAX_TOOL_RESULTS 条】，
    而不是该轮产生过的全部工具结果。"""
    cost = sum(estimate_tokens(_dialogue_view(m)) for m in r)
    facts = factual_by_turn.get(r[0].get("turn", 0), [])
    for m in facts[-MAX_TOOL_RESULTS:]:
        cost += estimate_tokens(tool_result_view(m["content"], KIND_RESULT))
    return cost


def _fit_rounds(rounds: list[list[dict]],
                factual_by_turn: dict[int, list[dict]],
                budget: int,
                min_rounds: int = MIN_TURNS) -> tuple:
    """从最新轮往回装：预算不足停；保底至少 min_rounds 个完整轮。
    返回 (保留[正序], 丢弃[正序])"""
    kept, used = [], 0
    for r in reversed(rounds):
        cost = _round_cost(r, factual_by_turn)
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
      3. 事实：只取【保留轮次内】kind=result 的最后 MAX_TOOL_RESULTS 条；
         kind=context 一律不进跨轮投影（一次性引导，需要时重新调工具）

    旧消息不删除、不修改——只是不投影。
    """
    summaries, rounds, factual_by_turn, budget = _budget_split(history)
    kept_rounds, _ = _fit_rounds(rounds, factual_by_turn, budget)
    kept_turns = {r[0].get("turn", 0) for r in kept_rounds}

    view: list[dict] = []
    for s in summaries:
        view.append({"role": "assistant",
                     "content": "[历史摘要] "
                                + truncate_sentence(s["content"], SUMMARY_MAX_CHARS)})
    for r in kept_rounds:
        for m in r:
            view.append({"role": m["role"], "content": _dialogue_view(m)})

    bound = [m for m in history
             if m["role"] == "tool" and m.get("kind") == KIND_RESULT
             and m.get("turn") in kept_turns]
    for m in bound[-MAX_TOOL_RESULTS:]:
        view.append({"role": "tool",
                     "content": tool_result_view(m["content"], KIND_RESULT)})

    return view


def find_compressible(history: list[dict]) -> list[dict]:
    """
    找"该折叠成摘要的旧段"（压缩触发点）：与 project_history 共享
    _budget_split / _fit_rounds（同口径）——投影装不下的旧轮就是压缩目标。
    段内容 = 丢弃的旧轮对话 + 旧轮绑定的事实 + 旧摘要（摘要模型看得见真数字）。
    """
    summaries, rounds, factual_by_turn, budget = _budget_split(history)
    _, dropped = _fit_rounds(rounds, factual_by_turn, budget)
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
