"""会话事实与压缩账本。追加事实，事务更新终态，不删除历史。"""

import json
import os
import sqlite3
from contextlib import contextmanager

from context import serialize

DB_PATH = os.environ.get(
    "SESSION_DB_PATH", os.path.join(os.path.dirname(__file__), "sessions.db")
)


@contextmanager
def connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    with connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL, kind TEXT DEFAULT 'chat',
            is_summary INTEGER DEFAULT 0, replaced_by INTEGER, replaces_range TEXT,
            turn INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        for name, declaration in {
            "kind": "TEXT DEFAULT 'chat'",
            "is_summary": "INTEGER DEFAULT 0",
            "replaced_by": "INTEGER",
            "replaces_range": "TEXT",
            "turn": "INTEGER DEFAULT 0",
            "source_ids": "TEXT",
            "logical_position": "INTEGER",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {declaration}")
        conn.execute("""CREATE TABLE IF NOT EXISTS turns (
            session_id TEXT, turn INTEGER, status TEXT NOT NULL,
            answer TEXT DEFAULT '', error TEXT, mode TEXT DEFAULT 'answer',
            started_at TEXT DEFAULT CURRENT_TIMESTAMP, ended_at TEXT,
            PRIMARY KEY(session_id, turn))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS query_results (
            result_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn INTEGER NOT NULL,
            payload TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, session_id TEXT, turn INTEGER, type TEXT,
            payload TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS model_calls (
            id INTEGER PRIMARY KEY, session_id TEXT, turn INTEGER, phase TEXT,
            request TEXT, response TEXT, config TEXT, view TEXT,
            usage TEXT, status TEXT, error TEXT, elapsed_ms INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        _backfill_turns(conn)
        for row in conn.execute(
            "SELECT * FROM turns WHERE status='running'"
        ).fetchall():
            _finish(
                conn,
                row["session_id"],
                row["turn"],
                "failed",
                row["answer"],
                "process_interrupted",
                row["mode"],
            )
        conn.execute(
            "UPDATE model_calls SET status='failed',error='process_interrupted' WHERE status='running'"
        )


def _backfill_turns(conn):
    """按会话顺序补零值；已有编号只验证不重写，歧义立即回滚。"""
    current: dict[str, int] = {}
    for row in conn.execute("SELECT * FROM messages ORDER BY id").fetchall():
        if row["is_summary"]:
            continue
        sid, turn = row["session_id"], row["turn"]
        previous = current.get(sid, 0)
        expected = previous + 1 if row["role"] == "user" else previous
        if turn:
            if (row["role"] == "user" and turn <= previous) or (
                row["role"] != "user" and previous and turn != previous
            ):
                raise ValueError(f"turn_migration_conflict: message {row['id']}")
        else:
            if not expected:
                raise ValueError(f"turn_migration_orphan: message {row['id']}")
            turn = expected
            conn.execute("UPDATE messages SET turn=? WHERE id=?", (turn, row["id"]))
        current[sid] = turn


def begin_turn(sid: str, question: str) -> int:
    with connection() as conn:
        turn = conn.execute(
            """SELECT MAX(n)+1 FROM (
            SELECT COALESCE(MAX(turn),0) n FROM messages WHERE session_id=?
            UNION ALL SELECT COALESCE(MAX(turn),0) FROM turns WHERE session_id=?)""",
            (sid, sid),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO turns(session_id,turn,status) VALUES (?,?,'running')",
            (sid, turn),
        )
        conn.execute(
            "INSERT INTO messages(session_id,role,content,turn) VALUES (?,'user',?,?)",
            (sid, question, turn),
        )
        return turn


def mark_cancelling(sid, turn):
    """阻塞调用停止前标记取消待收尾；仅 running 可转换，幂等。"""
    with connection() as conn:
        conn.execute(
            "UPDATE turns SET status='cancelling' "
            "WHERE session_id=? AND turn=? AND status='running'",
            (sid, turn),
        )


def _finish(conn, sid, turn, status, answer, error, mode, evidence=None):
    changed = conn.execute(
        """UPDATE turns SET status=?, answer=?, error=?, mode=?,
        ended_at=CURRENT_TIMESTAMP WHERE session_id=? AND turn=?
        AND status IN ('running','cancelling')""",
        (status, answer, error, mode, sid, turn),
    ).rowcount
    if changed:
        conn.execute(
            "INSERT INTO messages(session_id,role,content,turn) VALUES (?,'assistant',?,?)",
            (sid, answer, turn),
        )
        _event(
            conn,
            sid,
            turn,
            {"type": "done", "status": status, "error": error, "mode": mode,
             "evidence_ids": (evidence or {}).get("evidence_ids", []),
             "final_query_id": (evidence or {}).get("final_query_id")},
        )
    return bool(changed)


def finish_turn(sid, turn, status, answer, error=None, mode="answer", evidence=None):
    if status not in {"completed", "failed", "cancelled"}:
        raise ValueError("invalid terminal status")
    with connection() as conn:
        return _finish(conn, sid, turn, status, answer, error, mode, evidence)


def _event(conn, sid, turn, event):
    conn.execute(
        "INSERT INTO events(session_id,turn,type,payload) VALUES (?,?,?,?)",
        (sid, turn, event["type"], serialize(event)),
    )


def save_event(sid, turn, event):
    """工具事实及其事件同事务，调用方在提交后发布。"""
    with connection() as conn:
        if event["type"] == "tool_result":
            result = event.get("result")
            if result and event["name"] == "execute_sql" and not event.get("cached"):
                conn.execute(
                    "INSERT INTO query_results(result_id,session_id,turn,payload) VALUES (?,?,?,?)",
                    (result["result_id"], sid, turn, serialize(result)),
                )
            kind = (
                "error"
                if event.get("is_error")
                else (
                    "result" if result and event["name"] == "execute_sql" else "context"
                )
            )
            conn.execute(
                """INSERT INTO messages(session_id,role,content,kind,turn)
                VALUES (?,'tool',?,?,?)""",
                (sid, event["output"], kind, turn),
            )
        if event["type"] in {"text", "text_chunk"}:
            conn.execute(
                "UPDATE turns SET answer=answer || ? WHERE session_id=? AND turn=?",
                (event["content"], sid, turn),
            )
        _event(conn, sid, turn, event)


def load_messages(sid) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT m.*, COALESCE(t.status,'completed') status,
            COALESCE(t.mode,'answer') mode FROM messages m LEFT JOIN turns t
            ON m.session_id=t.session_id AND m.turn=t.turn
            WHERE m.session_id=? ORDER BY m.id""",
            (sid,),
        ).fetchall()
    messages = [dict(r) for r in rows]
    completed_turns = [m["turn"] for m in messages if m["status"] == "completed"]
    latest = max(completed_turns, default=0)
    for m in messages:
        m["source_ids"] = json.loads(m["source_ids"]) if m["source_ids"] else []
        m["protected"] = m["turn"] == latest and m["mode"] == "clarify"
    return messages


def load_results(sid) -> dict[str, dict]:
    with connection() as conn:
        results = {
            r["result_id"]: json.loads(r["payload"])
            for r in conn.execute(
                "SELECT * FROM query_results WHERE session_id=? ORDER BY rowid", (sid,)
            )
        }
        legacy = conn.execute(
            """SELECT id,content,turn FROM messages WHERE session_id=?
            AND role='tool' AND kind='result' ORDER BY id""",
            (sid,),
        ).fetchall()
    new_turns = {r.get("turn") for r in results.values()}
    for row in legacy:
        if row["turn"] not in new_turns:
            key = f"legacy:{row['id']}"
            results[key] = {
                "result_id": key,
                "legacy_content": row["content"],
                "completeness": "unknown",
                "turn": row["turn"],
            }
    return results


def apply_summary(sid: str, segment: list[dict], summary: str) -> int:
    """比较精确来源快照后原子替换；摘要沿用最早逻辑位置。"""
    ids = [m["id"] for m in segment]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("invalid_summary_sources")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id=? AND id IN ("
            + ",".join("?" for _ in ids)
            + ")",
            [sid, *ids],
        ).fetchall()
        snapshots = {m["id"]: m for m in segment}
        if len(rows) != len(ids) or any(
            any(r[key] != snapshots[r["id"]].get(key) for key in
                ("role", "content", "turn", "is_summary", "logical_position", "replaced_by"))
            or json.loads(r["source_ids"] or "[]") != snapshots[r["id"]].get("source_ids", [])
            for r in rows
        ):
            raise ValueError("stale_summary_sources")
        position = min(m.get("logical_position") or m["id"] for m in segment)
        cur = conn.execute(
            """INSERT INTO messages
            (session_id,role,content,is_summary,turn,source_ids,logical_position)
            VALUES (?,'assistant',?,1,?,?,?)""",
            (sid, summary, min(m["turn"] for m in segment), serialize(ids), position),
        )
        summary_id = cur.lastrowid
        conn.executemany(
            "UPDATE messages SET replaced_by=? WHERE id=? AND session_id=?",
            [(summary_id, id_, sid) for id_ in ids],
        )
        return summary_id
