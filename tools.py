"""细粒度工具函数；签名校验与 JSON schema 共用同一份定义。"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, TypedDict
from uuid import uuid4

from pydantic import ConfigDict, Field, TypeAdapter, validate_call

from context import result_index, result_page, serialize
from datasource import CURRENT_SOURCE
from db import execute_query
from run_context import RunCancelled
from semantic_layer import (
    match_domains,
    match_metrics,
    match_tables,
)
from sql_guard import SqlSecurityError, prepare_query


@dataclass
class ToolResult:
    content: str
    error_type: str | None = None
    result: dict | None = None
    query: dict | None = None

    @property
    def is_error(self) -> bool:
        return self.error_type is not None

    @property
    def sql(self) -> str | None:
        return (self.query or self.result or {}).get("execution_sql")


@validate_call(config=ConfigDict(strict=True))
def get_domains() -> ToolResult:
    """工具：列出所有可用数据域"""
    lines = [f"- {d.key}（{d.name}）：{d.description}" for d in CURRENT_SOURCE.get().domains]
    return ToolResult(content="可用数据域：\n" + "\n".join(lines))


@validate_call(config=ConfigDict(strict=True))
def get_tables(question: str) -> ToolResult:
    """工具：根据问题列出相关表（只给表名+描述；列信息用 get_table_schema 单独取）"""
    source = CURRENT_SOURCE.get()
    domains = match_domains(question, source.domains)
    metrics = match_metrics(question, metrics=source.metrics)
    tables = match_tables(question, domains, metrics, source.tables)
    lines = [f"- {t.name}：{t.description}（域：{t.domain_key}）" for t in tables]
    return ToolResult(content="相关表：\n" + "\n".join(lines))


@validate_call(config=ConfigDict(strict=True))
def get_table_schema(table_name: str) -> ToolResult:
    """工具：单表完整 schema（列名 + 业务含义），写 SQL 前必调。"""
    name = table_name.strip().lower()
    for t in CURRENT_SOURCE.get().tables:
        if t.name.lower() != name:
            continue
        if not t.is_visible:
            return ToolResult(
                content=f"表 {table_name} 不存在或不可访问。", error_type="not_found"
            )
        lines = [f"表 {t.name}：{t.description}"]
        for c in t.columns:
            lines.append(f"  - {c}：{t.column_descriptions.get(c, '')}")
        return ToolResult(content="\n".join(lines))
    return ToolResult(
        content=f"未找到表 {table_name}。请先用 get_tables 确认表名。",
        error_type="not_found",
    )


@validate_call(config=ConfigDict(strict=True))
def get_metric_caliber(hint: str) -> ToolResult:
    """工具：指标的业务口径（计算表达式、数据表、时间字段、过滤条件），
    让模型使用统一口径而不是自己发明算法。"""
    metrics = match_metrics(hint, metrics=CURRENT_SOURCE.get().metrics)
    if not metrics:
        return ToolResult(
            content=(
                "未匹配到已知指标。可先用 get_tables 查看可用表，"
                "当前目录没有该指标定义；可按题目明确给出的定义查询，不得套用其他数据集口径。"
            )
        )
    lines = []
    for m in metrics:
        lines.append(
            f"- {m.name}（{m.key}）：{m.description}\n"
            f"  计算表达式：{m.sql_expression}\n"
            f"  数据表：{m.table}；时间字段：{m.time_field or '无'}；"
            f"过滤条件：{m.filters or '无'}"
        )
    return ToolResult(content="\n".join(lines))


@validate_call(config=ConfigDict(strict=True))
def execute_sql(sql: str) -> ToolResult:
    """工具：先过安全护栏，再执行 SQL，返回 ToolResult（含结构化执行依据）。
    错误文本保持简短（异常 + 引导语），模型据此调 get_table_schema 自我修正。"""
    try:
        prepared = prepare_query(
            sql, {t.name.lower(): t.columns for t in CURRENT_SOURCE.get().tables if t.is_visible},
            fixed_clock=CURRENT_SOURCE.get().clock is not None,
        )
    except SqlSecurityError as e:
        return ToolResult(content=f"❌ SQL 被安全护栏拒绝：{e}", error_type="security")
    try:
        result = execute_query(prepared.execution_sql)
        result.update(
            result_id=uuid4().hex,
            model_sql=sql,
            sql=prepared.sql,
            execution_sql=prepared.execution_sql,
            completeness="truncated" if result["truncated"] else "complete",
            status="success" if result["rows"] else "empty",
        )
        content = json.dumps(result, ensure_ascii=False, allow_nan=False)
        return ToolResult(content=content, result=result)
    except RunCancelled:
        raise
    except Exception as e:  # noqa: BLE001 — 工具边界，取消单独传播
        return ToolResult(
            content=(
                f"❌ SQL 执行失败：{type(e).__name__}: {e}\n"
                "如需确认列名，请调用 get_table_schema 查看完整表结构。"
            ),
            error_type="timeout" if isinstance(e, TimeoutError) else "execution",
            query={"model_sql": sql, "sql": prepared.sql, "execution_sql": prepared.execution_sql,
                   "status": "failed"},
        )


class ToolSpec(TypedDict):
    fn: Callable[..., ToolResult]
    params: str


@validate_call(config=ConfigDict(strict=True))
def read_result(
    result_id: str,
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    *,
    results: dict[str, dict],
) -> ToolResult:
    """读取当前会话已留档的结果；分页不重新执行 SQL。"""
    result = results.get(result_id)
    if result is None:
        return ToolResult(content="结果不存在或不属于本会话", error_type="not_found")
    page = result_page(result, offset, limit)
    return ToolResult(content=serialize(page), result=page)


@validate_call(config=ConfigDict(strict=True))
def list_results(
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=30)] = 30,
    *,
    results: dict[str, dict],
) -> ToolResult:
    """分页浏览本会话全部结果索引，包括已压缩轮次。"""
    return ToolResult(content=serialize(result_index(results, offset, limit)))


def tool_catalog() -> dict:
    catalog = {}
    for name, spec in TOOLS.items():
        schema = TypeAdapter(spec["fn"]).json_schema()
        schema.get("properties", {}).pop("results", None)
        schema["required"] = [k for k in schema.get("required", []) if k != "results"]
        catalog[name] = {"description": spec["fn"].__doc__, "parameters": schema}
    return catalog


# 调用参数由函数签名和 validate_call 校验，不另维护一份参数字段模型。
TOOLS: dict[str, ToolSpec] = {
    "read_result": {"fn": read_result, "params": "分页读取结果"},
    "list_results": {"fn": list_results, "params": "分页读取索引"},
    "get_domains": {
        "fn": get_domains,
        "params": "无参数",
    },
    "get_tables": {
        "fn": get_tables,
        "params": "question: str（用户的问题原文）",
    },
    "get_table_schema": {
        "fn": get_table_schema,
        "params": "table_name: str（表名，先用 get_tables 获取）",
    },
    "get_metric_caliber": {
        "fn": get_metric_caliber,
        "params": "hint: str（指标名称或用户问题原文）",
    },
    "execute_sql": {
        "fn": execute_sql,
        "params": "sql: str（SELECT 查询语句）",
    },
}
