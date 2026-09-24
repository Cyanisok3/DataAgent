"""
db.py —— 真数据库连接（SQLite）+ 真实数据导入

数据来源：dbt 官方示例项目 jaffle-shop 的种子数据（GitHub 公开仓库
dbt-labs/jaffle-shop，CC 许可教学数据）。真实数据：
  customers 935 位客户 / orders 61,948 笔订单 / items 90,900 条订单明细
  products 10 个商品 / stores 6 家门店 / supplies 65 条供应记录
覆盖整整一年的真实经营记录（原时间 2024-09 ~ 2025-08）。

两个数据工程处理（写进语义层口径，用户测试"最近 N 天"才有意义）：
  1. 时间平移：把数据末尾对齐"今天"，否则"最近 30 天"永远查不到
  2. 金额换算：原数据是美分（整数），÷100 转成美元（REAL）

对应原版的 DataSource + SqlExecutor。要上 MySQL 只改 DB_URL 一行。
"""
import csv
import os
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text

# SQLite 数据库文件
DB_URL = "sqlite:///business.db"

engine = create_engine(DB_URL, echo=False)  # echo=True 会打印 SQL

DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "jaffle")

# 时间平移：原数据最后一天（2025-08-31）→ 今天，保证"最近 N 天"可用
_ORIG_END = datetime(2025, 8, 31)


def _shift_days(today: datetime | None = None) -> int:
    """返回需要给原始时间加的偏移天数（数据末尾对齐今天）"""
    today = today or datetime.now()
    return (today.replace(hour=0, minute=0, second=0, microsecond=0)
            - _ORIG_END).days


def init_db():
    """建表；表空则从 data/jaffle/*.csv 导入真实数据（幂等：重复启动不重复导）"""
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS customers (
                id TEXT PRIMARY KEY,
                name TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                customer_id TEXT,
                ordered_at TEXT,
                store_id TEXT,
                subtotal REAL,
                tax_paid REAL,
                order_total REAL
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                order_id TEXT,
                sku TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS products (
                sku TEXT PRIMARY KEY,
                name TEXT,
                type TEXT,
                price REAL,
                description TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS stores (
                id TEXT PRIMARY KEY,
                name TEXT,
                opened_at TEXT,
                tax_rate REAL
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS supplies (
                id TEXT,          -- 真实数据中 id 有重复（同一供应品多行），故不设主键
                name TEXT,
                cost REAL,
                perishable INTEGER,
                sku TEXT
            )
        """))
        conn.commit()

        if conn.execute(text("SELECT COUNT(*) FROM orders")).scalar() > 0:
            return  # 已有数据，跳过导入（幂等）

        shift = _shift_days()

        # ─── customers / products / stores / supplies：原样导入 ───
        _load_csv(conn, "raw_customers.csv", "customers", "(id, name)")
        _load_csv(conn, "raw_products.csv", "products",
                  "(sku, name, type, price, description)", price=int)
        _load_csv(conn, "raw_stores.csv", "stores",
                  "(id, name, opened_at, tax_rate)", tax_rate=float)
        _load_csv(conn, "raw_supplies.csv", "supplies",
                  "(id, name, cost, perishable, sku)",
                  cost=float, perishable=lambda v: 1 if v == "True" else 0)

        # ─── orders：时间平移 + 美分转美元 ───
        with open(os.path.join(DATA_DIR, "raw_orders.csv")) as f:
            rows = list(csv.DictReader(f))
        order_rows = []
        for r in rows:
            t = datetime.fromisoformat(r["ordered_at"]) + timedelta(days=shift)
            order_rows.append({
                "id": r["id"],
                "customer_id": r["customer"],
                "ordered_at": t.strftime("%Y-%m-%d %H:%M:%S"),
                "store_id": r["store_id"],
                "subtotal": int(r["subtotal"]) / 100,
                "tax_paid": int(r["tax_paid"]) / 100,
                "order_total": int(r["order_total"]) / 100,
            })
        conn.execute(
            text("INSERT INTO orders (id, customer_id, ordered_at, store_id, "
                 "subtotal, tax_paid, order_total) "
                 "VALUES (:id, :customer_id, :ordered_at, :store_id, "
                 ":subtotal, :tax_paid, :order_total)"),
            order_rows)

        # ─── items：原样导入（id 是 UUID 文本）───
        with open(os.path.join(DATA_DIR, "raw_items.csv")) as f:
            item_rows = [{"id": r["id"], "order_id": r["order_id"], "sku": r["sku"]}
                         for r in csv.DictReader(f)]
        conn.execute(
            text("INSERT INTO items (id, order_id, sku) "
                 "VALUES (:id, :order_id, :sku)"),
            item_rows)
        conn.commit()


def _load_csv(conn, filename, table, columns, **casts):
    """通用 CSV 导入：columns 指定列序，casts 指定需要类型转换的列"""
    with open(os.path.join(DATA_DIR, filename)) as f:
        rows = list(csv.DictReader(f))
    cleaned = []
    for r in rows:
        row = {}
        for c in r:
            v = r[c]
            if c in casts:
                v = casts[c](v)
            row[c] = v
        cleaned.append(row)
    if cleaned:
        conn.execute(text(f"INSERT INTO {table} {columns} "
                          f"VALUES ({', '.join(':' + c for c in cleaned[0])})"),
                     cleaned)


def execute_query(sql: str) -> str:
    """执行 SELECT SQL，返回结果文本"""
    with engine.connect() as conn:
        result = conn.execute(text(sql))

        # 列名
        columns = list(result.keys())
        rows = result.fetchall()

        if not rows:
            return "查询结果为空。"

        # 格式化成文本
        lines = [f"Columns: {columns}", f"Total rows: {len(rows)}"]
        for row in rows[:20]:  # 最多显示 20 行
            lines.append(str(dict(row._mapping)))
        if len(rows) > 20:
            lines.append(f"（已截断，仅显示前 20 行，共 {len(rows)} 行）")

        return "\n".join(lines)


# 自测
if __name__ == "__main__":
    import os
    if os.path.exists("business.db"):
        os.remove("business.db")

    init_db()
    print("=== 数据概览 ===")
    for tbl in ("customers", "orders", "items", "products", "stores", "supplies"):
        with engine.connect() as conn:
            print(f"{tbl}: {conn.execute(text(f'SELECT COUNT(*) FROM {tbl}')).scalar()} 行")

    print("\n=== 时间范围（已平移）===")
    with engine.connect() as conn:
        r = conn.execute(text("SELECT MIN(ordered_at), MAX(ordered_at) FROM orders")).fetchone()
        print(f"{r[0]} ~ {r[1]}")

    print("\n=== 测试：各门店销售额（美元）===")
    print(execute_query(
        "SELECT s.name, ROUND(SUM(o.order_total), 2) AS total "
        "FROM orders o JOIN stores s ON o.store_id = s.id "
        "GROUP BY s.name ORDER BY total DESC"
    ))
    print("\n=== 测试：最近 30 天订单量 ===")
    print(execute_query(
        "SELECT COUNT(*) AS cnt FROM orders "
        "WHERE ordered_at >= DATE('now', '-30 day')"
    ))
