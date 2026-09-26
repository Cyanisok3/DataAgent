"""单进程评测预算：发送前保守预留，异常或缺 usage 不退还预留。"""
from dataclasses import dataclass
from decimal import Decimal


class EvaluationBudgetExceeded(RuntimeError):
    pass


@dataclass
class EvaluationBudget:
    max_tokens: int
    max_cost: Decimal
    input_price: Decimal
    output_price: Decimal
    tokens_reserved: int = 0
    cost_reserved: Decimal = Decimal(0)
    calls_reserved: int = 0

    def __post_init__(self):
        if self.max_tokens <= 0 or any(not n.is_finite() or n <= 0 for n in
                                      (self.max_cost, self.input_price, self.output_price)):
            raise ValueError("positive_finite_budget_and_prices_required")

    def reserve(self, input_tokens: int, output_tokens: int):
        cost = (self.input_price * input_tokens + self.output_price * output_tokens) / 1_000_000
        if self.tokens_reserved + input_tokens + output_tokens > self.max_tokens or self.cost_reserved + cost > self.max_cost:
            raise EvaluationBudgetExceeded("evaluation_budget_exhausted")
        self.tokens_reserved += input_tokens + output_tokens
        self.cost_reserved += cost
        self.calls_reserved += 1

    def report(self):
        return {"tokens_reserved": self.tokens_reserved, "cost_reserved": str(self.cost_reserved),
                "calls_reserved": self.calls_reserved, "max_tokens": self.max_tokens,
                "max_cost": str(self.max_cost),
                "accounting": "UTF8 request bytes + 2048 overhead + max output; conservative reservation, not invoice"}
