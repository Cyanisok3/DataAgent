"""适配器使用微型临时库；官方 EX 自检使用已校验下载的官方源码，不访问真实模型。"""
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import llm
import session_store
from benchmark.bird_data import load_source
from benchmark.bird_eval import run_question, validate_frozen_run
from benchmark.bird_score import score_one
from benchmark.eval_budget import EvaluationBudget, EvaluationBudgetExceeded
from datasource import CURRENT_SOURCE, DataSource
from sql_guard import SqlSecurityError, prepare_query
from tools import execute_sql, get_metric_caliber, get_table_schema


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "sample" / "sample.sqlite"
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE "Values Table" ("Item ID" INTEGER, amount INTEGER)')
        conn.executemany('INSERT INTO "Values Table" VALUES (?,?)', [(i, i) for i in range(210)])
    return load_source(tmp_path, "sample")


def test_injected_catalog_never_uses_jaffle_metrics(source):
    token = CURRENT_SOURCE.set(source)
    try:
        assert "Item ID" in get_table_schema("Values Table").content
        assert get_table_schema("orders").is_error
        assert "没有该指标定义" in get_metric_caliber("销售额").content
        result = execute_sql('SELECT * FROM "Values Table"')
        assert not result.is_error and result.result["row_count"] == 200
        assert result.result["completeness"] == "truncated"
        assert "201" in result.result["execution_sql"] and "LIMIT" not in result.result["sql"]
    finally:
        CURRENT_SOURCE.reset(token)


@pytest.mark.parametrize("sql", ["SELECT CURRENT_DATE", "SELECT date('now')",
                                  "SELECT datetime()", "SELECT strftime('%Y','now')"])
def test_frozen_clock_rejects_machine_clock_sql(sql):
    with pytest.raises(SqlSecurityError, match="fixed_clock"):
        prepare_query(sql, {}, fixed_clock=True)
    assert "2026-09-24" in prepare_query("SELECT date('2026-09-24')", {}, fixed_clock=True).sql


def test_clock_prompt_uses_injected_date(source):
    frozen = DataSource(source.path, source.tables, source.domains,
                        clock=datetime(2026, 9, 24, tzinfo=ZoneInfo("Asia/Shanghai")))
    token = CURRENT_SOURCE.set(frozen)
    try:
        assert "2026-09-24" in llm._build_system_prompt()
    finally:
        CURRENT_SOURCE.reset(token)


def test_budget_reserves_before_call_and_does_not_overspend():
    budget = EvaluationBudget(100, Decimal(1), Decimal(1), Decimal(1))
    budget.reserve(70, 20)
    with pytest.raises(EvaluationBudgetExceeded):
        budget.reserve(1, 10)
    assert budget.tokens_reserved == 90 and budget.calls_reserved == 1


def test_unlimited_cost_records_usage_without_invented_price():
    budget = EvaluationBudget(None, None, None, None)
    budget.reserve(10_000, 1024)
    assert budget.report()["cost_reserved"] is None
    assert budget.report()["max_cost"] is None
    assert budget.calls_reserved == 1 and budget.tokens_reserved == 11024
    with pytest.raises(ValueError):
        EvaluationBudget(None, Decimal(10), None, None)


def test_full_run_requires_unchanged_scored_debug(tmp_path):
    manifest = {"scope": "full", "question_ids": list(range(500)), "model": "fixed"}
    with pytest.raises(ValueError, match="requires_frozen"):
        validate_frozen_run(manifest, None)
    previous = dict(manifest, scope="debug", question_ids=list(range(20)))
    (tmp_path / "manifest.json").write_text(json.dumps(previous))
    (tmp_path / "score.json").write_text(json.dumps({
        "scope": "debug", "metric": "official_EX_full_SQL",
        "scorer_revision": "abd11b6db92a1c9f809b32f7564c7c71b34d67f0",
        "total": 20, "correct": 1,
        "scores": [{"question_id": qid} for qid in previous["question_ids"]],
    }))
    validate_frozen_run(manifest, tmp_path)
    score_path = tmp_path / "score.json"
    score = json.loads(score_path.read_text())
    score["correct"] = 0
    score_path.write_text(json.dumps(score))
    with pytest.raises(ValueError, match="exact_match_required"):
        validate_frozen_run(manifest, tmp_path)
    score["correct"] = 1
    score_path.write_text(json.dumps(score))
    with pytest.raises(ValueError, match="configuration_changed"):
        validate_frozen_run(dict(manifest, model="changed"), tmp_path)


def test_business_regression_uses_snapshot_and_restores_globals(tmp_path, monkeypatch, source):
    from benchmark import regression_9q
    previous = session_store.DB_PATH, llm.WATERMARK_TOKENS
    monkeypatch.setattr(regression_9q, "DEFAULT_SOURCE", source)
    seen = []
    def fake_question(q, injected, budget, timeout, max_calls, **kwargs):
        seen.append(injected)
        assert kwargs["require_sql"] is False and injected.clock.year == 2026
        return {"status": "completed"}
    monkeypatch.setattr(regression_9q, "run_question", fake_question)
    report = regression_9q.run_regression(tmp_path / "run", execute=True, input_budget=6000)
    assert len(seen) == 9 and seen[0].path != source.path
    assert report["database_unchanged"]
    assert (session_store.DB_PATH, llm.WATERMARK_TOKENS) == previous


def test_full_agent_selects_final_query_not_last_probe(source, monkeypatch):
    requests, selected = [], []
    steps = iter([
        {"tool": "get_table_schema", "args": {"table_name": "Values Table"}},
        {"tool": "execute_sql", "args": {"sql": 'SELECT SUM(amount) FROM "Values Table"'}},
        {"tool": "execute_sql", "args": {"sql": 'SELECT MAX(amount) FROM "Values Table"'}},
    ])
    def complete(messages, phase, stream=False):
        requests.append(messages)
        if phase == "final":
            text = "已经查询。"
        else:
            chain = json.loads(messages[-1]["content"].removeprefix("[本轮工具链] "))
            selected[:] = [e["result"]["result_id"] for e in chain if e.get("result")]
            next_step = next(steps, None)
            action = dict(thought="行动说明", **next_step) if next_step else {
                "thought": "完成", "evidence_ids": [selected[0]], "final_query_id": selected[0]}
            text = json.dumps(action)
        return iter([SimpleNamespace(choices=[
            SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason="stop")])])
    monkeypatch.setattr(llm, "_completion", complete)
    q = {"question_id": 5, "db_id": "sample", "question": "合计？",
         "evidence": "amount 是数量", "SQL": "FORBIDDEN_GOLD_MARKER"}
    budget = EvaluationBudget(100000, Decimal(10), Decimal(1), Decimal(1))
    record = run_question(q, source, budget, 30, 14)
    assert record["status"] == "completed" and "SUM" in record["sql"] and "MAX" not in record["sql"]
    assert record["executed_queries"] == 2
    assert "FORBIDDEN_GOLD_MARKER" not in str(requests)
    with session_store.connection() as conn:
        terminal = json.loads(conn.execute("SELECT payload FROM events WHERE type='done'").fetchone()[0])
        assert terminal["final_query_id"] == record["final_query_id"]


@pytest.mark.parametrize("sql,expected", [('SELECT amount FROM "Values Table"', 1),
                                         ('SELECT amount+1 FROM "Values Table"', 0),
                                         ('DELETE FROM "Values Table"', 0)])
def test_official_ex_correct_wrong_readonly(source, sql, expected):
    assets = Path(__file__).parents[1] / "benchmark" / "data" / "bird-mini-dev"
    if not (assets / "evaluation_ex.py").exists():
        pytest.skip("先运行 python -m benchmark.bird_data 下载固定官方评分源码")
    score = score_one(sql, 'SELECT amount FROM "Values Table"', source.path, assets)
    assert score["res"] == expected


def test_official_ex_hard_timeout(source):
    assets = Path(__file__).parents[1] / "benchmark" / "data" / "bird-mini-dev"
    if not (assets / "evaluation_ex.py").exists():
        pytest.skip("固定官方评分源码未准备")
    sql = 'SELECT SUM(a.amount*b.amount*c.amount*d.amount) FROM "Values Table" a, "Values Table" b, "Values Table" c, "Values Table" d'
    assert score_one(sql, "SELECT 1", source.path, assets, timeout=0.5)["error"] == "timeout"
