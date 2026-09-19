"""
session_store.py —— 会话持久化（SQLite）

现在每次请求都是无状态的：用户问"各区域销售额"，下次问"那华北呢？"
系统完全不记得之前聊过什么。

这模块解决：把每轮对话存到 SQLite，下次带同一个 session_id 来，
自动加载历史消息。
"""
import sqlite3
from datetime import datetime

DB_PATH = "sessions.db"


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 让查询结果可以按列名访问
    return conn


def init_db():
    """建表（第一次启动时调一次）"""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,          -- user / assistant / tool
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def save_message(session_id: str, role: str, content: str):
    """存一条消息"""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
        (session_id, role, content),
    )
    conn.commit()
    conn.close()


def load_messages(session_id: str) -> list[dict]:
    """加载某个会话的全部历史消息"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    conn.close()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


# 自测
if __name__ == "__main__":
    import os
    # 测试前删掉旧库
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    init_db()

    sid = "test-session-001"
    save_message(sid, "user", "各区域销售额是多少？")
    save_message(sid, "assistant", "华东 123 万，华北 98 万...")

    print("=== 加载历史 ===")
    for m in load_messages(sid):
        print(f"[{m['role']}] {m['content']}")
