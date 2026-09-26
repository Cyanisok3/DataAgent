import json
import threading
import time
from types import SimpleNamespace

import pytest

import llm
import session_runner as runner
import session_store as store
from context import build_view, visible_history
from event_logger import usage_stats
from run_context import CURRENT_RUN, RunCancelled, RunContext


def terminal(sid):
    with store.connection() as conn:
        return dict(
            conn.execute(
                "SELECT * FROM turns WHERE session_id=? ORDER BY turn DESC", (sid,)
            ).fetchone()
        )


def test_events_persisted_before_publication(monkeypatch):
    def stream(*args):
        yield {"type": "text_chunk", "content": "完成"}
        yield {"type": "done", "status": "completed"}

    monkeypatch.setattr(runner, "run_react_stream", stream)
    seen = []

    def publish(event):
        with store.connection() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM events WHERE type=?", (event["type"],)
            ).fetchone()[0]
        seen.append(event)

    runner.run_session("a", "问题", publish, threading.Event())
    assert terminal("a")["status"] == "completed"
    assert seen[-1]["type"] == "done"


@pytest.mark.parametrize("failure", ["decision", "final", "logging"])
def test_failure_has_unique_terminal_and_no_partial_history(monkeypatch, failure):
    def stream(*args):
        if failure == "decision":
            raise RuntimeError("decision")
        yield {"type": "text_chunk", "content": "部分"}
        raise RuntimeError("final")

    monkeypatch.setattr(runner, "run_react_stream", stream)
    if failure == "logging":
        monkeypatch.setattr(
            runner, "save_event", lambda *a: (_ for _ in ()).throw(OSError("disk"))
        )
    events = []
    runner.run_session("a", "问题", events.append, threading.Event())
    assert terminal("a")["status"] == "failed"
    assert visible_history(store.load_messages("a")) == []
    with store.connection() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM events WHERE type='done'").fetchone()[0]
            == 1
        )


def test_cancel_saves_partial_and_releases_lock(monkeypatch):
    cancel = threading.Event()

    def stream(*args):
        yield {"type": "text_chunk", "content": "部分"}
        cancel.set()
        yield {"type": "text_chunk", "content": "不要发布"}

    monkeypatch.setattr(runner, "run_react_stream", stream)
    events = []
    runner.run_session("a", "问题", events.append, cancel)
    row = terminal("a")
    assert row["status"] == "cancelled" and row["answer"] == "部分"
    assert len(events) == 1
    assert runner._locks["a"].acquire(blocking=False)
    runner._locks["a"].release()


def test_same_session_serial_different_sessions_parallel(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    order = []

    def stream(question, *args):
        order.append(question)
        if question == "first":
            entered.set()
            assert release.wait(3)
        yield {"type": "done", "status": "completed"}

    monkeypatch.setattr(runner, "run_react_stream", stream)

    def start(sid, question):
        t = threading.Thread(
            target=runner.run_session,
            args=(sid, question, lambda e: None, threading.Event()),
        )
        t.start()
        return t

    first = start("a", "first")
    assert entered.wait(3)
    second = start("a", "second")
    other = start("b", "other")
    other.join(3)
    assert not other.is_alive() and order == ["first", "other"]
    release.set()
    first.join(3)
    second.join(3)
    assert order == ["first", "other", "second"]


def test_actual_requests_stream_usage_and_partial_response_logged(monkeypatch):
    turn = store.begin_turn("a", "问题")
    token = CURRENT_RUN.set(RunContext(session_id="a", turn=turn))
    view = build_view("系统", [], [{"role": "user", "content": "长内容" * 600}])

    def complete(*args):
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="部分"), finish_reason=None
                )
            ]
        )
        raise RuntimeError("secret-provider-error-must-not-be-logged")

    monkeypatch.setattr(llm, "_completion", complete)
    try:
        with pytest.raises(RuntimeError):
            list(llm._call(view, "final", stream=True))
    finally:
        CURRENT_RUN.reset(token)
    with store.connection() as conn:
        row = dict(conn.execute("SELECT * FROM model_calls").fetchone())
    assert json.loads(row["request"]) == view.messages
    assert row["response"] == "部分" and row["status"] == "failed"
    assert row["error"] == "RuntimeError"
    assert "secret-provider" not in str(row)
    usage = usage_stats("a")
    assert usage["serialized_bytes"] == view.serialized_bytes
    assert usage["last_llm_call"]["usage"] is None
    assert usage["missing_usage_phases"] == ["final"]


def test_usage_only_chunk_and_final_finish_reason(monkeypatch):
    usage = SimpleNamespace(
        model_dump=lambda: {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    )

    def complete(*args):
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="答复"), finish_reason="stop"
                )
            ]
        )
        yield SimpleNamespace(choices=[], usage=usage)

    monkeypatch.setattr(llm, "_completion", complete)
    turn = store.begin_turn("a", "q")
    token = CURRENT_RUN.set(RunContext(session_id="a", turn=turn))
    try:
        assert list(llm._call(build_view("s", [], []), "final", True)) == ["答复"]
    finally:
        CURRENT_RUN.reset(token)
    assert usage_stats("a")["round_usage"]["total_tokens"] == 12


def test_deadline_and_model_budget_are_bounded():
    with pytest.raises(TimeoutError):
        RunContext(deadline=time.monotonic() - 1).check()
    run = RunContext(max_calls=1)
    run.take_call()
    with pytest.raises(RuntimeError, match="budget"):
        run.take_call()
    run.cancel.set()
    with pytest.raises(RunCancelled):
        run.check()
