"""调用方注入的数据目录与时钟；默认仍是本项目业务库。"""
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from semantic_layer import DOMAINS, METRICS, TABLES, Domain, Metric, Table


@dataclass(frozen=True)
class DataSource:
    path: Path
    tables: list[Table]
    domains: list[Domain]
    metrics: dict[str, Metric] = field(default_factory=dict)
    clock: datetime | None = None
    data_end: str | None = None

    def now(self) -> datetime:
        return self.clock or datetime.now(ZoneInfo("Asia/Shanghai"))


DEFAULT_SOURCE = DataSource(
    path=Path(__file__).with_name("business.db"), tables=TABLES, domains=DOMAINS, metrics=METRICS)
CURRENT_SOURCE: ContextVar[DataSource] = ContextVar("dataagent_source", default=DEFAULT_SOURCE)
