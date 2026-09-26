"""所有测试使用临时会话库，禁止真实模型网络调用。"""

import pytest

import llm
import session_store


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(session_store, "DB_PATH", str(tmp_path / "sessions.db"))
    session_store.init_db()

    def forbidden_client():
        raise AssertionError("tests must not contact a real model")

    monkeypatch.setattr(llm, "_get_client", forbidden_client)
    return tmp_path
