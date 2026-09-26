"""单进程评测预算：发送前保守预留，异常或缺 usage 不退还预留。"""
from dataclasses import dataclass
from decimal import Decimal


class EvaluationBudgetExceeded(RuntimeError):
    pass


@dataclass
class EvaluationBudget:
    max_tokens: int | None
    max_cost: Decimal | None
    input_price: Decimal | None
    output_price: Decimal | None
    tokens_reserved: int = 0
    cost_reserved: Decimal = Decimal(0)
    calls_reserved: int = 0

    def __post_init__(self):
        values = (self.max_cost, self.input_price, self.output_price)
        if ((self.max_tokens is not None and self.max_tokens <= 0)
                or any(n is not None and (not n.is_finite() or n <= 0) for n in values)
                or (self.input_price is None) != (self.output_price is None)
                or (self.max_cost is not None and self.input_price is None)):
            raise ValueError("positive_finite_budget_and_prices_required")

    def reserve(self, input_tokens: int, output_tokens: int):
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("negative_reservation")
        cost = ((self.input_price * input_tokens + self.output_price * output_tokens) / 1_000_000
                if self.input_price is not None and self.output_price is not None else Decimal(0))
        if ((self.max_tokens is not None and self.tokens_reserved + input_tokens + output_tokens > self.max_tokens)
                or (self.max_cost is not None and self.cost_reserved + cost > self.max_cost)):
            raise EvaluationBudgetExceeded("evaluation_budget_exhausted")
        self.tokens_reserved += input_tokens + output_tokens
        self.cost_reserved += cost
        self.calls_reserved += 1

    def report(self):
        return {"tokens_reserved": self.tokens_reserved,
                "cost_reserved": str(self.cost_reserved) if self.input_price is not None else None,
                "calls_reserved": self.calls_reserved, "max_tokens": self.max_tokens,
                "max_cost": str(self.max_cost) if self.max_cost is not None else None,
                "accounting": "estimated input tokens + 2048-token margin + max output tokens; reservation, not invoice"}
