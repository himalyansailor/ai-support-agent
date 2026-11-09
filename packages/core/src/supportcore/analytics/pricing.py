"""Model prices and the cost of an LLM call. Prices are entered by the platform admin (nothing is seeded);
a model without a price has no cost: `cost_micros` is None, never an estimate.

Cost is in integer micro-units of USD: (input_tokens * input_per_million + output_tokens * output_per_million)
/ 1_000_000, rounded half up, in integer arithmetic. A priced turn keeps the cost it was recorded with; a
later price change does not re-price it.

`price_for` caches per process for 60 s, keyed by (provider, model), absent prices included. The admin
routes call `invalidate` after a change, so the process that handled it sees the change at once; other
processes pick it up within 60 s. The cached value is a detached copy, safe to use after the session ends.
"""

from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import ModelPrice

__all__ = ["CACHE_SECONDS", "MAX_CACHE_ENTRIES", "clear_cache", "cost_micros", "invalidate", "price_for"]

CACHE_SECONDS = 60
MAX_CACHE_ENTRIES = 1000  # model ids can be free text (openai_compatible): the cache must not grow without bound
_MILLION = 1_000_000

_monotonic = time.monotonic  # replaced in tests
_cache: dict[tuple[str, str], tuple[float, ModelPrice | None]] = {}


def clear_cache() -> None:
    _cache.clear()


def invalidate(provider: str, model: str) -> None:
    _cache.pop((provider, model), None)


async def price_for(session: AsyncSession, provider: str, model: str) -> ModelPrice | None:
    """The platform price of (provider, model), or None when none is set."""
    key = (provider, model)
    hit = _cache.get(key)
    if hit is not None and _monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    row = (
        await session.execute(select(ModelPrice).where(ModelPrice.provider == provider, ModelPrice.model == model))
    ).scalar_one_or_none()
    price = (
        None
        if row is None
        else ModelPrice(
            id=row.id,
            provider=row.provider,
            model=row.model,
            input_per_million_micros=row.input_per_million_micros,
            output_per_million_micros=row.output_per_million_micros,
            currency=row.currency,
            updated_by=row.updated_by,
            updated_at=row.updated_at,
        )
    )
    now = _monotonic()
    for stale in [k for k, (at, _) in _cache.items() if now - at >= CACHE_SECONDS]:
        del _cache[stale]
    _cache.pop(key, None)  # first: re-inserting an existing key at the cap must not evict another entry
    while len(_cache) >= MAX_CACHE_ENTRIES:
        del _cache[next(iter(_cache))]  # dicts keep insertion order: the oldest entry
    _cache[key] = (now, price)
    return price


def cost_micros(price: ModelPrice | None, input_tokens: int | None, output_tokens: int | None) -> int | None:
    """Micro-USD for the tokens, rounded half up; None when there is no price or a token count is unknown."""
    if price is None or input_tokens is None or output_tokens is None:
        return None
    total = input_tokens * price.input_per_million_micros + output_tokens * price.output_per_million_micros
    return (total + _MILLION // 2) // _MILLION
