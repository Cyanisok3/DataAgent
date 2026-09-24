"""
context.py —— 投影层（纯函数，不碰任何 IO）

DSH 的"视图/存储分离"落到代码结构：
  存储（session_store.py）管"事实怎么存"（只追加日志）
  投影（本文件）管"给模型看什么"（确定性规则过滤/裁剪）

本文件全是纯函数：输入 history 消息列表，输出投影/压缩判定。
不 import session_store（避免环）；session_store / compaction 反向依赖这里。

消息分类契约（KIND_*）也定义在这里——投影过滤依赖它，集中一处。
"""
# ─── 预算与裁剪常量（L20 预算化投影 + L22 硬上限）───
WATERMARK_CHARS = 6000          # 投影预算（字符）：给模型看的上下文总量上限
MIN_TURNS = 1                   # 保底：预算再紧张也至少保留最近 1 个完整轮
MAX_TOOL_RESULTS = 2            # 事实性工具结果最多保留几条（支持追问）
TOOL_RESULT_MAX_CHARS = 500     # 工具结果投影 head 截断
DIALOGUE_MAX_CHARS = 1500       # 对话消息投影 head 截断（保底轮也不破硬上限）
SUMMARY_MAX_CHARS = 400         # 摘要消息投影长度上限

# 消息分类（写日志时声明，投影时按规则过滤）
KIND_CHAT = "chat"        # 对话消息（user/assistant）
KIND_CONTEXT = "context"  # 引导性元数据（get_context 结果）：消费完即弃
KIND_RESULT = "result"    # 事实性查询结果（execute_sql 结果）：可被追问引用


def _tool_view(m: dict) -> str:
    """工具结果投影形态：head 截断 + 中性措辞（预算扣减必须用它）"""
    c = m["content"]
    if len(c) > TOOL_RESULT_MAX_CHARS:
        return (c[:TOOL_RESULT_MAX_CHARS]
                + f"\n...（结果较长，仅保留开头，完整内容存于日志可审计，共 {len(c)} 字）")
    return c


def _dialogue_view(m: dict) -> str:
    """对话消息投影形态：head 截断（保底轮也稳在硬上限内）"""
    c = m["content"]
    if len(c) > DIALOGUE_MAX_CHARS:
        return c[:DIALOGUE_MAX_CHARS] + f"\n...（已截断，完整内容共 {len(c)} 字）"
    return c


def _budget_split(history: list[dict]) -> tuple:
    """
    同口径预算分配（project_history 与 find_compressible 共享，L22 P2-1）：
    返回 (summaries, rounds, factual_by_turn, budget_after_summary)
      summaries         有效摘要（is_summary=1 且未被吸收）
      rounds            对话按 turn 分组（未吸收、非摘要的 user/assistant）
      factual_by_turn   {turn: [kind=result 消息]}（事实按轮绑定）
      budget            扣完摘要后的剩余预算
    """
    summaries = [m for m in history
                 if m.get("is_summary") and not m.get("replaced_by")]
    dialogue = [m for m in history
                if m["role"] in ("user", "assistant")
                and not m.get("is_summary") and not m.get("replaced_by")]
    factual = [m for m in history
               if m["role"] == "tool" and m.get("kind") == KIND_RESULT
               and not m.get("replaced_by")]

    budget = WATERMARK_CHARS - sum(len(_dialogue_view(s)) for s in summaries)

    by_turn: dict[int, list[dict]] = {}
    for m in dialogue:
        by_turn.setdefault(m.get("turn", 0), []).append(m)
    rounds = [by_turn[t] for t in sorted(by_turn)]

    factual_by_turn: dict[int, list[dict]] = {}
    for m in factual:
        factual_by_turn.setdefault(m.get("turn", 0), []).append(m)

    return summaries, rounds, factual_by_turn, budget


def _round_cost(r: list[dict], factual_by_turn: dict[int, list[dict]]) -> int:
    """一轮的投影成本：该轮对话（截断后）+ 该轮绑定的事实（截断后）"""
    cost = sum(len(_dialogue_view(m)) for m in r)
    turn = r[0].get("turn", 0)
    for m in factual_by_turn.get(turn, []):
        cost += len(_tool_view(m))
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


def project_history(history: list[dict]) -> list[dict]:
    """
    投影：从完整日志选出"该给模型看什么"（确定性规则，非智能判断）

    规则（L20 预算化 + L22 轮次绑定）：
      1. 摘要（观点）：有效摘要前置，不可丢
      2. 对话：按预算从新往回装（_fit_rounds，至少 MIN_TURNS 个完整轮），
         单条经 _dialogue_view 截断
      3. 事实：只取【保留轮次内】的 kind=result，最近 MAX_TOOL_RESULTS 条，
         单条经 _tool_view 截断；kind=context 一律丢弃（一次性引导）

    旧消息不删除、不修改——只是不投影。压缩改的是观点，不是事实。
    """
    summaries, rounds, factual_by_turn, budget = _budget_split(history)
    kept_rounds, _ = _fit_rounds(rounds, factual_by_turn, budget)
    kept_turns = {r[0].get("turn", 0) for r in kept_rounds}

    view: list[dict] = []
    for s in summaries:
        view.append({"role": "assistant", "content": f"[历史摘要] {_dialogue_view(s)}"})
    for r in kept_rounds:
        for m in r:
            view.append({"role": m["role"], "content": _dialogue_view(m)})

    bound = [m for m in history
             if m["role"] == "tool" and m.get("kind") == KIND_RESULT
             and m.get("turn") in kept_turns]
    for m in bound[-MAX_TOOL_RESULTS:]:
        view.append({"role": "tool", "content": _tool_view(m)})

    return view


def find_compressible(history: list[dict]) -> list[dict]:
    """
    找"该折叠成摘要的旧段"（压缩触发点）：
    与 project_history 共享 _budget_split / _fit_rounds（同口径）——
    投影装不下的旧轮，就是压缩要处理的目标。
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


def visible_chars(history: list[dict]) -> int:
    """投影总字符数（调试/自测用）"""
    return sum(len(m["content"]) for m in project_history(history))
