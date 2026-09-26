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
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import create_engine, text

from datasource import CURRENT_SOURCE
from run_context import CURRENT_RUN
from sql_guard import MAX_ROWS

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

        if (conn.execute(text("SELECT COUNT(*) FROM orders")).scalar() or 0) > 0:
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


MAX_RESULT_BYTES = 1_000_000
MAX_CELL_BYTES = 64_000
BUSINESS_PATH = Path(__file__).with_name("business.db")


def execute_query(sql: str, *, path: str | Path | None = None, timeout: float = 5) -> dict:
    """只读、有时限、有界读取。返回行数不是全库匹配行数。"""
    target = Path(path or CURRENT_SOURCE.get().path).resolve()
    uri = "file:" + quote(str(target), safe="/") + "?mode=ro"
    query_engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    deadline = time.monotonic() + timeout
    run = CURRENT_RUN.get()

    def interrupted():
        return time.monotonic() >= deadline or bool(run and (
            run.cancel.is_set() or time.monotonic() >= run.deadline))

    try:
        if run:
            run.check()
        with query_engine.connect() as conn:
            driver = conn.connection.driver_connection
            assert driver is not None
            driver.execute("PRAGMA query_only = ON")
            driver.set_progress_handler(lambda: int(interrupted()), 100)
            # 限制 SQLite 本身的分配，不等到巨型字符串已经进入 Python 才拒绝。
            if hasattr(driver, "setlimit"):
                driver.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_RESULT_BYTES)
            try:
                cursor = conn.exec_driver_sql(sql)
                columns = list(cursor.keys())
                rows = []
                size = 0
                for _ in range(MAX_ROWS + 1):
                    row = cursor.fetchone()
                    if row is None:
                        break
                    values = list(row)
                    for value in values:
                        if isinstance(value, bytes):
                            raise TypeError("resource_limit: 不支持二进制单元格")
                        if len(str(value).encode("utf-8")) > MAX_CELL_BYTES:
                            raise ValueError("resource_limit: 单元格过大，请缩小查询")
                    size += len(json.dumps(values, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                    if size > MAX_RESULT_BYTES:
                        raise ValueError("resource_limit: 结果过大，请缩小查询")
                    rows.append(values)
                if run:
                    run.check()
                if interrupted():
                    raise TimeoutError("query_timeout")
                return {"columns": columns, "rows": rows[:MAX_ROWS],
                        "row_count": min(len(rows), MAX_ROWS), "truncated": len(rows) > MAX_ROWS}
            finally:
                driver.set_progress_handler(None, 0)
    except Exception as exc:
        if run:
            run.check()
        if interrupted():
            raise TimeoutError("query_timeout") from exc
        raise
    finally:
        query_engine.dispose()
