"""协议容错不能绕过完整动作、参数和工具安全边界。"""

import json
import threading
from types import SimpleNamespace

import pytest

import llm
import react_loop
import session_runner
import session_store as store
from context import build_view, visible_history
from model_actions import parse_action
from run_context import CURRENT_RUN, RunContext


def dsml(name="get_tables", params=None):
    body = "".join(
        f'<｜｜DSML｜｜ parameter name="{key}" string="{kind}">{value}'
        '</｜｜DSML｜｜ parameter>' for key, kind, value in (params or [])
    )
    return (f'<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="{name}">{body}'
            '</｜｜DSML｜｜ invoke></｜｜DSML｜｜ calls>')


@pytest.mark.parametrize("name,params,expected", [
    ("get_tables", [("question", "true", "schools & enrollment")],
     {"question": "schools & enrollment"}),
    ("get_domains", [], {}),
    ("get_table_schema", [("args", "false", '{"table_name":"major"}'),
                          ("tool", "true", "get_table_schema"),
                          ("thought", "true", "读取结构")], {"table_name": "major"}),
    ("execute_sql", [("sql", "true", "SELECT 'a<b' WHERE 1 < 2")],
     {"sql": "SELECT 'a<b' WHERE 1 < 2"}),
    ("read_result", [("result_id", "true", "r"), ("offset", "false", "10")],
     {"result_id": "r", "offset": 10}),
])
def test_dsml_normalizes_one_action(name, params, expected):
    action = parse_action("先读取结构\n" + dsml(name, params))
    assert action["tool"] == name and action["args"] == expected
    assert action["thought"]


@pytest.mark.parametrize("content", [
    dsml("unknown"),
    dsml(params=[("tool", "true", "execute_sql")]),
    dsml(params=[("question", "true", "a"), ("question", "true", "b")]),
    dsml(params=[("args", "false", "{}"), ("question", "true", "q")]),
    dsml(params=[("args", "false", "[]")]),
    dsml(params=[("args", "false", '{"question":')]),
    dsml().replace('</｜｜DSML｜｜ invoke>', ''),
    dsml().replace('</｜｜DSML｜｜ invoke>', 'unexpected</｜｜DSML｜｜ invoke>'),
    dsml(params=[("question", "true", dsml("get_domains"))]),
])
def test_dsml_rejects_ambiguous_or_incomplete_calls(content):
    with pytest.raises(json.JSONDecodeError):
        parse_action(content)


def test_dsml_uses_first_call_and_does_not_skip_invalid_first():
    assert parse_action(dsml("get_domains") + dsml())["tool"] == "get_domains"
    with pytest.raises(json.JSONDecodeError):
        parse_action(dsml("unknown") + dsml("get_domains"))
    action = '{"thought":"回答","evidence_ids":[]}'
    assert parse_action(action + dsml("get_domains"))["evidence_ids"] == []
    assert parse_action(dsml(params=[("question", "true", action)]))["tool"] == "get_tables"


def test_dsml_tool_action_wrapper_uses_whitelisted_tool():
    params = [("thought", "true", "读取"), ("tool", "true", "get_domains"),
              ("args", "false", "{}")]
    assert parse_action(dsml("ToolAction", params))["tool"] == "get_domains"
    params[1] = ("tool", "true", "unknown")
    with pytest.raises(json.JSONDecodeError):
        parse_action(dsml("ToolAction", params))


def test_dsml_still_uses_parameter_and_sql_guards():
    for name, params, error in [
        ("execute_sql", [("sql", "false", "42")], "schema_error"),
        ("get_domains", [("unexpected", "true", "x")], "schema_error"),
        ("execute_sql", [("sql", "true", "SELECT readfile('/tmp/x')")], "security"),
    ]:
        action = parse_action(dsml(name, params))
        assert react_loop._invoke_tool(name, action["args"], {}).error_type == error


def chunks(content, reason):
    yield SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=content), finish_reason=reason)])
    yield SimpleNamespace(choices=[], usage=SimpleNamespace(
        model_dump=lambda: {"completion_tokens": 1024, "total_tokens": 1040}))


@pytest.mark.parametrize("content", [
    '{"thought":"读取","tool":"get_domains","args":{}}\n余下说明',
    dsml("get_domains"),
])
def test_length_decision_recovers_complete_action_and_logs_truncation(monkeypatch, content):
    monkeypatch.setattr(llm, "_completion", lambda *a, **k: chunks(content, "length"))
    turn = store.begin_turn("a", "q")
    run = RunContext(session_id="a", turn=turn)
    token = CURRENT_RUN.set(run)
    try:
        assert llm.chat([{"role": "user", "content": "q"}])["tool"] == "get_domains"
        assert run.repairs == 0
    finally:
        CURRENT_RUN.reset(token)
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM model_calls").fetchone()
    assert row["response"] == content and row["status"] == "truncated"
    assert row["finish_reason"] == "length" and row["error"] == "OutputTruncated"
    assert json.loads(row["usage"])["completion_tokens"] == 1024


def test_length_incomplete_action_gets_only_one_repair(monkeypatch):
    calls = []

    def complete(*args, **kwargs):
        calls.append(args)
        return chunks('{"thought":', "length")

    monkeypatch.setattr(llm, "_completion", complete)
    with pytest.raises(json.JSONDecodeError):
        llm.chat([{"role": "user", "content": "q"}])
    assert len(calls) == 2


def test_final_length_keeps_partial_text_but_never_completes(monkeypatch):
    monkeypatch.setattr(react_loop, "chat", lambda *a: {
        "thought": "回答", "evidence_ids": [], "mode": "answer"})
    monkeypatch.setattr(llm, "_completion", lambda *a, **k: chunks("部分回答", "length"))
    events = []
    session_runner.run_session("a", "q", events.append, threading.Event())
    with store.connection() as conn:
        turn = conn.execute("SELECT * FROM turns").fetchone()
        call = conn.execute("SELECT * FROM model_calls").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM events WHERE type='done'").fetchone()[0] == 1
    assert turn["answer"] == "部分回答" and turn["status"] == "failed"
    assert turn["error"] == "OutputTruncated"
    assert call["finish_reason"] == "length" and call["status"] == "truncated"
    assert events[-1]["status"] == "failed"
    assert visible_history(store.load_messages("a")) == []


@pytest.mark.parametrize("reason", [None, "content_filter", "tool_calls"])
def test_other_finish_reasons_are_not_salvaged(monkeypatch, reason):
    monkeypatch.setattr(llm, "_completion", lambda *a, **k: chunks(
        '{"thought":"回答","evidence_ids":[]}', reason))
    with pytest.raises(llm.ProviderResponseIncomplete):
        llm.chat([{"role": "user", "content": "q"}])


def test_summary_length_rejected(monkeypatch):
    monkeypatch.setattr(llm, "_completion", lambda *a, **k: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"keep":[0]}'),
                                 finish_reason="length")]))
    with pytest.raises(llm.OutputTruncated):
        list(llm._call(build_view("s", [], []), "summary"))


def test_finish_reason_migration_is_additive_and_idempotent():
    with store.connection() as conn:
        conn.execute("ALTER TABLE model_calls DROP COLUMN finish_reason")
        conn.execute("INSERT INTO model_calls(id,response,status) VALUES (1,'原始输出','completed')")
    store.init_db()
    store.init_db()
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM model_calls WHERE id=1").fetchone()
    assert row["response"] == "原始输出" and row["status"] == "completed"
    assert row["finish_reason"] is None
