"""离线动作及最终请求证据检查，不读取密钥或产品数据库。"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import llm
import react_loop as loop
from context import ContextInsufficient
from tools import ToolResult, tool_catalog


def action(tool, **args):
    return {"thought": "下一步", "tool": tool, "args": args}


def answer(*ids):
    return {"thought": "足够证据", "evidence_ids": list(ids), "mode": "answer"}


def run_script(monkeypatch, actions, results=None):
    decisions = iter(actions)
    final_requests = []

    def stream_chunks(text):
        # 把完整文本拆成 3 个 chunk，模拟供应商逐 token 返回。
        step = max(1, len(text) // 3)
        parts = [text[i:i+step] for i in range(0, len(text), step)]
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            yield SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=part),
                    finish_reason="stop" if last else None)],
                usage=None)
        yield SimpleNamespace(choices=[], usage=SimpleNamespace(
            model_dump=lambda: {"prompt_tokens": 10, "completion_tokens": 5,
                                "total_tokens": 15}))

    def complete(messages, phase, stream=False):
        if not stream:
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(next(decisions))))])
        if phase == "decision":
            return stream_chunks(json.dumps(next(decisions), ensure_ascii=False))
        final_requests.append(messages)
        return stream_chunks("回答")

    monkeypatch.setattr(llm, "_completion", complete)
    monkeypatch.setattr(llm, "_build_system_prompt", lambda: "测试提示")
    return list(loop.run_react_stream("请回答", results=results)), str(final_requests)


def test_historical_pages_and_new_query_all_reach_final_request(monkeypatch):
    old = {
        "result_id": "old",
        "sql": "SELECT old",
        "rows": [["旧行" * 700], [123], [987654]],
        "completeness": "complete",
    }
    new = {
        "result_id": "new",
        "execution_sql": "SELECT new LIMIT 201",
        "rows": [[i] for i in range(30)] + [["第31行TAIL"]],
        "completeness": "truncated",
    }
    monkeypatch.setitem(
        loop.TOOLS["execute_sql"],
        "fn",
        lambda sql: ToolResult(content="新结果", result=new),
    )
    events, request = run_script(
        monkeypatch,
        [
            action("read_result", result_id="old", offset=0, limit=2),
            action("read_result", result_id="old", offset=2),
            action("execute_sql", sql="SELECT 1"),
            action("read_result", result_id="new", offset=20),
            answer("old", "new"),
        ],
        {"old": old},
    )
    assert "987654" in request and "第31行TAIL" in request and "SELECT old" in request
    assert "truncated" in request
    assert events[-1]["status"] == "completed"


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        [],
        {"thought": "x", "evidence_ids": [], "tool": "get_domains", "args": {}},
        {"thought": "x", "tool": "get_domains", "args": []},
        {"thought": "x", "final": "不再接受随后丢弃的答案"},
    ],
)
def test_bad_action_fails_after_single_repair(monkeypatch, invalid):
    events, request = run_script(monkeypatch, [invalid, invalid])
    assert events[-1]["status"] == "failed" and request == "[]"
    assert not any(e["type"] == "tool_call" for e in events)


def test_bad_action_repaired(monkeypatch):
    events, _ = run_script(monkeypatch, [{}, answer()])
    assert events[-1]["status"] == "completed"


@pytest.mark.parametrize(
    "name,args,error",
    [
        ("execute_sql", {"sql": 42}, "schema_error"),
        ("get_domains", {"unexpected": True}, "schema_error"),
        ("get_table_schema", {}, "schema_error"),
        ("read_result", {"result_id": 7}, "schema_error"),
        ("read_result", {"result_id": "missing"}, "not_found"),
        ("read_result", {"result_id": "r", "offset": -1}, "schema_error"),
        ("read_result", {"result_id": "r", "results": {}}, "schema_error"),
        ("missing_tool", {}, "unknown_tool"),
    ],
)
def test_structured_errors(name, args, error):
    assert loop._invoke_tool(name, args, {}).error_type == error


def test_unknown_answer_reference_fails(monkeypatch):
    events, requests = run_script(monkeypatch, [answer("another-session")])
    assert events[-1]["error"] == "invalid_evidence_ids" and requests == "[]"


def test_repeated_sql_reuses_result(monkeypatch):
    execute = Mock(
        return_value=ToolResult(
            content="数据", result={"result_id": "r", "rows": [[1]]}
        )
    )
    monkeypatch.setitem(loop.TOOLS["execute_sql"], "fn", execute)
    events, _ = run_script(
        monkeypatch,
        [
            action("execute_sql", sql="SELECT 1"),
            action("execute_sql", sql="SELECT 1"),
            answer("r"),
        ],
    )
    execute.assert_called_once()
    assert any(e.get("cached") for e in events)


def test_stream_error_retains_partial_and_failed(monkeypatch):
    monkeypatch.setattr(loop, "chat", lambda *a: answer())

    def broken(*args):
        yield "部分回答"
        raise RuntimeError("offline failure")

    monkeypatch.setattr(loop, "chat_stream_final", broken)
    events = list(loop.run_react_stream("问题"))
    assert any(e.get("content") == "部分回答" for e in events)
    assert events[-1]["status"] == "failed"


def test_call_chain_keeps_identity_and_tail():
    chain = llm._tool_chain(
        [
            {
                "role": "tool",
                "name": "read_result",
                "input": {"result_id": "r"},
                "content": "x" * 5000 + "TAIL",
            }
        ]
    )
    assert "TAIL" in chain and "read_result" in chain and '"r"' in chain


def test_oversized_request_not_sent(monkeypatch):
    complete = Mock()
    monkeypatch.setattr(llm, "_completion", complete)
    monkeypatch.setattr(llm, "WATERMARK_TOKENS", 10)
    with pytest.raises(ContextInsufficient):
        llm.chat([{"role": "user", "content": "长文本" * 50}])
    complete.assert_not_called()


def test_registry_is_single_schema_source():
    schema = tool_catalog()
    assert schema["get_tables"]["parameters"]["required"] == ["question"]
    assert not schema["get_tables"]["parameters"]["additionalProperties"]
    assert "results" not in schema["read_result"]["parameters"]["properties"]


def test_max_iterations_fails(monkeypatch):
    monkeypatch.setattr(loop, "chat", lambda *a: action("get_domains"))
    assert list(loop.run_react_stream("问题"))[-1]["error"] == "max_iters"


def test_provider_context_length_error_is_structured(monkeypatch):
    class ProviderLengthError(Exception):
        code = "context_length_exceeded"
        message = "maximum context length exceeded"

    def raise_length(*a, **k):
        raise ProviderLengthError

    monkeypatch.setattr(llm, "_completion", raise_length)
    view = llm.decision_view([{"role": "user", "content": "q"}])
    with pytest.raises(llm.ContextLengthExceeded):
        list(llm._call(view, "decision"))


def test_react_loop_maps_context_length_to_structured_error(monkeypatch):
    def raise_length(*a, **k):
        raise llm.ContextLengthExceeded

    monkeypatch.setattr(loop, "chat", raise_length)
    events = list(loop.run_react_stream("问题"))
    assert events[-1]["status"] == "failed"
    assert events[-1]["error"] == "context_length_exceeded"
