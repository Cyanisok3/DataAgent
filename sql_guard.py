"""SQLite 查询白名单。校验答案语义与预览执行上限分开，不改写日期基准。"""

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

MAX_ROWS = 200
SAFE_FUNCTIONS = frozenset(
    ["ABS", "AVG", "COUNT", "MAX", "MIN", "SUM", "ROUND", "COALESCE", "IFNULL", "NULLIF", "LOWER", "UPPER", "LENGTH", "SUBSTRING", "SUBSTR", "TRIM", "LTRIM", "RTRIM", "REPLACE", "DATE", "DATETIME", "TIME", "STRFTIME", "JULIANDAY", "CAST", "CASE", "IF", "IIF", "CURRENT_DATE", "CURRENT_TIMESTAMP", "CURRENT_TIME", "GROUP_CONCAT", "CONCAT", "CONCAT_WS", "INSTR", "UNICODE", "CHAR", "TOTAL"]
).union({"TIME_TO_STR", "TS_OR_DS_TO_TIMESTAMP"})  # SQLGlot 的 SQLite strftime 内部节点


class SqlSecurityError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedQuery:
    sql: str
    execution_sql: str


def prepare_query(sql: str, allowed: dict[str, list[str]], *, fixed_clock: bool = False) -> PreparedQuery:
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
        if len(statements) != 1:
            raise SqlSecurityError("仅允许一条查询")
        ast = statements[0]
        if not isinstance(ast, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            raise SqlSecurityError("仅允许只读查询")
        if any(isinstance(n, (exp.DDL, exp.DML, exp.Into)) for n in ast.walk()):
            raise SqlSecurityError("禁止写操作")
        for table in ast.find_all(exp.Table):
            if table.db or table.catalog:
                raise SqlSecurityError("禁止访问附加库")
        for scope in traverse_scope(ast):
            for source in scope.sources.values():
                if isinstance(source, exp.Table) and source.name.lower() not in allowed:
                    raise SqlSecurityError(f"表不在可访问目录中：{source.name}")
        for func in ast.find_all(exp.Func):
            # SQLGlot 将逻辑连接符也归入 Func；子节点仍会逐一检查。
            if isinstance(func, (exp.And, exp.Or)):
                continue
            name = func.name if isinstance(func, exp.Anonymous) else func.sql_name()
            if fixed_clock and (
                name.upper().startswith("CURRENT_")
                or (name.upper() in {"DATE", "TIME", "DATETIME", "JULIANDAY", "STRFTIME",
                                      "TIME_TO_STR", "TS_OR_DS_TO_TIMESTAMP"}
                    and (not any(func.iter_expressions()) or any(
                        v.is_string and v.this.lower() in {"now", "localtime", "utc"}
                        for v in func.find_all(exp.Literal))))
            ):
                raise SqlSecurityError("fixed_clock_requires_absolute_dates: 请使用提示中的固定日期字面量")
            if name.upper() not in SAFE_FUNCTIONS:
                raise SqlSecurityError(f"不支持的函数：{name}")
        for node in ast.walk():
            if isinstance(node, (exp.Limit, exp.Offset)):
                value = node.expression
                if (
                    not isinstance(value, exp.Literal)
                    or not value.is_int
                    or int(value.this) < 0
                ):
                    raise SqlSecurityError("LIMIT/OFFSET 必须是非负整数字面量")
        schema: dict[str, object] = {
            name: dict.fromkeys(columns, "UNKNOWN") for name, columns in allowed.items()
        }
        ast = qualify(
            ast, dialect="sqlite", schema=schema, validate_qualify_columns=True
        )
        normalized = ast.sql(dialect="sqlite")
        limit = ast.args.get("limit")
        cap = min(int(limit.expression.this), MAX_ROWS + 1) if limit else MAX_ROWS + 1
        return PreparedQuery(
            sql=normalized, execution_sql=ast.copy().limit(cap).sql(dialect="sqlite")
        )
    except SqlSecurityError:
        raise
    except (sqlglot.errors.SqlglotError, ValueError, TypeError) as exc:
        raise SqlSecurityError(f"SQL 无法安全解析或字段不在目录中：{exc}") from exc
