"""
tools.py —— 工具层：模型可以调用的函数（细粒度，对齐原版设计）

L24 审计修复（对照 Java 版 tools 包）：
  旧版一个 get_context 倾销 domains+tables+metrics（800–1200 字），
  关键信息被无关内容淹没、截断后指标表达式丢失。
  现拆成与原版一致的细粒度工具，每次返回小而完整：
    get_domains()       所有数据域（概览）
    get_tables(question) 相关表清单（只给表名+描述）
    get_table_schema(table_name)  单表完整列信息（写 SQL 前取）
    get_metric_caliber(hint)      指标口径（表达式/时间字段/过滤条件）
    execute_sql(sql)    执行 SELECT
  模型按需逐个取，信息不再被截断淹没。

L25：全部工具返回 ToolResult（统一结构化类型）。
  content 是给模型看的可读文本；sql/columns/row_count/truncated
  供回答阶段组装"查询依据"，解决"查过却无法确认时间范围/口径"。
"""
from dataclasses import dataclass, field

from db import execute_query
from semantic_layer import (
    DOMAINS,
    TABLES,
    match_domains,
    match_metrics,
    match_tables,
)
from sql_guard import SqlSecurityError, validate_and_transform


@dataclass
class ToolResult:
    """统一工具返回类型（评审文档第 6 节）。
    content 是给模型看的可读文本（前 20 行）；full_content 是完整结果
    （落库用，L29 工具结果完整留档）；结构化字段供回答阶段和日志使用。"""
    content: str
    is_error: bool = False
    error_type: str | None = None  # "security" | "execution" | None
    # execute_sql 专用的结构化执行依据
    sql: str | None = None          # 实际执行的 SQL（含护栏改写）
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False         # 是否被前 20 行截断
    full_content: str | None = None  # L29：完整查询结果（落库用，read_result 有效）


def get_domains() -> ToolResult:
    """工具：列出所有可用数据域"""
    lines = [f"- {d.key}（{d.name}）：{d.description}" for d in DOMAINS]
    return ToolResult(content="可用数据域：\n" + "\n".join(lines))


def get_tables(question: str) -> ToolResult:
    """工具：根据问题列出相关表（只给表名+描述；列信息用 get_table_schema 单独取）"""
    domains = match_domains(question)
    metrics = match_metrics(question)
    tables = match_tables(question, domains, metrics)
    lines = [f"- {t.name}：{t.description}（域：{t.domain_key}）" for t in tables]
    return ToolResult(content="相关表：\n" + "\n".join(lines))


def get_table_schema(table_name: str) -> ToolResult:
    """工具：单表完整 schema（列名 + 业务含义），写 SQL 前必调。"""
    name = table_name.strip().lower()
    for t in TABLES:
        if t.name != name:
            continue
        if not t.is_visible:
            return ToolResult(content=f"表 {table_name} 不存在或不可访问。")
        lines = [f"表 {t.name}：{t.description}"]
        for c in t.columns:
            lines.append(f"  - {c}：{t.column_descriptions.get(c, '')}")
        return ToolResult(content="\n".join(lines))
    return ToolResult(
        content=f"未找到表 {table_name}。请先用 get_tables 确认表名。")


def get_metric_caliber(hint: str) -> ToolResult:
    """工具：指标的业务口径（计算表达式、数据表、时间字段、过滤条件），
    让模型使用统一口径而不是自己发明算法。"""
    metrics = match_metrics(hint)
    if not metrics:
        return ToolResult(content=(
            "未匹配到已知指标。可先用 get_tables 查看可用表，"
            "或换一个指标名称（如销售额、订单量、客单价、销量）。"))
    lines = []
    for m in metrics:
        lines.append(
            f"- {m.name}（{m.key}）：{m.description}\n"
            f"  计算表达式：{m.sql_expression}\n"
            f"  数据表：{m.table}；时间字段：{m.time_field or '无'}；"
            f"过滤条件：{m.filters or '无'}"
        )
    return ToolResult(content="\n".join(lines))


def execute_sql(sql: str) -> ToolResult:
    """工具：先过安全护栏，再执行 SQL，返回 ToolResult（含结构化执行依据）。
    错误文本保持简短（异常 + 引导语），模型据此调 get_table_schema 自我修正。"""
    try:
        safe_sql = validate_and_transform(sql)
    except SqlSecurityError as e:
        return ToolResult(content=f"❌ SQL 被安全护栏拒绝：{e}",
                          is_error=True, error_type="security")
    try:
        result = execute_query(safe_sql)
        return ToolResult(content=result["content"],
                          full_content=result["full_content"],
                          sql=safe_sql,
                          columns=result["columns"],
                          row_count=result["row_count"],
                          truncated=result["truncated"])
    except Exception as e:
        return ToolResult(
            content=(f"❌ SQL 执行失败：{type(e).__name__}: {e}\n"
                     "如需确认列名，请调用 get_table_schema 查看完整表结构。"),
            is_error=True, error_type="execution")


# 工具注册表：名字 → (函数, 参数描述)。system prompt 据此告诉模型怎么调。
TOOLS = {
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


# 自测
if __name__ == "__main__":
    print(get_domains())
    print()
    print(get_tables("最近90天哪个商品销量最高"))
    print()
    print(get_table_schema("orders"))
    print()
    print(get_metric_caliber("销售额"))
