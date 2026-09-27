"""从冻结源码续跑基础设施中断的评测；原目录只读，语义失败不重试。"""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

INFRA_ERRORS = {"APIStatusError", "APIConnectionError", "APITimeoutError",
                "RateLimitError", "AuthenticationError", "PermissionDeniedError",
                "runner_interrupted", "provider_unavailable"}
TABLES = ("messages", "turns", "query_results", "events", "model_calls")


def retained_records(previous):
    manifest = json.loads((previous / "manifest.json").read_text())
    rows = [json.loads(line) for line in (previous / "records.jsonl").read_text().splitlines()]
    ids = [r["question_id"] for r in rows]
    if ids != manifest["question_ids"][:len(ids)]:
        raise ValueError("previous_record_order_mismatch")
    return {r["question_id"]: r for r in rows
            if r["status"] != "not_run" and r.get("error") not in INFRA_ERRORS}


def copy_sessions(previous, destination, records, budget):
    """只复制保留题目的事实；新题不继承上次接口失败的上下文。"""
    with (
        sqlite3.connect(previous.resolve().as_uri() + "?mode=ro", uri=True) as src,
        sqlite3.connect(destination) as dst,
    ):
        for qid in records:
            sid = f"bird-{qid}"
            states = src.execute("SELECT status FROM turns WHERE session_id=?", (sid,)).fetchall()
            if not states or any(s[0] in {"running", "cancelling"} for s in states):
                raise ValueError("retained_session_not_terminal")
            for table in TABLES:
                if (src.execute(f"PRAGMA table_info({table})").fetchall()
                        != dst.execute(f"PRAGMA table_info({table})").fetchall()):
                    raise ValueError("session_schema_mismatch")
                for row in src.execute(f"SELECT * FROM {table} WHERE session_id=?", (sid,)):
                    placeholders = ",".join("?" for _ in row)
                    dst.execute(f"INSERT INTO {table} VALUES ({placeholders})", row)
            for view, config in src.execute(
                "SELECT view,config FROM model_calls WHERE session_id=?", (sid,)
            ):
                budget.reserve(json.loads(view)["estimated_tokens"] + 2048,
                               json.loads(config)["max_tokens"])


def guarded_create(create, errors, log_path):
    def invoke(*args, **kwargs):
        if any(e["status_code"] in {401, 402, 403} for e in errors):
            raise RuntimeError("provider_unavailable")
        try:
            return create(*args, **kwargs)
        except Exception as exc:
            event = {"status_code": getattr(exc, "status_code", None),
                     "error": type(exc).__name__}
            errors.append(event)
            with log_path.open("a") as stream:
                stream.write(json.dumps(event) + "\n")
            raise
    return invoke


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "previous", "debug-run", "databases", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    options = parser.parse_args()
    sys.path.insert(0, str(options.snapshot.resolve()))
    import llm
    from benchmark import bird_eval

    previous = json.loads((options.previous / "manifest.json").read_text())
    if bird_eval.code_snapshot() != previous["code_sha256"]:
        raise ValueError("frozen_source_mismatch")
    kept = retained_records(options.previous)
    original = bird_eval.run_question
    errors = []
    initialized = False

    def resume(q, source, budget, timeout, max_calls):
        nonlocal initialized
        if not initialized:
            copy_sessions(options.previous / "sessions.db", options.output / "sessions.db", kept, budget)
            bird_eval.write_json(options.output / "continuation.json", {
                "previous": str(options.previous.resolve()), "snapshot": str(options.snapshot.resolve()),
                "retained_question_ids": list(kept),
                "policy": "retry infrastructure failures only; preserve semantic failures",
                "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            })
            initialized = True
        if q["question_id"] in kept:
            record = kept[q["question_id"]]
            if record["db_id"] != q["db_id"]:
                raise ValueError("retained_database_mismatch")
            return record
        if any(e["status_code"] in {401, 402, 403} for e in errors):
            return {"question_id": q["question_id"], "db_id": q["db_id"],
                    "status": "not_run", "error": "provider_unavailable", "sql": None}
        return original(q, source, budget, timeout, max_calls)

    bird_eval.run_question = resume
    client = llm._get_client()
    client.chat.completions.create = guarded_create(
        client.chat.completions.create, errors, options.output / "provider_errors.jsonl")
    args = SimpleNamespace(
        assets=options.snapshot / "benchmark/data/bird-mini-dev", databases=options.databases,
        output=options.output, scope="full", model=previous["model"],
        max_calls=previous["max_calls_per_question"], timeout=previous["timeout_per_question"],
        frozen_debug_run=options.debug_run, execute=True, **previous["budget_config"],
    )
    print(json.dumps(bird_eval.run_evaluation(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
