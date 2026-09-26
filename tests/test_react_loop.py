"""证据传递与动作校验回归：不读取密钥、不联网、不访问产品数据库。"""
import builtins
import importlib
import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def agent(monkeypatch):
    original_open = builtins.open

    def open_test_key(path, *args, **kwargs):
        if path == "api_key.txt":
            return io.StringIO("offline-test")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as importing:
        importing.setattr(builtins, "open", open_test_key)
        importing.setattr("openai.OpenAI", Mock())
        llm = importlib.import_module("llm")
        loop = importlib.import_module("react_loop")
    monkeypatch.setattr(llm, "client", Mock())
    return llm, loop


def action(tool, **args):
    return {"thought": "下一步", "tool": tool, "args": args}


def run_script(agent, monkeypatch, actions, history=None):
    llm, loop = agent
    decisions = iter(actions)
    final_requests = []

    def complete(messages, stream=False):
        if stream:
            final_requests.append(messages)
            return iter([SimpleNamespace(choices=[
                SimpleNamespace(delta=SimpleNamespace(content="测试回答"))])])
        import json
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(next(decisions), ensure_ascii=False)))])

    monkeypatch.setattr(llm, "_completion", complete)
    monkeypatch.setattr(llm, "_build_system_prompt", lambda: "测试提示")
    events = list(loop.run_react_stream("请回答", full_history=history))
    return events, str(final_requests)


def test_history_and_new_results_reach_answer(agent, monkeypatch):
    _, loop = agent
    from tools import ToolResult
    historical = "[execute_sql({}) → SQL: SELECT 'a]b']\n" + "旧行\n" * 600
    historical += "第三行关键数字 987654"
    full = "新行\n" * 600 + "第21行关键数字 123456"
    monkeypatch.setitem(loop.TOOLS["execute_sql"], "fn", lambda sql: ToolResult(
        content="仅预览", full_content=full, sql=sql, truncated=True))
    actions = [action("read_result", result_id=7),
               action("execute_sql", sql="SELECT 2"),
               action("execute_sql", sql="SELECT 3"),
               {"thought": "完成", "final": "回答"}]
    events, request = run_script(agent, monkeypatch, actions, [
        {"id": 7, "role": "tool", "kind": "result", "content": historical}])
    assert "987654" in request and "123456" in request
    assert "SELECT 2" in request and "SELECT 3" in request and "a]b" in request
    assert events[-1]["status"] == "completed"


@pytest.mark.parametrize("invalid", [
    {}, [], {"thought": "x", "final": "a", "tool": "get_domains", "args": {}},
    {"thought": "x", "tool": "get_domains", "args": []},
])
def test_bad_actions_fail_after_one_repair(agent, monkeypatch, invalid):
    events, request = run_script(agent, monkeypatch, [invalid, invalid])
    assert events[-1]["status"] == "failed"
    assert not any(e["type"] == "tool_call" for e in events)
    assert request == "[]"


def test_bad_action_can_be_repaired(agent, monkeypatch):
    events, _ = run_script(agent, monkeypatch, [
        {}, {"thought": "完成", "final": "你好"}])
    assert events[-1]["status"] == "completed"


@pytest.mark.parametrize("tool,args,error", [
    ("execute_sql", {"sql": 42}, "schema_error"),
    ("get_domains", {"unexpected": True}, "schema_error"),
    ("get_table_schema", {}, "schema_error"),
    ("read_result", {"result_id": "7"}, "schema_error"),
    ("read_result", {"result_id": -1}, "schema_error"),
    ("read_result", {"result_id": 7}, "not_found"),
    ("missing_tool", {}, "unknown_tool"),
])
def test_tool_errors_are_structured(agent, monkeypatch, tool, args, error):
    events, _ = run_script(agent, monkeypatch, [
        action(tool, **args), {"thought": "完成", "final": "说明错误"}])
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["is_error"] and result["error_type"] == error


def test_query_chain_keeps_tail_and_call_identity(agent):
    llm, _ = agent
    content = "x" * 2100 + "TAIL"
    chain = llm._tool_chain([
        {"role": "tool", "name": "read_result", "input": {"result_id": 7},
         "content": content, "kind": "context"}])
    assert "TAIL" in chain and "read_result" in chain and "7" in chain


def test_final_stream_failure_has_failed_terminal(agent, monkeypatch):
    _, loop = agent
    monkeypatch.setattr(loop, "chat", lambda *a, **k: {"thought": "x", "final": "a"})

    def broken_stream(*args, **kwargs):
        yield "部分回答"
        raise RuntimeError("offline failure")

    monkeypatch.setattr(loop, "chat_stream_final", broken_stream)
    events = list(loop.run_react_stream("问题"))
    assert any(e.get("content") == "部分回答" for e in events)
    assert events[-1]["status"] == "failed"
    assert not any(e.get("status") == "completed" for e in events)


def test_invalid_sql_parameter_never_reaches_database(agent, monkeypatch):
    _, loop = agent
    execute = Mock(side_effect=AssertionError("不得执行"))
    monkeypatch.setattr("tools.execute_query", execute)
    result = loop._invoke_tool("execute_sql", {"sql": 42}, [])
    assert result.error_type == "schema_error"
    execute.assert_not_called()


def test_model_cannot_supply_another_history(agent):
    _, loop = agent
    result = loop._invoke_tool("read_result", {"result_id": 7, "history": []}, [])
    assert result.error_type == "schema_error"


@pytest.mark.parametrize("stream", [False, True])
def test_oversized_request_is_not_sent(agent, monkeypatch, stream):
    llm, _ = agent
    monkeypatch.setattr(llm, "WATERMARK_TOKENS", 10)
    with pytest.raises(ValueError, match="context_insufficient"):
        llm._completion([{"role": "user", "content": "长文本" * 50}], stream=stream)
    llm.client.chat.completions.create.assert_not_called()


def test_failed_query_is_not_answer_evidence(agent, monkeypatch):
    _, loop = agent
    from tools import ToolResult
    monkeypatch.setitem(loop.TOOLS["execute_sql"], "fn", lambda sql: ToolResult(
        content="错误信息 SECRET_ERROR_MARKER", is_error=True, error_type="execution"))
    events, request = run_script(agent, monkeypatch, [
        action("execute_sql", sql="SELECT 1"), {"thought": "说明失败", "final": "失败"}])
    assert "SECRET_ERROR_MARKER" not in request
    assert "没有取得成功查询或读取的证据" in request
    assert next(e for e in events if e["type"] == "tool_result")["is_error"]
