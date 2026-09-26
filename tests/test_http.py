"""真实本机 TCP 测试；不调用供应商、不接触产品数据库。"""

import json
import socket
import threading
import time
from http.client import HTTPConnection

import pytest
import uvicorn

import main
import session_runner
import session_store
from run_context import CURRENT_RUN


@pytest.fixture
def server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(main.app, log_level="error", lifespan="on")
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, kwargs={"sockets": [sock]})
    thread.start()
    deadline = time.monotonic() + 5
    while not instance.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert instance.started
    yield sock.getsockname()[1]
    instance.should_exit = True
    thread.join(5)
    sock.close()
    assert not thread.is_alive()


def test_chat_and_sse_share_terminal_contract(monkeypatch, server):
    def stream(*args):
        yield {"type": "text", "content": "答复"}
        yield {"type": "done", "status": "completed"}

    monkeypatch.setattr(session_runner, "run_react_stream", stream)
    conn = HTTPConnection("127.0.0.1", server, timeout=5)
    conn.request(
        "POST",
        "/chat",
        json.dumps({"message": "问题", "session_id": "plain"}),
        {"Content-Type": "application/json"},
    )
    result = json.loads(conn.getresponse().read())
    assert result["status"] == "completed" and result["answer"] == "答复"
    conn.request(
        "POST",
        "/chat/stream",
        json.dumps({"message": "问题", "session_id": "sse"}),
        {"Content-Type": "application/json"},
    )
    events = [
        json.loads(line[6:])
        for line in conn.getresponse().read().decode().splitlines()
        if line.startswith("data: ")
    ]
    conn.close()
    assert events[-1]["status"] == "completed"
    with session_store.connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM turns WHERE status='completed'"
            ).fetchone()[0]
            == 2
        )


def test_actual_socket_disconnect_cancels_worker_and_retains_partial(
    monkeypatch, server
):
    closed = threading.Event()

    def stream(*args):
        try:
            yield {"type": "text_chunk", "content": "部分回答"}
            run = CURRENT_RUN.get()
            assert run is not None
            assert run.cancel.wait(5), "HTTP disconnect was not propagated"
            run.check()
        finally:
            closed.set()

    monkeypatch.setattr(session_runner, "run_react_stream", stream)
    conn = HTTPConnection("127.0.0.1", server, timeout=5)
    conn.request(
        "POST",
        "/chat/stream",
        json.dumps({"message": "问题", "session_id": "disconnect"}),
        {"Content-Type": "application/json"},
    )
    response = conn.getresponse()
    while b"text_chunk" not in response.readline():
        pass
    response.close()
    conn.close()
    assert closed.wait(5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with session_store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM turns WHERE session_id='disconnect'"
            ).fetchone()
        if row["status"] != "running":
            break
        time.sleep(0.01)
    assert row["status"] == "cancelled" and row["answer"] == "部分回答"
    assert session_runner._locks["disconnect"].acquire(timeout=1)
    session_runner._locks["disconnect"].release()
