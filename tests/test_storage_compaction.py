import json
import sqlite3

import pytest

import compaction
import llm
import session_store as store
from context import visible_history


def ended(sid, question, answer):
    turn = store.begin_turn(sid, question)
    store.finish_turn(sid, turn, "completed", answer)
    return turn


def test_result_scope_legacy_and_compressed_discovery():
    turn = ended("a", "销售额", "完成")
    result = {"result_id": "r", "rows": [[1]], "turn": turn, "completeness": "complete"}
    event = {
        "type": "tool_result",
        "name": "execute_sql",
        "result": result,
        "output": "data",
    }
    store.save_event("a", turn, event)
    assert store.load_results("b") == {}
    assert list(store.load_results("a")) == ["r"]
    store.apply_summary("a", store.load_messages("a")[:2], "提取摘要")
    assert store.load_results("a")["r"] == result
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO messages(session_id,role,content,kind,turn) VALUES ('old','tool','旧结果','result',1)"
        )
    legacy = next(iter(store.load_results("old").values()))
    assert legacy["completeness"] == "unknown" and "rows" not in legacy


def test_terminal_unique_and_partial_not_projected():
    turn = store.begin_turn("a", "问题")
    assert store.finish_turn("a", turn, "failed", "半句", "failure")
    assert not store.finish_turn("a", turn, "completed", "不允许覆盖")
    assert visible_history(store.load_messages("a")) == []
    with store.connection() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM events WHERE type='done'").fetchone()[0]
            == 1
        )


def test_cancelling_marks_intermediate_and_finish_accepts_it():
    turn = store.begin_turn("a", "问题")
    store.mark_cancelling("a", turn)
    with store.connection() as conn:
        assert conn.execute(
            "SELECT status FROM turns WHERE session_id='a'"
        ).fetchone()[0] == "cancelling"
    # 重复标记幂等；终态更新可从 cancelling 流转
    store.mark_cancelling("a", turn)
    assert store.finish_turn("a", turn, "cancelled", "部分", "client_disconnected")
    with store.connection() as conn:
        assert conn.execute(
            "SELECT status FROM turns WHERE session_id='a'"
        ).fetchone()[0] == "cancelled"


def test_startup_recovers_running_preserving_partial():
    turn = store.begin_turn("a", "问题")
    store.save_event("a", turn, {"type": "text_chunk", "content": "已生成"})
    store.init_db()
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM turns").fetchone()
        assert (row["status"], row["answer"], row["error"]) == (
            "failed",
            "已生成",
            "process_interrupted",
        )
    store.init_db()


def test_backfill_interleaved_sessions_preserves_existing_and_idempotence():
    with store.connection() as conn:
        for sid, role, turn in [
            ("a", "user", 0),
            ("b", "user", 0),
            ("a", "assistant", 0),
            ("b", "assistant", 0),
            ("a", "user", 2),
            ("a", "assistant", 0),
        ]:
            conn.execute(
                "INSERT INTO messages(session_id,role,content,turn) VALUES (?,?,?,?)",
                (sid, role, "text", turn),
            )
    store.init_db()
    assert [m["turn"] for m in store.load_messages("a")] == [1, 1, 2, 2]
    assert [m["turn"] for m in store.load_messages("b")] == [1, 1]
    before = store.load_messages("a")
    store.init_db()
    assert store.load_messages("a") == before


def test_backfill_conflict_rolls_back():
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO messages(session_id,role,content,turn) VALUES ('a','user','one',2)"
        )
        conn.execute(
            "INSERT INTO messages(session_id,role,content,turn) VALUES ('a','user','two',1)"
        )
    with pytest.raises(ValueError, match="conflict"):
        store.init_db()
    assert [m["turn"] for m in store.load_messages("a")] == [2, 1]


def test_summary_stale_and_second_summary_logical_order():
    ended("a", "一", "A 100")
    ended("a", "二", "B 200")
    first = store.load_messages("a")
    id1 = store.apply_summary("a", first[:2], "A 100")
    with pytest.raises(ValueError, match="stale"):
        store.apply_summary("a", first[:2], "篡改")
    visible = visible_history(store.load_messages("a"))
    assert visible[0]["id"] == id1
    id2 = store.apply_summary("a", visible, "A 100; B 200")
    summary = visible_history(store.load_messages("a"))[0]
    assert summary["id"] == id2 and summary["logical_position"] == first[0]["id"]


def test_summary_failure_does_not_commit(monkeypatch):
    ended("a", "修正", "冗余" * 3000)
    ended("a", "可以", "已完成")
    turn = store.begin_turn("a", "接着")
    monkeypatch.setattr(llm, "WATERMARK_TOKENS", 1000)
    monkeypatch.setattr(llm, "_build_system_prompt", lambda: "s")
    monkeypatch.setattr(compaction, "summarize_history", lambda p: '{"keep":[999]}')
    compaction.maybe_compress("a", turn, "接着", {})
    assert not any(m["is_summary"] for m in store.load_messages("a"))
    with store.connection() as conn:
        event = json.loads(
            conn.execute(
                "SELECT payload FROM events WHERE type='compaction'"
            ).fetchone()[0]
        )
        assert event["status"] == "rejected" and event["source_version"]


def test_extract_summary_commits_once_under_pressure(monkeypatch):
    ended("a", "修正口径", "确认100元\n\n" + "多余文字" * 2000)
    ended("a", "可以", "任务完成")
    turn = store.begin_turn("a", "接着")
    monkeypatch.setattr(llm, "WATERMARK_TOKENS", 1000)
    monkeypatch.setattr(llm, "_build_system_prompt", lambda: "s")
    monkeypatch.setattr(compaction, "summarize_history", lambda p: '{"keep":[]}')
    compaction.maybe_compress("a", turn, "接着", {})
    summaries = [m for m in store.load_messages("a") if m["is_summary"]]
    assert len(summaries) == 1
    assert "修正口径" in summaries[0]["content"] and "100" in summaries[0]["content"]
    view = llm.decision_view(
        [{"role": "user", "content": "接着"}], store.load_messages("a")
    )
    assert "可以" in str(view.messages) and "任务完成" in str(view.messages)


def test_old_database_copy_migration(tmp_path, monkeypatch):
    # 用实际旧结构构建夹具，不使用产品库路径。
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT)"
        )
        conn.execute("INSERT INTO messages VALUES (1,'a','user','问题')")
        conn.execute("INSERT INTO messages VALUES (2,'a','assistant','回答')")
    monkeypatch.setattr(store, "DB_PATH", str(path))
    store.init_db()
    assert [m["turn"] for m in store.load_messages("a")] == [1, 1]
