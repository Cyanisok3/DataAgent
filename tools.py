"""
tools.py —— 工具注册表
本项目的"工具"就是 Python 函数，模型决定调哪个，我们执行哪个。
现在用 mock 工具（不连真数据库），后面阶段换成真 SQLAlchemy。
"""
from semantic_layer import build_context
from sql_guard import validate_and_transform, SqlSecurityError
from db import execute_query, init_db


def get_domains() -> str:
    """工具：获取可用数据域"""
    from semantic_layer import DOMAINS
    return "\n".join(f"- {d.name}：{d.description}" for d in DOMAINS)


def get_context(question: str) -> str:
    """工具：语义层反向匹配"""
    import json
    ctx = build_context(question)
    return json.dumps(ctx, ensure_ascii=False, indent=2)


def execute_sql(sql: str) -> str:
    """工具：执行 SELECT SQL（先过护栏，再查真数据库）"""
    # 第 1 步：过 SQL 安全护栏
    try:
        safe_sql = validate_and_transform(sql)
    except SqlSecurityError as e:
        return f"❌ SQL 被安全护栏拒绝: {e}"

    # 第 2 步：真查 SQLite 数据库
    return execute_query(safe_sql)


# 工具表：名字 → {函数, 描述}
# 模型只能从这张表里挑工具，不能随便调函数（安全！）
TOOLS = {
    "get_domains": {
        "fn": get_domains,
        "description": "获取可用的数据域列表",
    },
    "get_context": {
        "fn": get_context,
        "description": "根据用户问题反向匹配相关的数据域、表和指标",
    },
    "execute_sql": {
        "fn": execute_sql,
        "description": "执行 SELECT SQL 查询数据库",
    },
}
