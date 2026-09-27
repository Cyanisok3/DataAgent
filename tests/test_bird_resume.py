import json
import sqlite3
from types import SimpleNamespace

import pytest

from benchmark.resume_bird import copy_sessions, guarded_create, retained_records


def test_retain_semantic_failure_but_retry_infrastructure(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"question_ids": [1, 2, 3, 4]}))
    rows = [{"question_id": 1, "status": "completed", "error": None},
            {"question_id": 2, "status": "failed", "error": "max_iters"},
            {"question_id": 3, "status": "failed", "error": "APIStatusError"}]
    (tmp_path / "records.jsonl").write_text("\n".join(map(json.dumps, rows)))
    assert list(retained_records(tmp_path)) == [1, 2]


def test_copy_only_retained_sessions(tmp_path):
    from benchmark.resume_bird import TABLES
    paths = [tmp_path / name for name in ("old.db", "new.db")]
    for path in paths:
        with sqlite3.connect(path) as conn:
            for table in TABLES:
                conn.execute(f"CREATE TABLE {table} (session_id TEXT,status TEXT,view TEXT,config TEXT)")
                if path == paths[0]:
                    for sid in ("bird-1", "bird-2"):
                        conn.execute(f"INSERT INTO {table} VALUES (?,?,?,?)",
                                     (sid, "completed", '{"estimated_tokens":10}', '{"max_tokens":20}'))
    reserved = []
    copy_sessions(*paths, {1: {}}, SimpleNamespace(reserve=lambda *args: reserved.append(args)))
    with sqlite3.connect(paths[1]) as conn:
        for table in TABLES:
            assert conn.execute(f"SELECT session_id FROM {table}").fetchall() == [("bird-1",)]
    assert reserved == [(2058, 20)]


def test_fatal_provider_error_stops_future_requests(tmp_path):
    class BalanceError(Exception):
        status_code = 402
    calls = []
    def create():
        calls.append(1)
        raise BalanceError("secret must not be logged")
    guarded = guarded_create(create, [], tmp_path / "errors.jsonl")
    with pytest.raises(BalanceError):
        guarded()
    with pytest.raises(RuntimeError, match="provider_unavailable"):
        guarded()
    assert len(calls) == 1
    assert "secret" not in (tmp_path / "errors.jsonl").read_text()
