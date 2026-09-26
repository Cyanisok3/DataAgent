"""固定业务快照的九题轨迹探针；与 BIRD 独立，默认不调用模型。"""
import argparse
import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import compaction
import llm
import session_store
from benchmark.bird_data import file_hash, write_json
from benchmark.bird_eval import code_snapshot, run_question
from benchmark.eval_budget import EvaluationBudget
from datasource import DEFAULT_SOURCE

QUESTIONS = [
    "今年各门店的总销售额？按高到低",
    "最近90天各门店销售额和订单量",
    "最近90天哪种商品销量最高？",
    "可以",
    "最近一年每个月各门店销售额明细",
    "布鲁克林哪个月销售额最高？",
    "各门店客单价和活跃客户数",
    "刚才的90天销售额合计和占比",
    "整体总结门店运营特点",
]


def run_regression(output: Path, *, execute=False, input_budget=None, timeout=120.0):
    """每种预算单独运行、独立日志；终态完成不等于数值正确。"""
    if input_budget is not None and input_budget <= 0:
        raise ValueError("positive_input_budget_required")
    manifest = {
        "questions": QUESTIONS, "clock": "2026-09-24T00:00:00+08:00",
        "model": llm.MODEL, "output_tokens": llm.OUTPUT_TOKENS,
        "input_budget": input_budget or llm.WATERMARK_TOKENS,
        "max_calls": 14, "timeout": timeout, "code_sha256": code_snapshot(),
        "status": "execution_requested" if execute else "preflight_only_no_model_calls",
    }
    if not execute:
        return manifest
    output.mkdir(parents=True, exist_ok=False)
    path = output / "business-snapshot.db"
    original = sqlite3.connect(DEFAULT_SOURCE.path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        with sqlite3.connect(path) as snapshot:
            original.backup(snapshot)
    finally:
        original.close()
    manifest["database_sha256"] = file_hash(path)
    write_json(output / "manifest.json", manifest)
    source = replace(DEFAULT_SOURCE, path=path,
                     clock=datetime(2026, 9, 24, tzinfo=ZoneInfo("Asia/Shanghai")))
    previous = (session_store.DB_PATH, llm.WATERMARK_TOKENS, compaction.WATERMARK_TOKENS)
    budget = EvaluationBudget(None, None, None, None)
    records = []
    try:
        session_store.DB_PATH = str(output / "sessions.db")
        session_store.init_db()
        llm.WATERMARK_TOKENS = compaction.WATERMARK_TOKENS = manifest["input_budget"]
        for number, question in enumerate(QUESTIONS, 1):
            record = run_question(
                {"question_id": number, "question": question, "evidence": "", "db_id": "business"},
                source, budget, timeout, 14, session_id="business-9q", require_sql=False)
            records.append(record)
            write_json(output / f"question-{number}.json", record)
        with session_store.connection() as conn:
            audits = [json.loads(row[0]) for row in conn.execute(
                "SELECT payload FROM events WHERE type='compaction' ORDER BY id")]
        report = {"total": len(records), "terminal_completed": sum(
            r["status"] == "completed" for r in records),
            "records": records, "compaction": audits, "budget": budget.report(),
            "numeric_correctness": "requires SQL/result review; completion is not correctness",
            "database_unchanged": file_hash(path) == manifest["database_sha256"]}
        write_json(output / "summary.json", report)
        return report
    finally:
        session_store.DB_PATH, llm.WATERMARK_TOKENS, compaction.WATERMARK_TOKENS = previous


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input-budget", type=int, help="压低应用输入预算以检查压缩；不保证压缩有收益")
    parser.add_argument("--execute", action="store_true", help="显式允许这九题真实模型调用，不设费用上限")
    args = parser.parse_args()
    print(json.dumps(run_regression(args.output, execute=args.execute,
                                    input_budget=args.input_budget), ensure_ascii=False, indent=2))
