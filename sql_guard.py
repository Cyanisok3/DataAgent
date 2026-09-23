"""
sql_guard.py —— SQL 安全护栏：AST 校验 + 自动改写 + 方言转换

对应 L4 学的四道安检（JSQLParser 的 Python 版）：
  1. 只允许 SELECT（拒绝 INSERT/UPDATE/DELETE/DROP）
  2. 禁止 INTO 子句（防止 SELECT INTO 写表）
  3. 自动加 LIMIT（防止全表扫描）
  4. （超时截断留到生产做，这里先做前 3 道）

方言策略（呼应 L4 的"方言差异收敛"）：
  - 模型按 MySQL 方言写 SQL（DeepSeek 训练数据里 MySQL 最常见）
  - 护栏解析时指定 dialect="mysql"，把 MySQL 专有时间函数
    （DATE_SUB / DATE_ADD）改写成 SQLite 等价语法
  - 最终以 dialect="sqlite" 输出——保证数据库能真正执行

核心思想：模型生成的 SQL 绝不能裸跑——先过 AST 校验 + 方言适配，安全了才执行。
"""
import sqlglot
from sqlglot import exp


class SqlSecurityError(Exception):
    """SQL 安全校验失败"""
    pass


def _rewrite_date_functions(ast) -> None:
    """
    方言改写：MySQL/PG 的日期运算在 SQLite 里不存在，手动改写为
    SQLite 的 DATE('now', '-n unit') 形式（就地修改 AST 节点）。
    覆盖两种写法：
      1. 函数形式：DATE_SUB(CURDATE(), INTERVAL 30 DAY)
      2. 减法形式：CURRENT_DATE - INTERVAL '30' DAY  （模型常混用 PG 语法）
    """
    # 1. 函数形式 DATE_SUB / DATE_ADD
    for node in ast.find_all(exp.DateSub, exp.DateAdd):
        unit = node.args.get("unit")
        unit = unit.name.lower() if unit else "day"
        amount = node.args.get("expression")
        amount = amount.name if amount else "1"
        sign = "-" if isinstance(node, exp.DateSub) else "+"
        sqlite_call = exp.Anonymous(this="date", expressions=[
            exp.Literal.string("now"),
            exp.Literal.string(f"{sign}{amount} {unit}"),
        ])
        node.replace(sqlite_call)

    # 2. 减法/加法形式：CURRENT_DATE ± INTERVAL 'n' unit
    for node in ast.find_all(exp.Sub, exp.Add):
        interval = node.args.get("expression")
        if not isinstance(interval, exp.Interval):
            continue
        # this 必须是"当前日期"类函数（列 - interval 几乎不会出现，不改）
        this = node.args.get("this")
        if isinstance(this, exp.Column) and this.name.upper() not in ("CURDATE", "NOW"):
            continue
        unit = interval.args.get("unit")
        unit = unit.name.lower() if unit else "day"
        amount = interval.this
        amount = amount.name if amount else "1"
        sign = "-" if isinstance(node, exp.Sub) else "+"
        sqlite_call = exp.Anonymous(this="date", expressions=[
            exp.Literal.string("now"),
            exp.Literal.string(f"{sign}{amount} {unit}"),
        ])
        node.replace(sqlite_call)


def validate_and_transform(sql: str) -> str:
    """
    输入：模型生成的原始 SQL（MySQL 方言）
    输出：安全、可执行（SQLite 方言）的 SQL（自动加了 LIMIT 等）
    异常：SqlSecurityError（任何一道安检没过）
    """
    # ─── 第 0 步：按 MySQL 方言解析 SQL 成 AST ───
    try:
        ast = sqlglot.parse_one(sql, dialect="mysql")
    except Exception as e:
        raise SqlSecurityError(f"SQL 语法错误: {e}")

    # ─── 第 0.5 步：方言改写（MySQL 时间函数 → SQLite 等价）───
    _rewrite_date_functions(ast)

    # ─── 第 1 道：只允许 SELECT ───
    if not isinstance(ast, exp.Select):
        raise SqlSecurityError(
            f"只允许 SELECT 查询，检测到 {type(ast).__name__} 语句"
        )

    # ─── 第 2 道：禁止 INTO（防止 SELECT ... INTO new_table 写表）───
    if ast.find(exp.Into):
        raise SqlSecurityError("禁止使用 INTO 子句（不允许写表）")

    # ─── 第 3 道：自动加 LIMIT ───
    if not ast.args.get("limit"):
        ast = ast.limit(200)  # 默认最多返回 200 行

    # ─── 返回安全 + 方言适配后的 SQL（SQLite 方言）───
    return ast.sql(dialect="sqlite")


# ─── 自测 ────────────────────────────────────────────────────

if __name__ == "__main__":
    # 测试 1：正常 SELECT → 应该通过，自动加 LIMIT
    print("=== 测试 1：正常 SELECT ===")
    try:
        safe = validate_and_transform(
            "SELECT region, SUM(amount) FROM orders GROUP BY region"
        )
        print(f"✓ 通过 → {safe}")
    except SqlSecurityError as e:
        print(f"✗ 拒绝 → {e}")

    # 测试 2：DELETE → 应该拒绝
    print("\n=== 测试 2：DELETE ===")
    try:
        safe = validate_and_transform("DELETE FROM orders WHERE id = 1")
        print(f"✗ 居然通过了 → {safe}")
    except SqlSecurityError as e:
        print(f"✓ 正确拒绝 → {e}")

    # 测试 3：DROP → 应该拒绝
    print("\n=== 测试 3：DROP TABLE ===")
    try:
        safe = validate_and_transform("DROP TABLE orders")
        print(f"✗ 居然通过了 → {safe}")
    except SqlSecurityError as e:
        print(f"✓ 正确拒绝 → {e}")

    # 测试 4：已有 LIMIT → 不该重复加
    print("\n=== 测试 4：已有 LIMIT ===")
    try:
        safe = validate_and_transform("SELECT * FROM orders LIMIT 10")
        print(f"✓ 通过 → {safe}")
    except SqlSecurityError as e:
        print(f"✗ 拒绝 → {e}")
