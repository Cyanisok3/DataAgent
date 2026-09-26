"""复用生产会话执行器跑 Mini-Dev；默认仅预检，--execute 才请求真实模型。"""
import argparse
import json
import sqlite3
import threading
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import llm
import session_store
from context import serialize
from event_logger import usage_stats
from run_context import RunContext
from scripts.bird_data import (
    DATA_REVISION,
    SCORER_REVISION,
    file_hash,
    load_source,
    prepare,
    write_json,
)
from scripts.eval_budget import EvaluationBudget
from session_runner import run_session


def select_questions(assets, scope):
    prepare(assets)
    questions = json.loads((assets / "prompts.json").read_text())
    if scope == "debug":
        ids = set(json.loads((assets / "debug_ids.json").read_text()))
        questions = [q for q in questions if q["question_id"] in ids]
    if len({q["question_id"] for q in questions}) != len(questions):
        raise ValueError("duplicate_question_ids")
    return questions


def run_question(q, source, budget, timeout, max_calls, *, session_id=None, require_sql=True):
    sid = session_id or f"bird-{q['question_id']}"
    events: list[dict] = []
    run = RunContext(max_calls=max_calls, deadline=time.monotonic() + timeout,
                     reserve_request=budget.reserve)
    # 显式字段投影：不会把整条带 gold 的原始样本传给 Agent。
    message = serialize({"question": q["question"], "evidence": q["evidence"],
                         "request": "请回答并用 final_query_id 指定回答本题的最终查询，不能选探查查询。"})
    if not require_sql:
        message = q["question"]
    started = time.monotonic()
    run_session(sid, message, events.append, threading.Event(), source=source, run=run)
    terminal = next((e for e in reversed(events) if e["type"] == "done"), {})
    final_id = terminal.get("final_query_id")
    result = session_store.load_results(sid).get(final_id or "", {})
    valid = terminal.get("status") == "completed" and (bool(result.get("sql")) or not require_sql)
    calls = [e for e in events if e["type"] == "tool_result"]
    return {"question_id": q["question_id"], "db_id": q["db_id"],
            "status": "completed" if valid else "failed",
            "error": None if valid else terminal.get("error") or "no_valid_final_query",
            "final_query_id": final_id, "sql": result.get("sql") if valid else None,
            "evidence_ids": terminal.get("evidence_ids", []),
            "answer": "".join(e["content"] for e in events if e["type"] in {"text", "text_chunk"}),
            "execution_sql": result.get("execution_sql") if valid else None,
            "completeness": result.get("completeness") if valid else None,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "tool_calls": len(calls), "cached_calls": sum(bool(e.get("cached")) for e in calls),
            "executed_queries": sum(e["name"] == "execute_sql" and not e.get("cached") for e in calls),
            "model_calls": run.model_calls, "usage": usage_stats(sid)}


def run_evaluation(args):
    questions = select_questions(args.assets, args.scope)
    sources = {id_: load_source(args.databases, id_) for id_ in sorted({q["db_id"] for q in questions})}
    hashes = {id_: file_hash(source.path) for id_, source in sources.items()}
    manifest = {"scope": args.scope, "question_ids": [q["question_id"] for q in questions],
                "data_revision": DATA_REVISION, "scorer_revision": SCORER_REVISION,
                "prompts_sha256": file_hash(args.assets / "prompts.json"),
                "oracle_sha256": file_hash(args.assets / "oracle.json"),
                "databases": hashes, "sqlite_version": sqlite3.sqlite_version,
                "model": args.model, "context_window": llm.CONTEXT_WINDOW,
                "output_tokens": llm.OUTPUT_TOKENS, "max_calls_per_question": args.max_calls,
                "timeout_per_question": args.timeout, "input": "question+evidence",
                "tuning": "fixed first 20 stratified by database; no automatic tuning"}
    # 包含 CSV 描述在内的目录快照，以便发现说明文件变化。
    manifest["catalogs"] = {id_: [asdict(t) for t in source.tables] for id_, source in sources.items()}
    if not args.execute:
        return dict(manifest, status="preflight_only_no_model_calls")
    if not args.model or args.timeout <= 0 or args.max_calls <= 0:
        raise ValueError("model_and_positive_limits_required")
    budget = EvaluationBudget(args.max_tokens, Decimal(args.max_cost),
                              Decimal(args.input_price), Decimal(args.output_price))
    manifest["budget"] = budget.report()
    manifest["pricing"] = {"currency": args.currency, "input_per_million": args.input_price,
                           "output_per_million": args.output_price}
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output / "manifest.json", manifest)
    session_store.DB_PATH = str(args.output / "sessions.db")
    session_store.init_db()
    llm.MODEL = args.model
    records, exhausted = [], False
    with (args.output / "records.jsonl").open("x", encoding="utf-8") as stream:
        for q in questions:
            if exhausted:
                record = {"question_id": q["question_id"], "db_id": q["db_id"],
                          "status": "not_run", "error": "evaluation_budget_exhausted", "sql": None}
            else:
                record = run_question(q, sources[q["db_id"]], budget, args.timeout, args.max_calls)
                exhausted = record["error"] == "EvaluationBudgetExceeded"
            records.append(record)
            stream.write(serialize(record) + "\n")
            stream.flush()
    report = {"total": len(records), "completed": sum(r["status"] == "completed" for r in records),
              "not_run": sum(r["status"] == "not_run" for r in records), "budget": budget.report()}
    write_json(args.output / "summary.json", report)
    # 官方文件格式，缺失预测保留空 SQL，绝不缩小评分分母。
    write_json(args.output / "predict_mini_dev.json", {
        str(i): (r.get("sql") or "") + "\t----- bird -----\t" + r["db_id"] for i, r in enumerate(records)})
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=Path("data/bird-mini-dev"))
    parser.add_argument("--databases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scope", choices=["debug", "full"], default="debug")
    parser.add_argument("--model")
    parser.add_argument("--max-calls", type=int, default=14)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-tokens", type=int, default=0)
    parser.add_argument("--max-cost", default="0")
    parser.add_argument("--input-price", default="0")
    parser.add_argument("--output-price", default="0")
    parser.add_argument("--currency", choices=["CNY", "USD"], default="CNY")
    parser.add_argument("--execute", action="store_true")
    print(json.dumps(run_evaluation(parser.parse_args()), ensure_ascii=False, indent=2))
