import json

import pytest

import llm
from context import estimate_tokens, serialize
from llm import parse_action
from run_context import CURRENT_RUN, RunContext


def test_parse_action_extracts_first_valid_action_from_model_envelope():
    content = (
        '先检查结构。\n\n{"thought":"读取表","tool":"get_table_schema",'
        '"args":{"table_name":"orders"}}\n\n'
        '{"thought":"结束","evidence_ids":[]}'
    )
    action = parse_action(content)
    assert action == {
        "thought": "读取表",
        "tool": "get_table_schema",
        "args": {"table_name": "orders"},
    }


def test_parse_action_rejects_output_without_a_valid_contract():
    with pytest.raises(json.JSONDecodeError, match="no_valid_action"):
        parse_action('<｜｜DSML｜｜ invoke name="get_tables">')


def test_completion_reserves_context_token_estimate(monkeypatch):
    reservations = []
    fake_client = type("Client", (), {
        "chat": type("Chat", (), {
            "completions": type("Completions", (), {
                "create": staticmethod(lambda **kwargs: kwargs),
            })(),
        })(),
    })()
    monkeypatch.setattr(llm, "_get_client", lambda: fake_client)
    run = RunContext(reserve_request=lambda input_size, output_size:
                     reservations.append((input_size, output_size)))
    token = CURRENT_RUN.set(run)
    messages = [{"role": "user", "content": "统计三家门店"}]
    try:
        llm._completion(messages, "decision")
    finally:
        CURRENT_RUN.reset(token)
    assert reservations == [(estimate_tokens(serialize(messages)) + 2048, llm.OUTPUT_TOKENS)]
