"""
db.py —— 真数据库连接（SQLite）

对应项目原版的 DataSource + SqlExecutor。
现在用 SQLite（文件级，开箱即用），后面可换成 MySQL。
"""
from sqlalchemy import create_engine, text

# SQLite 数据库文件
DB_URL = "sqlite:///business.db"

engine = create_engine(DB_URL, echo=False)  # echo=True 会打印 SQL


def init_db():
    """建表 + 插假数据（第一次跑时调一次）"""
    with engine.connect() as conn:
        # 建 orders 表
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id INTEGER PRIMARY KEY,
                user_id INTEGER,
                paid_amount REAL,
                status TEXT,
                is_test INTEGER,
                settle_time TEXT,
                region TEXT
            )
        """))

        # 检查是否已有数据
        count = conn.execute(text("SELECT COUNT(*) FROM orders")).scalar()
        if count == 0:
            # 插假数据
            conn.execute(text("""
                INSERT INTO orders (order_id, user_id, paid_amount, status, is_test, settle_time, region) VALUES
                (1, 101, 1299.00, 'paid', 0, '2026-09-01 10:30:00', '华东'),
                (2, 102, 2499.50, 'paid', 0, '2026-09-02 14:20:00', '华东'),
                (3, 103, 899.00,  'paid', 0, '2026-09-03 09:15:00', '华北'),
                (4, 104, 1999.00, 'paid', 0, '2026-09-05 16:45:00', '华北'),
                (5, 105, 699.00,  'paid', 0, '2026-09-06 11:00:00', '华南'),
                (6, 106, 1599.00, 'paid', 0, '2026-09-07 13:30:00', '华南'),
                (7, 107, 399.00,  'paid', 0, '2026-09-08 10:00:00', '华东'),
                (8, 108, 999.00,  'paid', 1, '2026-09-08 15:00:00', '华东'),
                (9, 109, 2999.00, 'pending', 0, '2026-09-09 12:00:00', '华北')
            """))
        conn.commit()


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
            lines.append(f"... 还有 {len(rows) - 20} 行未显示")

        return "\n".join(lines)


# 自测
if __name__ == "__main__":
    import os
    if os.path.exists("business.db"):
        os.remove("business.db")

    init_db()
    print("=== 测试：查所有已支付订单 ===")
    print(execute_query("SELECT region, SUM(paid_amount) as total FROM orders WHERE status='paid' AND is_test=0 GROUP BY region"))
