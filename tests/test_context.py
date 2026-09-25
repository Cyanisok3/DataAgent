"""
tests/test_context.py —— 投影层纯函数的回归测试

覆盖评审文档（docs/2026-09-24-context-and-trace-review.md）中
与投影/预算相关的关键规则：
  - estimate_tokens / truncate_sentence / tool_result_view 的基础行为
  - project_history：kind 分类过滤、事实条数上限、摘要前置、replaced_by 排除
  - _round_cost 与 project_history 同口径（审计中发现的预算虚高回归）
  - find_compressible 与 project_history 共享判定口径

运行：cd DataAgent && .venv/bin/python -m pytest tests/test_context.py -v
"""
import context as C


# ─── 辅助：构造一条消息 ────────────────────────────────────

def _msg(role, content, **kw):
    m = {"role": role, "content": content, "id": 0}
    m.update(kw)
    return m


def _user(content, turn=1):
    return _msg("user", content, kind=C.KIND_CHAT, turn=turn)


def _assistant(content, turn=1):
    return _msg("assistant", content, kind=C.KIND_CHAT, turn=turn)


def _tool_result(content, turn=1, **kw):
    return _msg("tool", content, kind=C.KIND_RESULT, turn=turn, **kw)


def _tool_context(content, turn=1, **kw):
    return _msg("tool", content, kind=C.KIND_CONTEXT, turn=turn, **kw)


# ─── 1. estimate_tokens ────────────────────────────────────

class TestEstimateTokens:
    def test_empty(self):
        assert C.estimate_tokens("") == 1  # max(1, 0//4) = 1

    def test_cjk_only(self):
        # 5 个中文字 → cjk=5, max(1, 0//4)=1 → 6
        # （纯 CJK 也会加 1，因为非 CJK 部分的下限是 1）
        assert C.estimate_tokens("你好世界啊") == 6

    def test_ascii_only(self):
        # 8 个英文字符 → 8//4 = 2
        assert C.estimate_tokens("hello world") == 2  # 11 chars → 2 (11//4=2) + 0 cjk

    def test_mixed(self):
        # "销售额" 3 cjk + "sales" 5 ascii → 3 + 1 = 4
        assert C.estimate_tokens("销售额sales") == 4


# ─── 2. truncate_sentence ─────────────────────────────────

class TestTruncateSentence:
    def test_short_unchanged(self):
        assert C.truncate_sentence("短文本", 100) == "短文本"

    def test_cuts_at_sentence_boundary(self):
        text = "第一句。第二句很长很长很长。第三句。"
        # max_chars=14：第二个句号在位置 13，过半(>=7)，应在该处截断
        result = C.truncate_sentence(text, 14)
        assert result == "第一句。第二句很长很长很长。"
        assert "第三句" not in result

    def test_no_boundary_hard_cut(self):
        text = "a" * 100  # 无标点
        result = C.truncate_sentence(text, 50)
        assert len(result) == 50

    def test_boundary_must_be_past_half(self):
        # 边界在前半部分时不应采用（避免切在开头）
        text = "a。" + "b" * 100
        result = C.truncate_sentence(text, 50)
        # "a。" 在位置 1，小于 25（half），不应作为边界 → 硬切
        assert len(result) == 50


# ─── 3. tool_result_view ──────────────────────────────────

class TestToolResultView:
    def test_short_unchanged(self):
        assert C.tool_result_view("短结果", C.KIND_RESULT) == "短结果"

    def test_result_uses_result_limit(self):
        long = "x" * (C.RESULT_MAX_CHARS + 100)
        result = C.tool_result_view(long, C.KIND_RESULT)
        assert "仅保留开头" in result
        assert len(result) < len(long)

    def test_context_uses_context_limit(self):
        long = "x" * (C.CONTEXT_MAX_CHARS + 100)
        result = C.tool_result_view(long, C.KIND_CONTEXT)
        assert "仅保留开头" in result

    def test_unknown_kind_defaults_to_result(self):
        long = "x" * (C.RESULT_MAX_CHARS + 100)
        result = C.tool_result_view(long, "unknown")
        assert "仅保留开头" in result


# ─── 4. project_history 基础行为 ──────────────────────────

class TestProjectHistory:
    def test_empty_history(self):
        assert C.project_history([]) == []

    def test_context_kind_not_projected(self):
        """引导类工具结果（schema/口径）不进跨轮投影。"""
        history = [
            _user("查销售额", turn=1),
            _tool_context("表结构...", turn=1),
            _tool_result("华东 100", turn=1),
            _assistant("回答", turn=1),
        ]
        view = C.project_history(history)
        roles = [m["role"] for m in view]
        # 不应有 context 类工具；result 类工具应保留
        assert "tool" in roles
        tool_contents = [m["content"] for m in view if m["role"] == "tool"]
        assert all("表结构" not in c for c in tool_contents)

    def test_result_index_cap(self):
        """L27：结果索引最多保留 RESULT_INDEX_MAX 条（替代旧的 MAX_TOOL_RESULTS）。"""
        history = [_user("q", turn=1)]
        for i in range(C.RESULT_INDEX_MAX + 5):
            history.append(_tool_result(f"结果{i}", turn=1, id=i + 1))
        history.append(_assistant("a", turn=1))
        view = C.project_history(history)
        # tool 消息只有一条（合并的索引列表）
        tool_msgs = [m for m in view if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        index_text = tool_msgs[0]["content"]
        # 索引中应包含最后 RESULT_INDEX_MAX 条，不包含最早的 5 条
        assert "结果0" not in index_text  # 最早的被挤出索引
        assert f"结果{C.RESULT_INDEX_MAX + 4}" in index_text  # 最新的在索引中

    def test_summary_prepended(self):
        """有效摘要前置，且不可丢。"""
        history = [
            _msg("assistant", "旧摘要", is_summary=1, turn=1),
            _user("新问题", turn=2),
            _assistant("新回答", turn=2),
        ]
        view = C.project_history(history)
        assert view[0]["role"] == "assistant"
        assert "历史摘要" in view[0]["content"]
        assert "旧摘要" in view[0]["content"]

    def test_replaced_by_excluded(self):
        """被摘要吸收的消息（replaced_by 非空）不进投影。"""
        history = [
            _user("旧问题", turn=1),
            _msg("assistant", "旧回答", turn=1, replaced_by=99),
            _user("新问题", turn=2),
            _assistant("新回答", turn=2),
        ]
        view = C.project_history(history)
        contents = [m["content"] for m in view]
        assert "旧回答" not in contents

    def test_min_turns_guaranteed(self):
        """即使预算紧张，也至少保留 MIN_TURNS 个完整轮。"""
        # 构造大量轮次，每轮对话很长
        history = []
        for t in range(1, 20):
            history.append(_user(f"问题{t}", turn=t))
            history.append(_assistant(f"回答{t}" * 100, turn=t))
        view = C.project_history(history)
        # 至少保留 1 轮（MIN_TURNS=1），即至少有 user+assistant
        roles = [m["role"] for m in view]
        assert "user" in roles
        assert "assistant" in roles


# ─── 5. _round_cost 与 project_history 同口径（关键回归）───

class TestBudgetConsistency:
    """
    审计回归：旧版 _round_cost 把一轮全部工具结果计入成本，
    而 project_history 只投影最后 MAX_TOOL_RESULTS 条 → 预算虚高，
    导致"没超水位却触发压缩"。
    修复后两者必须同口径。
    """

    def test_round_cost_dialogue_only(self):
        """L27：_round_cost 只算对话，事实结果不占实时投影预算
        （事实以索引形式合并为一条消息，成本固定且很小）。"""
        turn = 1
        round_msgs = [_user("q", turn=turn), _assistant("a", turn=turn)]

        cost = C._round_cost(round_msgs)

        # 只算对话的 token
        expected = sum(C.estimate_tokens(C._dialogue_view(m)) for m in round_msgs)
        assert cost == expected

    def test_find_compressible_matches_project_history_dropped(self):
        """find_compressible 返回的旧轮，必须恰好是 project_history 丢弃的轮。"""
        # 构造多轮，每轮带事实结果
        history = []
        for t in range(1, 6):
            history.append(_user(f"q{t}", turn=t))
            history.append(_tool_result(f"r{t}", turn=t))
            history.append(_assistant(f"a{t}", turn=t))

        # 直接比较：project_history 保留的 turn 集合 vs find_compressible 涉及的 turn
        summaries, rounds, factual, budget = C._budget_split(history)
        kept, dropped = C._fit_rounds(rounds, budget)
        kept_turns = {r[0]["turn"] for r in kept}
        dropped_turns = {r[0]["turn"] for r in dropped}

        compressible = C.find_compressible(history)
        compressible_turns = {m["turn"] for m in compressible if m["role"] in ("user", "assistant")}

        # 压缩段涉及的轮必须恰好是被丢弃的轮
        assert compressible_turns == dropped_turns
        # 保留轮与丢弃轮不重叠
        assert kept_turns.isdisjoint(dropped_turns)

    def test_short_conversation_no_compression(self):
        """短对话不应触发压缩。"""
        history = [
            _user("今年销售额", turn=1),
            _tool_result("华东 100", turn=1),
            _assistant("回答", turn=1),
        ]
        assert C.find_compressible(history) == []


class TestResultIndex:
    """L27：结果索引替代"按轮绑定的完整结果投影"。"""

    def test_index_includes_all_turns(self):
        """结果索引包含所有轮次的 kind=result（不再按保留轮绑定）。"""
        history = [
            _user("旧问题", turn=1),
            _tool_result("旧事实", turn=1, id=1),
            _assistant("旧回答", turn=1),
            _user("新问题", turn=2),
            _tool_result("新事实", turn=2, id=2),
            _assistant("新回答", turn=2),
        ]
        view = C.project_history(history)
        tool_msgs = [m for m in view if m["role"] == "tool"]
        assert len(tool_msgs) == 1  # 合并为一条索引消息
        index_text = tool_msgs[0]["content"]
        assert "旧事实" in index_text
        assert "新事实" in index_text

    def test_index_excludes_context_and_error(self):
        """索引只包含 kind=result，排除 context 和 error。"""
        history = [
            _user("q", turn=1),
            _tool_context("表结构", turn=1, id=1),
            _tool_result("正确结果", turn=1, id=2),
            _msg("tool", "错误结果", kind=C.KIND_ERROR, turn=1, id=3),
            _assistant("a", turn=1),
        ]
        index = C.build_result_index(history)
        assert "正确结果" in index
        assert "表结构" not in index
        assert "错误结果" not in index
