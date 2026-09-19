"""
sql_guard.py —— SQL 安全护栏：AST 校验 + 自动改写

对应 L4 学的四道安检（JSQLParser 的 Python 版）：
  1. 只允许 SELECT（拒绝 INSERT/UPDATE/DELETE/DROP）
  2. 禁止 INTO 子句（防止 SELECT INTO 写表）
  3. 自动加 LIMIT（防止全表扫描）
  4. （超时截断留到接真数据库时做，这里先做前 3 道）

核心思想：模型生成的 SQL 绝不能裸跑——先过 AST 校验，安全了才执行。
"""
import sqlglot
from sqlglot import exp


class SqlSecurityError(Exception):
    """SQL 安全校验失败"""
    pass


def validate_and_transform(sql: str) -> str:
    """
    输入：模型生成的原始 SQL
    输出：安全、可执行的 SQL（自动加了 LIMIT 等）
    异常：SqlSecurityError（任何一道安检没过）
    """
    # ─── 第 0 步：解析 SQL 成 AST ───
    try:
        ast = sqlglot.parse_one(sql)
    except Exception as e:
        raise SqlSecurityError(f"SQL 语法错误: {e}")

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

    # ─── 返回安全后的 SQL 字符串 ───
    return ast.sql()


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
