"""投影与摘要纯函数：新契约不再保留旧字符串裁剪断言。"""

import pytest

import context as C
from compaction import paragraphs_for, validate_summary


def msg(id_, role, content, turn=1, **kw):
    return dict(id=id_, role=role, content=content, turn=turn, **kw)


@pytest.mark.parametrize(
    "text,expected", [("", 0), ("你好", 2), ("hello", 2), ("销售额sales", 5)]
)
def test_estimate(text, expected):
    assert C.estimate_tokens(text) == expected


@pytest.mark.parametrize("kind", ["result", "context", "unknown"])
def test_no_prefix_cut(kind):
    text = "x" * 5000 + "最后一行 987654"
    assert C.tool_result_view(text, kind) == text


def test_exact_request_cost_and_determinism():
    history = [
        msg(1, "user", "问题"),
        msg(2, "assistant", "答复"),
        msg(3, "tool", "不应重复计量" * 5000),
    ]
    current = [{"role": "user", "content": "这次问题"}]
    results = {"r": {"result_id": "r", "sql": "SELECT 12", "rows": [[12]]}}
    view = C.build_view("system", history, current, results)
    assert view == C.build_view("system", history, current, results)
    raw = C.serialize(view.messages)
    assert view.estimated_tokens == C.estimate_tokens(raw)
    assert view.serialized_bytes == len(raw.encode())
    assert "不应重复计量" not in raw and "SELECT 12" in raw


@pytest.mark.parametrize("size", [1911, 3037])
def test_real_trace_size_does_not_trigger_omission(size):
    history = [msg(1, "user", "问题"), msg(2, "assistant", "数" * size)]
    assert not C.build_view("s", history, [{"role": "user", "content": "继续"}]).omitted


def test_omits_whole_round_keeps_user_correction_and_latest():
    history = [
        msg(1, "user", "修正：只看已支付"),
        msg(2, "assistant", "冗余" * 1000),
        msg(3, "user", "可以", 2),
        msg(4, "assistant", "任务已完成", 2),
    ]
    view = C.build_view("s", history, [{"role": "user", "content": "新问题"}], budget=500)
    text = C.serialize(view.messages)
    assert "修正：只看已支付" in text and "任务已完成" in text and "可以" in text
    assert "冗余" not in text and view.omitted[0]["source_ids"] == [1, 2]


def test_required_over_budget_fails_instead_of_minimum_turn_override():
    with pytest.raises(C.ContextInsufficient):
        C.build_view("s", [], [{"role": "user", "content": "必须完整" * 1000}], budget=20)


def test_failed_partial_and_tools_not_history():
    history = [
        msg(1, "assistant", "部分错误答案", status="failed"),
        msg(2, "assistant", "完整"),
        msg(3, "tool", "schema"),
    ]
    assert C.project_history(history) == [{"role": "assistant", "content": "完整"}]


def test_legacy_summary_does_not_hide_original_and_new_summary_logical_position():
    history = [
        msg(1, "user", "原文", replaced_by=99),
        msg(99, "assistant", "旧非法摘要", is_summary=1),
        msg(3, "user", "后面的问题", 2),
    ]
    assert [m["content"] for m in C.visible_history(history)] == ["原文", "后面的问题"]
    history.append(
        msg(
            100,
            "assistant",
            "验证摘要",
            is_summary=1,
            source_ids=[1],
            logical_position=1,
        )
    )
    history[0]["replaced_by"] = 100
    assert [m["content"] for m in C.visible_history(history)] == [
        "验证摘要",
        "后面的问题",
    ]


def test_index_paginated_without_losing_old_results():
    results = {str(i): {"result_id": str(i), "sql": "SELECT 1"} for i in range(35)}
    assert C.result_index(results)["has_more"]
    assert C.result_index(results, 30)["results"][-1]["result_id"] == "34"


def test_page_whole_rows_and_legacy_unknown():
    result = {
        "result_id": "r",
        "rows": [["first"], ["x" * 5000]],
        "completeness": "truncated",
    }
    page = C.result_page(result, 1, 1)
    assert page["rows"] == [["x" * 5000]] and page["completeness"] == "truncated"
    assert not page["has_more"] and page["stored_rows"] == 2
    assert C.result_page({"legacy_content": "旧数据"})["completeness"] == "unknown"


def test_summary_copies_numbers_with_source_binding_and_all_user_corrections():
    segment = [
        msg(1, "user", "修正口径，不看取消订单"),
        msg(2, "assistant", "A门店100元\n\nB门店200元\n\n冗余段落"),
    ]
    paragraphs = paragraphs_for(segment)
    summary = validate_summary('{"keep":[]}', paragraphs)
    assert "A门店100元" in summary and "B门店200元" in summary
    assert "修正口径" in summary and "冗余段落" not in summary
    assert "source_id" in summary


@pytest.mark.parametrize(
    "raw",
    [
        '{"keep":[99]}',
        '{"keep":[0,0]}',
        '{"keep":[true]}',
        '{"keep":[],"text":"A门店200元"}',
    ],
)
def test_illegal_summary_rejected(raw):
    with pytest.raises(ValueError):
        validate_summary(raw, paragraphs_for([msg(1, "assistant", "A门店100元")]))


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT * FROM o WHERE d BETWEEN '2026-01-01' AND '2026-03-31'", True),
        ("SELECT * FROM o WHERE d >= '2026-01-01' AND d < '2026-04-01'", True),
        ("SELECT * FROM o WHERE date(d) >= '2026-01-01'", True),
        ("SELECT * FROM o WHERE amount > 100", False),
        (None, False),
    ],
)
def test_time_condition_extracted_only_from_explicit_date_range(sql, expected):
    assert bool(C.time_condition(sql)) == expected


def test_result_index_includes_time_condition_field():
    results = {
        "r1": {"result_id": "r1", "sql": "SELECT * FROM o WHERE d BETWEEN '2026-01-01' AND '2026-02-01'", "rows": [[1]]},
        "r2": {"result_id": "r2", "sql": "SELECT COUNT(*) FROM o", "rows": [[5]]},
    }
    idx = C.result_index(results)
    assert idx["results"][0]["time_condition"] is not None
    assert idx["results"][1]["time_condition"] is None
