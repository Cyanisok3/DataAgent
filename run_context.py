"""单轮取消、截止时间与调用预算；无存储或编排依赖。"""

import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event


class RunCancelled(Exception):
    pass


@dataclass
class RunContext:
    session_id: str = ""
    turn: int = 0
    cancel: Event = field(default_factory=Event)
    deadline: float = field(default_factory=lambda: time.monotonic() + 120)
    model_calls: int = 0
    max_calls: int = 14
    repairs: int = 0
    reserve_request: Callable[[int, int], None] | None = None

    def check(self):
        if self.cancel.is_set():
            raise RunCancelled("client_disconnected")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("turn_timeout")

    def take_call(self):
        self.check()
        if self.model_calls >= self.max_calls:
            raise RuntimeError("model_call_budget_exhausted")
        self.model_calls += 1


CURRENT_RUN: ContextVar[RunContext | None] = ContextVar("dataagent_run", default=None)
