"""复用生产会话执行器跑 Mini-Dev；默认仅预检，--execute 才请求真实模型。"""
import argparse
import json
import math
import os
import platform
import sqlite3
import threading
import time
from dataclasses import asdict
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path

import llm
import session_store
from benchmark.bird_data import (
    DATA_REVISION,
    SCORER_REVISION,
    file_hash,
    load_source,
    prepare,
    write_json,
)
from benchmark.eval_budget import EvaluationBudget
from context import serialize
from event_logger import usage_stats
from run_context import RunContext
from session_runner import run_session


def code_snapshot():
    root = Path(__file__).resolve().parents[1]
    paths = sorted(root.glob("*.py")) + sorted((root / "benchmark").glob("*.py"))
    return {str(p.relative_to(root)): file_hash(p) for p in paths}


def validate_frozen_run(manifest, debug_run):
    if debug_run is None:
        raise ValueError("full_run_requires_frozen_debug_run")
    previous = json.loads((debug_run / "manifest.json").read_text())
    score = json.loads((debug_run / "score.json").read_text())
    if (previous["scope"] != "debug" or score["scope"] != "debug"
            or score["metric"] != "official_EX_full_SQL"
            or score["scorer_revision"] != SCORER_REVISION
            or score["total"] != 20
            or score["correct"] < 1
            or [row["question_id"] for row in score["scores"]] != previous["question_ids"]):
        raise ValueError("scored_20_question_debug_with_exact_match_required")
    for key in manifest.keys() - {"scope", "question_ids", "tuning"}:
        if manifest[key] != previous.get(key):
            raise ValueError(f"frozen_configuration_changed:{key}")


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
                "api_timeout": float(os.getenv("DATAAGENT_API_TIMEOUT", "10")),
                "python": platform.python_version(),
                "dependencies": {name: version(name) for name in
                                 ("openai", "pydantic", "SQLAlchemy", "sqlglot")},
                "input_watermark": llm.WATERMARK_TOKENS,
                "code_sha256": code_snapshot(),
                "budget_config": {
                    "max_tokens": args.max_tokens,
                    "max_cost": args.max_cost,
                    "unlimited_cost": args.unlimited_cost,
                    "input_price": args.input_price,
                    "output_price": args.output_price,
                    "currency": args.currency,
                },
                "tuning": "fixed first 20 stratified by database; no automatic tuning"}
    # 包含 CSV 描述在内的目录快照，以便发现说明文件变化。
    manifest["catalogs"] = {id_: [asdict(t) for t in source.tables] for id_, source in sources.items()}
    if not args.execute:
        return dict(manifest, status="preflight_only_no_model_calls")
    if not args.model or not math.isfinite(args.timeout) or args.timeout <= 0 or args.max_calls <= 0:
        raise ValueError("model_and_positive_limits_required")
    if not args.unlimited_cost and args.max_cost is None:
        raise ValueError("explicit_cost_limit_or_unlimited_cost_required")
    budget = EvaluationBudget(args.max_tokens, Decimal(args.max_cost) if args.max_cost else None,
                              Decimal(args.input_price) if args.input_price else None,
                              Decimal(args.output_price) if args.output_price else None)
    if args.scope == "full":
        validate_frozen_run(manifest, args.frozen_debug_run)
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
            print(serialize({"progress": len(records), "total": len(questions),
                             "question_id": q["question_id"], "status": record["status"],
                             "error": record["error"]}), flush=True)
    provider_usage = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [r.get("usage", {}).get("round_usage", {}).get(key) for r in records]
        values = [value for value in values if value is not None]
        provider_usage[key] = sum(values) if values else None
    report = {
        "total": len(records),
        "completed": sum(r["status"] == "completed" for r in records),
        "failed": sum(r["status"] == "failed" for r in records),
        "not_run": sum(r["status"] == "not_run" for r in records),
        "model_calls": sum(r.get("model_calls", 0) for r in records),
        "tool_calls": sum(r.get("tool_calls", 0) for r in records),
        "executed_queries": sum(r.get("executed_queries", 0) for r in records),
        "cached_calls": sum(r.get("cached_calls", 0) for r in records),
        "provider_usage": provider_usage,
        "sessions_with_missing_usage": sum(
            bool(r.get("usage", {}).get("missing_usage_phases")) for r in records),
        "elapsed_seconds": round(sum(r.get("elapsed_seconds", 0) for r in records), 3),
        "compressed_sessions": sum(bool(r.get("usage", {}).get("compressed")) for r in records),
        "budget": budget.report(),
    }
    write_json(args.output / "summary.json", report)
    # 官方文件格式，缺失预测保留空 SQL，绝不缩小评分分母。
    write_json(args.output / "predict_mini_dev.json", {
        str(i): (r.get("sql") or "") + "\t----- bird -----\t" + r["db_id"] for i, r in enumerate(records)})
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=Path(__file__).resolve().parent / "data" / "bird-mini-dev")
    parser.add_argument("--databases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scope", choices=["debug", "full"], default="debug")
    parser.add_argument("--model", default=llm.MODEL)
    parser.add_argument("--max-calls", type=int, default=14)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-tokens", type=int)
    cost = parser.add_mutually_exclusive_group()
    cost.add_argument("--max-cost")
    cost.add_argument("--unlimited-cost", action="store_true")
    parser.add_argument("--input-price")
    parser.add_argument("--output-price")
    parser.add_argument("--frozen-debug-run", type=Path)
    parser.add_argument("--currency", choices=["CNY", "USD"], default="CNY")
    parser.add_argument("--execute", action="store_true")
    print(json.dumps(run_evaluation(parser.parse_args()), ensure_ascii=False, indent=2))
