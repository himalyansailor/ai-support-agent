"""MetricsQuery port (settings.metrics_query): instant PromQL queries against the platform's metrics store.

A query never raises and never takes longer than the adapter's bound (3 s): a failure gives an empty
result flagged `unavailable`, so the platform admin pages still load without the metrics store.
Callers pass module constants only; nothing from a request is ever put into the PromQL.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Sample:
    labels: dict[str, str]
    value: float  # may be NaN (e.g. a quantile with no traffic)


class QueryResult(list[Sample]):
    """The samples of an instant vector; `unavailable` is set (and the list empty) when the query failed."""

    def __init__(self, samples: list[Sample] | None = None, *, unavailable: bool = False) -> None:
        super().__init__(samples or [])
        self.unavailable = unavailable


class MetricsQuery(Protocol):
    async def query(self, promql: str) -> QueryResult: ...

    async def aclose(self) -> None: ...
