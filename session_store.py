"""
session_store.py —— 会话持久化（SQLite 存储层 + 压缩账本层）

重构后职责（L24）：
  存储层   管"事实怎么存"：只追加日志，一条不删
  账本层   管"压缩怎么落"：插入摘要 + 旧行打 replaced_by（只改不删）
  投影层   已拆到 context.py（纯函数，管"给模型看什么"）
  压缩编排 已拆到 compaction.py（锁 + 判定 + 摘要 + 落账本）

依赖方向（单向无环）：context（纯函数）← session_store ← compaction ← main
"""
import os
import sqlite3

from context import (
    CONTEXT_WINDOW_TOKENS,
    SUMMARY_MAX_CHARS,
    KIND_CHAT,
    KIND_CONTEXT,
    KIND_RESULT,
    estimate_tokens,
    project_history,
    find_compressible,
    truncate_sentence,
)

# 测试时可用 SESSION_DB_PATH 指向临时库（不碰产品 sessions.db）
DB_PATH = os.environ.get("SESSION_DB_PATH", "sessions.db")


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 让查询结果可以按列名访问
    return conn


def init_db():
    """建表（第一次启动时调一次）；旧库补列（兼容迁移）+ 回填轮次"""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,          -- user / assistant / tool
            content TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'chat',  -- chat / context / result
            is_summary INTEGER NOT NULL DEFAULT 0,  -- 1 = LLM 摘要消息
            replaced_by INTEGER,         -- 被哪条摘要吸收（账本标记，不删除）
            replaces_range TEXT,         -- 摘要消息：替代了哪些 id，如 "1-7"
            turn INTEGER NOT NULL DEFAULT 0,  -- 轮次号：user 开新轮
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 兼容迁移：老库缺列就补（幂等，列已存在则跳过）
    for col, ddl in [
        ("kind", "TEXT DEFAULT 'chat'"),
        ("is_summary", "INTEGER NOT NULL DEFAULT 0"),
        ("replaced_by", "INTEGER"),
        ("replaces_range", "TEXT"),
        ("turn", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass  # 列已存在

    # 回填轮次：老数据（turn 全 0）按 id 顺序，user 开新轮，其余沿用
    rows = conn.execute(
        "SELECT id, session_id, role FROM messages ORDER BY id"
    ).fetchall()
    cur_turn = 0
    last_sid = None
    for r in rows:
        if r["session_id"] != last_sid:
            last_sid, cur_turn = r["session_id"], 1
        elif r["role"] == "user":
            cur_turn += 1
        conn.execute("UPDATE messages SET turn = ? WHERE id = ?",
                     (cur_turn, r["id"]))
    conn.commit()
    conn.close()


def next_turn(session_id: str) -> int:
    """返回该会话下一个轮次号（当前最大轮 + 1；空会话从 1 开始）"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COALESCE(MAX(turn), 0) AS m FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    conn.close()
    return row["m"] + 1


def save_message(session_id: str, role: str, content: str,
                 kind: str = KIND_CHAT, turn: int | None = None):
    """存一条消息。kind 在写入时声明；turn 不传则沿用当前最大轮"""
    conn = _get_conn()
    if turn is None:
        turn = conn.execute(
            "SELECT COALESCE(MAX(turn), 0) AS m FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()["m"]
    conn.execute(
        "INSERT INTO messages (session_id, role, content, kind, turn) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, role, content, kind, turn),
    )
    conn.commit()
    conn.close()


def load_messages(session_id: str) -> list[dict]:
    """加载某个会话的全部历史消息（日志：只追加，一条不删）"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, role, content, kind, is_summary, replaced_by, "
        "replaces_range, turn FROM messages "
        "WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "role": r["role"], "content": r["content"],
             "kind": r["kind"], "is_summary": r["is_summary"],
             "replaced_by": r["replaced_by"], "replaces_range": r["replaces_range"],
             "turn": r["turn"]}
            for r in rows]


# ─── 写账本层 ───────────────────────────────────────────────

def apply_summary(session_id: str, segment: list[dict], summary_text: str):
    """压缩落库：插入摘要行（is_summary=1, replaces_range）+ 旧行打 replaced_by。
    只改不删——被吸收的消息一行都不删，只是不再进投影。
    摘要在句子边界截断（L25 修复：旧版 [:400] 硬切会切断数字/口径）。"""
    conn = _get_conn()
    first_id = segment[0]["id"]
    last_id = segment[-1]["id"]
    safe_summary = truncate_sentence(summary_text, SUMMARY_MAX_CHARS)
    cur = conn.execute(
        "INSERT INTO messages (session_id, role, content, kind, is_summary, "
        "replaces_range) VALUES (?, ?, ?, ?, 1, ?)",
        (session_id, "assistant", safe_summary, KIND_CHAT,
         f"{first_id}-{last_id}"),
    )
    summary_id = cur.lastrowid
    ids = [m["id"] for m in segment]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE messages SET replaced_by = ? WHERE id IN ({placeholders})",
        [summary_id] + ids,
    )
    conn.commit()
    conn.close()


def usage_stats(session_id: str) -> dict:
    """历史投影的 token 用量分项（L25 token 口径）：
    系统提示词由 llm 侧计算、main 组装总量；compressed 是真实值。"""
    history = load_messages(session_id)
    projected = project_history(history)
    dialogue_tokens = sum(estimate_tokens(m["content"])
                          for m in projected
                          if m["role"] in ("user", "assistant"))
    tool_tokens = sum(estimate_tokens(m["content"])
                      for m in projected if m["role"] == "tool")
    summaries = [m for m in history
                 if m.get("is_summary") and not m.get("replaced_by")]
    return {
        "session_id": session_id,
        "projected_tokens": dialogue_tokens + tool_tokens,
        "context_window_tokens": CONTEXT_WINDOW_TOKENS,
        "dialogue_tokens": dialogue_tokens,
        "tool_tokens": tool_tokens,
        "compressed": bool(summaries),
    }


# 自测（安全化：永远用 /tmp 测试库，绝不触碰产品 sessions.db）
if __name__ == "__main__":
    import os

    DB_PATH = "/tmp/session_store_selftest.db"
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    init_db()

    sid = "test-session-001"
    save_message(sid, "user", "各区域销售额是多少？")
    save_message(sid, "tool", '{"domains": [...], "tables": [...]}',
                 kind=KIND_CONTEXT)
    save_message(sid, "tool", "华东 4197.5，华北 2898.0，华南 2298.0",
                 kind=KIND_RESULT)
    save_message(sid, "assistant", "各区域销售额如下：华东 4197.5...")
    save_message(sid, "user", "那华北呢？")

    print("=== 全量日志（事实，永不删）===")
    for m in load_messages(sid):
        print(f"[{m['role']}:{m['kind']} turn={m['turn']}] {m['content'][:30]}...")

    print("\n=== 投影（观点：context 已被确定性过滤）===")
    for m in project_history(load_messages(sid)):
        print(f"[{m['role']}] {m['content'][:30]}...")

    print("\n=== 压缩判定（短对话应 0 → 不触发）===")
    print(f"可压缩段: {len(find_compressible(load_messages(sid)))} 条")
