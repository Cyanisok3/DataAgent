"""固定官方 EX 实现；独立只读子进程负责执行与硬超时，不加展示 LIMIT。"""
import argparse
import ast
import json
import math
import multiprocessing
import sqlite3
import sys
from pathlib import Path

from benchmark.bird_data import (
    ASSETS,
    SCORER_REVISION,
    database_path,
    file_hash,
    write_json,
)


def official_functions(directory: Path):
    namespace = {"connect_db": readonly_connection}
    for filename, function in [("evaluation_ex.py", "calculate_ex"),
                               ("evaluation_utils.py", "execute_sql")]:
        path = directory / filename
        if file_hash(path) != ASSETS[filename][1]:
            raise ValueError("official_scorer_hash_mismatch")
        tree = ast.parse(path.read_text())
        nodes: list[ast.stmt] = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function]
        if len(nodes) != 1:
            raise ValueError("official_function_missing")
        # 原函数体未经修改；不加载官方文件中与 SQLite 无关的驱动和 CLI。
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)  # noqa: S102 — 固定 SHA 校验后的官方函数
    return namespace["execute_sql"], namespace["calculate_ex"]


def readonly_connection(dialect, path):
    if dialect != "SQLite":
        raise ValueError("SQLite_only")
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    def authorize(action, arg1, arg2, database, trigger):
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
        if action not in allowed or (action == sqlite3.SQLITE_FUNCTION and
                                    arg2 in {"load_extension", "readfile", "writefile"}):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(authorize)
    return conn


def _score_worker(channel, predicted, gold, db_path, assets):
    try:
        if sys.platform.startswith("linux"):
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
        execute, compare = official_functions(Path(assets))
        correct = execute(predicted, gold, db_path, "SQLite", compare)
        channel.send({"res": correct, "error": None})
    except Exception as exc:  # noqa: BLE001 — 评分错误也留在分母
        channel.send({"res": 0, "error": type(exc).__name__})
    finally:
        channel.close()


def score_one(predicted, gold, db_path: Path, assets: Path, timeout=30.0):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("positive_finite_timeout_required")
    ctx = multiprocessing.get_context("spawn")
    reader, writer = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_score_worker, args=(writer, predicted, gold, str(db_path), str(assets)))
    process.start()
    writer.close()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(2)
        if process.is_alive():
            process.kill()
            process.join()
        reader.close()
        return {"res": 0, "error": "timeout"}
    try:
        return reader.recv() if reader.poll() else {"res": 0, "error": "worker_failed"}
    except EOFError:
        return {"res": 0, "error": "worker_failed"}
    finally:
        reader.close()


def score_run(run_dir: Path, assets: Path, root: Path, timeout: float):
    if (run_dir / "score.json").exists():
        raise FileExistsError("评分结果已存在，请保留原记录，不隐式重跑")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    records = [json.loads(line) for line in (run_dir / "records.jsonl").read_text().splitlines()]
    if [r["question_id"] for r in records] != manifest["question_ids"]:
        raise ValueError("prediction_count_or_order_mismatch")
    oracle = {q["question_id"]: q for q in json.loads((assets / "oracle.json").read_text())}
    if file_hash(assets / "oracle.json") != manifest["oracle_sha256"]:
        raise ValueError("oracle_snapshot_changed")
    paths = {id_: database_path(root, id_) for id_ in manifest["databases"]}
    if any(file_hash(path) != manifest["databases"][id_] for id_, path in paths.items()):
        raise ValueError("database_snapshot_changed")
    scores = []
    for record in records:
        q = oracle[record["question_id"]]
        if record["db_id"] != q["db_id"]:
            raise ValueError("prediction_database_mismatch")
        path = paths[q["db_id"]]
        score = (score_one(record["sql"], q["SQL"], path, assets, timeout)
                 if record["status"] == "completed" and record.get("sql")
                 else {"res": 0, "error": record.get("error") or "no_valid_final_query"})
        scores.append(dict(score, question_id=q["question_id"], db_id=q["db_id"]))
    if any(file_hash(path) != manifest["databases"][id_] for id_, path in paths.items()):
        raise ValueError("database_changed_during_scoring")
    report = {"metric": "official_EX_full_SQL", "scorer_revision": SCORER_REVISION,
              "sqlite_version": sqlite3.sqlite_version, "total": len(scores),
              "correct": sum(s["res"] for s in scores), "scores": scores,
              "scope": manifest["scope"], "preview_completeness_proven": False}
    report["EX"] = report["correct"] / report["total"] if report["total"] else None
    write_json(run_dir / "score.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--assets", type=Path, default=Path(__file__).resolve().parent / "data" / "bird-mini-dev")
    parser.add_argument("--databases", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    result = score_run(args.run, args.assets, args.databases, args.timeout)
    print(json.dumps({k: v for k, v in result.items() if k != "scores"}, indent=2))
