"""
session_store.py —— 会话持久化（SQLite）+ 投影（简化事件溯源）

参考 DeepSeek Harness 的设计：
  - 日志（SQLite）只追加，永不删除 → 事实完整可审计
  - 投影（project_history）决定"给模型看什么" → 观点有界可压缩
  - 压缩不删事实：越过水位的旧消息只是不投影，库里原封不动

消息分类（kind）：投影"判断"的时机前置到写入那一刻——
  写日志时声明这条消息是"一次性引导"还是"可引用事实"，
  投影只按确定性规则过滤，不做内容智能判断（DSH 哲学）。
"""
import sqlite3
from datetime import datetime

DB_PATH = "sessions.db"

# 水位：投影时最多给模型看最近几轮对话（1 轮 = 1 条 user + 1 条 assistant）
MAX_TURNS = 3

# 事实性工具结果最多保留几条（支持一步内多次查询后的追问）
MAX_TOOL_RESULTS = 2

# 确定性剪枝：单条工具结果超过该长度，投影时只留开头（head 截断）
# 参考 DSH 的 toolResultPruner（head/middle/tail 三段裁剪），我们简化做 head
TOOL_RESULT_MAX_CHARS = 500

# 消息分类（写日志时声明，投影时按规则过滤）
KIND_CHAT = "chat"        # 对话消息（user/assistant）
KIND_CONTEXT = "context"  # 引导性元数据（get_context 结果）：消费完即弃
KIND_RESULT = "result"    # 事实性查询结果（execute_sql 结果）：可被追问引用


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 让查询结果可以按列名访问
    return conn


def init_db():
    """建表（第一次启动时调一次）；旧库补 kind 列（兼容迁移）"""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,          -- user / assistant / tool
            content TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'chat',  -- chat / context / result
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 兼容：如果表已存在但没有 kind 列（L17 之前的库），补上
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN kind TEXT DEFAULT 'chat'")
    except sqlite3.OperationalError:
        pass  # 列已存在
    conn.commit()
    conn.close()


def save_message(session_id: str, role: str, content: str, kind: str = KIND_CHAT):
    """存一条消息（kind 在写入时声明——判断前置）"""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO messages (session_id, role, content, kind) VALUES (?, ?, ?, ?)",
        (session_id, role, content, kind),
    )
    conn.commit()
    conn.close()


def load_messages(session_id: str) -> list[dict]:
    """加载某个会话的全部历史消息（日志：只追加，一条不删）"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT role, content, kind FROM messages WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    conn.close()
    return [{"role": r["role"], "content": r["content"], "kind": r["kind"]}
            for r in rows]


def project_history(history: list[dict]) -> list[dict]:
    """
    投影：从完整日志选出"该给模型看什么"（确定性规则，非智能判断）

    规则：
      1. 对话骨架：最近 MAX_TURNS 轮的 user/assistant
      2. 工具结果：只投影 kind=result（事实性查询结果），
         kind=context（引导性元数据）一律丢弃——模型已基于它写完 SQL，
         消费完即弃，重新投影只会浪费 token
      3. 事实性结果最多保留最近 MAX_TOOL_RESULTS 条
      4. 超长结果 head 截断（确定性剪枝）

    旧消息不删除、不修改——只是不投影。压缩改的是观点，不是事实。
    """
    # ① 对话骨架（只要 user/assistant，过滤掉 tool）
    dialogue = [m for m in history if m["role"] in ("user", "assistant")]
    skeleton = dialogue[-(MAX_TURNS * 2):]          # 越过水位，只留尾部

    # ② 事实性工具结果（kind=result；kind=context 是引导信息，不进模型视野）
    factual = [m for m in history
               if m["role"] == "tool" and m.get("kind") == KIND_RESULT]
    for m in factual[-MAX_TOOL_RESULTS:]:
        content = m["content"]
        if len(content) > TOOL_RESULT_MAX_CHARS:    # ③ 确定性剪枝
            content = (
                content[:TOOL_RESULT_MAX_CHARS]
                + f"\n...（已截断，完整结果共 {len(content)} 字，存于日志可审计）"
            )
        skeleton.append({"role": "tool", "content": content})

    return skeleton


# 自测
if __name__ == "__main__":
    import os
    # 测试前删掉旧库
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    init_db()

    sid = "test-session-001"
    save_message(sid, "user", "各区域销售额是多少？")
    # get_context 的元数据：kind=context（引导性，消费完即弃）
    save_message(sid, "tool", '{"domains": [...], "tables": [...], "metrics": [...]}',
                 kind=KIND_CONTEXT)
    # execute_sql 的结果：kind=result（事实性，可被追问引用）
    save_message(sid, "tool", "华东 4197.5，华北 2898.0，华南 2298.0",
                 kind=KIND_RESULT)
    save_message(sid, "assistant", "各区域销售额如下：华东 4197.5...")
    save_message(sid, "user", "那华北呢？")

    print("=== 全量日志（事实，永不删）===")
    for m in load_messages(sid):
        print(f"[{m['role']}:{m['kind']}] {m['content'][:30]}...")

    print("\n=== 投影（观点：context 已被确定性过滤）===")
    for m in project_history(load_messages(sid)):
        print(f"[{m['role']}] {m['content'][:30]}...")
