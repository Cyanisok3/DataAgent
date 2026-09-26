"""确定性的模型请求视图：完整字段、整轮省略、统一序列化计量。"""

import json
import re
from dataclasses import dataclass

KIND_CHAT, KIND_CONTEXT, KIND_RESULT, KIND_ERROR = "chat", "context", "result", "error"
CONTEXT_TOOLS = {
    "get_domains",
    "get_tables",
    "get_table_schema",
    "get_metric_caliber",
    "read_result",
    "list_results",
}
# 应用配置默认值，不代表已核实的供应商窗口。llm 从环境配置覆盖。
CONTEXT_WINDOW_TOKENS = 256_000
WATERMARK_TOKENS = int(CONTEXT_WINDOW_TOKENS * 0.7) - 1024


def serialize(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def estimate_tokens(text: str) -> int:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return cjk + (len(text) - cjk + 3) // 4


_TIME_RANGE = re.compile(
    r"(BETWEEN\s+'[^']*'\s+AND\s+'[^']*'"
    r"|>=\s*'[^']*'\s+AND\s+(?:\w+\s+)?<\s*'[^']*'"
    r"|date\([^)]*\)\s*(?:>=|>|<|<=)\s*'[^']*')",
    re.IGNORECASE,
)


def time_condition(sql: str | None) -> str | None:
    """从已校验 SQL 中提取显式日期区间；无则 None，不猜测。"""
    if not sql:
        return None
    m = _TIME_RANGE.search(sql)
    return m.group(0) if m else None


@dataclass(frozen=True)
class ContextView:
    messages: list[dict]
    sources: list[str]
    omitted: list[dict]
    estimated_tokens: int
    serialized_bytes: int


class ContextInsufficient(ValueError):
    def __init__(self):
        super().__init__("context_insufficient: 必需上下文超过应用输入预算")


def result_index(results: dict[str, dict], offset=0, limit=30) -> dict:
    entries = list(results.values())
    return {
        "results": [
            {
                "result_id": r["result_id"],
                "question": r.get("question"),
                "sql": r.get("sql"),
                "time_condition": time_condition(r.get("sql")),
                "row_count": r.get("row_count"),
                "completeness": r.get("completeness", "unknown"),
            }
            for r in entries[offset : offset + limit]
        ],
        "offset": offset,
        "total": len(entries),
        "has_more": offset + limit < len(entries),
        "recovery": "list_results(offset, limit); read_result(result_id, offset, limit)",
    }


def result_page(result: dict, offset=0, limit=50) -> dict:
    if "rows" not in result:
        if offset:
            raise ValueError("旧文本结果不支持行分页；完整性未知")
        return dict(result, completeness="unknown")
    rows = result["rows"]
    return dict(
        result,
        rows=rows[offset : offset + limit],
        offset=offset,
        page_rows=len(rows[offset : offset + limit]),
        stored_rows=len(rows),
        has_more=offset + limit < len(rows),
    )


def visible_history(history: list[dict]) -> list[dict]:
    # 未经新版验证的旧摘要不能覆盖原始记录；已验证摘要按原逻辑位置排序。
    valid = {m["id"] for m in history if m.get("is_summary") and m.get("source_ids")}
    return sorted(
        [
            m
            for m in history
            if m.get("status", "completed") == "completed"
            and m["role"] in ("user", "assistant")
            and (not m.get("is_summary") or m["id"] in valid)
            and m.get("replaced_by") not in valid
        ],
        key=lambda m: (m.get("logical_position") or m.get("id", 0), m.get("id", 0)),
    )


def build_view(
    system: str,
    history: list[dict],
    current: list[dict],
    results: dict[str, dict] | None = None,
    budget: int = WATERMARK_TOKENS,
) -> ContextView:
    """预算基于最终 JSON；仅省略完整旧轮，不切字符串或证据行。"""
    groups: dict[int, list[dict]] = {}
    for m in visible_history(history):
        groups.setdefault(m.get("turn", 0), []).append(m)
    kept = list(groups.values())
    omitted: list[dict] = []
    index = result_index(results or {})
    index_visible = bool(index["results"])
    while True:
        messages = [{"role": "system", "content": system}]
        messages.extend(
            {"role": m["role"], "content": m["content"]}
            for group in kept
            for m in group
        )
        if index_visible:
            messages.append(
                {"role": "user", "content": "[结果索引] " + serialize(index)}
            )
        if omitted:
            messages.append(
                {"role": "user", "content": "[省略记录] " + serialize(omitted)}
            )
        messages.extend({"role": m["role"], "content": m["content"]} for m in current)
        raw = serialize(messages)
        cost = estimate_tokens(raw)
        if cost <= budget:
            sources = [str(m.get("id")) for group in kept for m in group]
            if index_visible:
                sources.extend(r["result_id"] for r in index["results"])
            sources.extend(str(id_) for m in current for id_ in m.get("source_ids", []))
            return ContextView(
                messages, sources, list(omitted), cost, len(raw.encode())
            )
        # 最近轮与明确标为待处理的轮不丢弃。用户修正通过原文摘要保留。
        removable = next(
            (
                i
                for i, group in enumerate(kept[:-1])
                if not any(m.get("protected") or m.get("is_summary") for m in group)
            ),
            None,
        )
        if removable is not None:
            group = kept.pop(removable)
            omitted.append(
                {
                    "turn": group[0].get("turn"),
                    "source_ids": [m.get("id") for m in group],
                    "reason": "input_budget",
                    "user_instructions": [
                        m["content"] for m in group if m["role"] == "user"
                    ],
                    "recovery": "结果仍可通过 list_results/read_result 读取；缺失口径必须澄清",
                }
            )
        elif index_visible:
            index_visible = False
            omitted.append(
                {
                    "reason": "result_index_budget",
                    "recovery": "list_results(offset=0, limit=30)",
                }
            )
        else:
            raise ContextInsufficient()


def tool_result_view(content: str, kind: str) -> str:
    return content


def project_history(history: list[dict]) -> list[dict]:
    return [
        {"role": m["role"], "content": m["content"]} for m in visible_history(history)
    ]
