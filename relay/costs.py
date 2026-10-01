from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

# SQLite stores money as integer billionths of a currency unit. Never use
# floating point for billing arithmetic or add amounts of different currencies.
NANOS_PER_UNIT = 1_000_000_000
RATE_NAMES = (
    "input",
    "output",
    "cache_read",
    "cache_write",
    "cache_write_5m",
    "cache_write_1h",
)


@dataclass(frozen=True)
class Pricing:
    currency: str = "USD"
    input: Decimal | None = None
    output: Decimal | None = None
    cache_read: Decimal | None = None
    # Generic rate is used only when upstream does not report cache write TTLs.
    cache_write: Decimal | None = None
    cache_write_5m: Decimal | None = None
    cache_write_1h: Decimal | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("COST_CURRENCY must contain three uppercase ASCII letters")
        for name in RATE_NAMES:
            raw = getattr(self, name)
            if raw is None or raw == "":
                object.__setattr__(self, name, None)
                continue
            try:
                value = Decimal(str(raw))
            except InvalidOperation:
                raise ValueError(f"COST_{name.upper()}_PER_MILLION must be numeric") from None
            if not value.is_finite() or value < 0 or value > 1_000_000:
                raise ValueError(f"COST_{name.upper()}_PER_MILLION must be between 0 and 1000000")
            if value.as_tuple().exponent < -9:
                raise ValueError(
                    f"COST_{name.upper()}_PER_MILLION supports at most 9 decimal places"
                )
            object.__setattr__(self, name, value)

    @classmethod
    def from_env(cls, env: dict) -> Pricing:
        return cls(
            currency=env.get("AM2OAIR_RELAY_COST_CURRENCY") or "USD",
            **{
                name: env.get(f"AM2OAIR_RELAY_COST_{name.upper()}_PER_MILLION") or None
                for name in RATE_NAMES
            },
        )

    def public(self) -> dict:
        return {
            "currency": self.currency,
            "unit": "per_million_tokens",
            "configured": self.input is not None and self.output is not None,
            "rates": {
                name: format(getattr(self, name), "f") if getattr(self, name) is not None else None
                for name in RATE_NAMES
            },
        }


@dataclass(frozen=True)
class CostRecord:
    uncached_input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_write_5m_tokens: int | None = None
    cache_write_1h_tokens: int | None = None
    cost_currency: str | None = None
    input_cost_nanos: int | None = None
    output_cost_nanos: int | None = None
    cache_read_cost_nanos: int | None = None
    cache_write_cost_nanos: int | None = None
    total_cost_nanos: int | None = None
    cost_status: str = "historical"
    pricing_snapshot: str | None = None


def estimate_cost(pricing: Pricing, usage: dict | None, complete: bool = True) -> CostRecord:
    """Estimate only from numeric upstream usage and a validated price snapshot.

    Unknown rates or usage remain NULL, not a misleading zero charge. Partial
    stream usage is kept separately and explicitly marked as incomplete.
    """
    metadata = {
        "cost_currency": pricing.currency,
        "pricing_snapshot": json.dumps(pricing.public(), separators=(",", ":")),
    }
    if (
        not isinstance(usage, dict)
        or "input_tokens" not in usage
        or (complete and "output_tokens" not in usage)
    ):
        return CostRecord(**metadata, cost_status="missing_usage")

    def count(name: str, source: dict = usage) -> int:
        value = source.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Invalid usage")
        return value

    try:
        inputs, outputs = count("input_tokens"), count("output_tokens")
        read, written = count("cache_read_input_tokens"), count("cache_creation_input_tokens")
        split = usage.get("cache_creation")
        five = hour = None
        if split is not None:
            if not isinstance(split, dict):
                raise ValueError("Invalid cache usage")
            five = count("ephemeral_5m_input_tokens", split)
            hour = count("ephemeral_1h_input_tokens", split)
            if five + hour != written:
                raise ValueError("Inconsistent cache usage")
    except ValueError:
        return CostRecord(**metadata, cost_status="invalid_usage")

    metadata.update(
        uncached_input_tokens=inputs,
        cache_read_tokens=read,
        cache_write_tokens=written,
        cache_write_5m_tokens=five,
        cache_write_1h_tokens=hour,
    )
    categories = [(inputs, pricing.input), (outputs, pricing.output), (read, pricing.cache_read)]
    writes = (
        [(five, pricing.cache_write_5m), (hour, pricing.cache_write_1h)]
        if five is not None
        else [(written, pricing.cache_write)]
    )
    if any(tokens and rate is None for tokens, rate in categories + writes):
        return CostRecord(**metadata, cost_status="unconfigured")

    with localcontext() as context:
        context.prec = 50

        def cost(parts: list[tuple[int, Decimal | None]]) -> int:
            # Per-million token rates * token counts * 1000 = nanocurrency units.
            amount = sum(Decimal(tokens) * (rate or Decimal(0)) * 1000 for tokens, rate in parts)
            return int(amount.to_integral_value(rounding=ROUND_HALF_UP))

        components = [cost([category]) for category in categories] + [cost(writes)]
    total = sum(components)
    if total > 2**63 - 1:
        return CostRecord(**metadata, cost_status="cost_overflow")
    return CostRecord(
        **metadata,
        input_cost_nanos=components[0],
        output_cost_nanos=components[1],
        cache_read_cost_nanos=components[2],
        cache_write_cost_nanos=components[3],
        total_cost_nanos=total,
        cost_status="calculated" if complete else "partial",
    )


def money(nanos: int | None) -> str | None:
    if nanos is None:
        return None
    return f"{nanos // NANOS_PER_UNIT}.{nanos % NANOS_PER_UNIT:09d}"


def public_cost(row: dict, *, aggregate: bool = False) -> dict:
    result = {"currency": row["cost_currency"]}
    for name in ("input", "output", "cache_read", "cache_write", "total"):
        result[name + "_cost"] = money(row[name + "_cost_nanos"])
    if aggregate:
        result.update(
            priced_requests=row["priced_requests"], partial_requests=row["partial_requests"]
        )
    else:
        result["status"] = row["cost_status"]
    return result
