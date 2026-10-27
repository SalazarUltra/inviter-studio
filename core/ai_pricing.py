"""Public catalog rates for estimating AI request cost in USD.

Prefer a provider-returned billed amount when available (e.g. xAI
``cost_in_usd_ticks``). Otherwise estimate from tokens × documented rates.
Rates are a snapshot for UI estimates — exact charges live in the provider console.
"""

from __future__ import annotations

from typing import Any

# xAI: 1 USD = 10^10 ticks (docs.x.ai Cost Tracking).
USD_TICKS_PER_DOLLAR = 10_000_000_000
RATES_AS_OF = "2026-10-27"

# (input_usd_per_1M, output_usd_per_1M) for short-context standard API rates.
# Aliases map retired / alternate IDs onto the billed model family.
_MODEL_RATES: dict[str, dict[str, tuple[float, float]]] = {
    "xai": {
        "grok-4.7": (2.00, 6.00),
        "grok-4.6": (2.00, 6.00),
        "grok-4.5": (2.00, 6.00),
        "grok-4.3": (1.25, 2.50),
        # Public docs (Oct 2026): these aliases currently resolve to grok-4.3 rates.
        "grok-4": (1.25, 2.50),
        "grok-4-latest": (1.25, 2.50),
        "grok-4-fast-reasoning": (1.25, 2.50),
        "grok-4-fast": (1.25, 2.50),
        "grok-4-fast-non-reasoning": (1.25, 2.50),
        "grok-4-1-fast-reasoning": (1.25, 2.50),
        "grok-4-1-fast": (1.25, 2.50),
        "grok-4-1-fast-non-reasoning": (1.25, 2.50),
    },
    "openai": {
        "gpt-6-luna": (0.10, 0.50),
        "gpt-6.1-sol": (2.00, 10.00),
        "gpt-6-astra": (10.00, 50.00),
        "gpt-6-sol": (2.00, 10.00),
    },
    "anthropic": {
        # Approximate public API rates for current Claude 5.5 IDs; confirm in console.
        "claude-haiku-5-5": (1.00, 5.00),
        "claude-sonnet-5-5": (3.00, 15.00),
    },
    "gemini": {
        # Approximate Google AI Studio / Gemini API flash rates.
        "gemini-3.5-flash-lite": (0.10, 0.40),
        "gemini-3.8-flash": (0.30, 2.50),
    },
}

# When only a total token count is stored, assume this input share for estimates.
# Reasoning models typically spend most tokens on output/reasoning, not the prompt.
_DEFAULT_INPUT_FRACTION = 0.35
_REASONING_INPUT_FRACTION = 0.20


def ticks_to_usd(ticks: int | float | None) -> float | None:
    if ticks is None:
        return None
    try:
        value = float(ticks)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value / USD_TICKS_PER_DOLLAR


def normalize_rate_model(provider: str, model: str) -> str:
    value = (model or "").strip()
    if provider == "gemini":
        value = value.removeprefix("models/")
    return value


def lookup_rates(provider: str, model: str) -> tuple[float, float] | None:
    """Return (input_per_1M, output_per_1M) or None when unknown."""
    provider_rates = _MODEL_RATES.get((provider or "").strip().lower())
    if not provider_rates:
        return None
    key = normalize_rate_model(provider, model)
    if key in provider_rates:
        return provider_rates[key]
    # Prefix match for dated snapshots like grok-4.7-2026-xx-xx
    for known, rates in provider_rates.items():
        if key.startswith(known) or known.startswith(key):
            return rates
    return None


def estimate_cost_usd(
    input_tokens: int,
    output_tokens: int,
    *,
    input_per_million: float,
    output_per_million: float,
    cached_input_tokens: int = 0,
    cached_input_per_million: float | None = None,
) -> float:
    inp = max(0, int(input_tokens))
    out = max(0, int(output_tokens))
    cached = min(max(0, int(cached_input_tokens)), inp)
    uncached = inp - cached
    cached_rate = float(cached_input_per_million) if cached_input_per_million is not None else float(input_per_million)
    return (uncached * float(input_per_million) + cached * cached_rate + out * float(output_per_million)) / 1_000_000


def _input_fraction(provider: str, model: str) -> float:
    provider = (provider or "").strip().lower()
    model = normalize_rate_model(provider, model).lower()
    if provider == "xai" or "reason" in model or model.startswith("grok"):
        return _REASONING_INPUT_FRACTION
    if provider == "openai" and model in {"gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol"}:
        return _REASONING_INPUT_FRACTION
    return _DEFAULT_INPUT_FRACTION


def estimate_from_total_tokens(provider: str, model: str, total_tokens: int) -> float | None:
    rates = lookup_rates(provider, model)
    if not rates or total_tokens <= 0:
        return None
    fraction = _input_fraction(provider, model)
    input_tokens = int(round(total_tokens * fraction))
    output_tokens = max(0, int(total_tokens) - input_tokens)
    return estimate_cost_usd(input_tokens, output_tokens, input_per_million=rates[0], output_per_million=rates[1])


def resolve_request_cost(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    usage: dict[str, Any] | None = None,
    input_cost_per_million: float | None = None,
    output_cost_per_million: float | None = None,
) -> tuple[float | None, str | None]:
    """Return (usd, source) where source is ``provider``, ``config``, or ``estimate``."""
    usage = usage or {}
    billed = ticks_to_usd(usage.get("cost_in_usd_ticks"))
    if billed is not None:
        return billed, "provider"

    if input_cost_per_million is not None and output_cost_per_million is not None:
        return (
            estimate_cost_usd(
                input_tokens,
                output_tokens,
                input_per_million=float(input_cost_per_million),
                output_per_million=float(output_cost_per_million),
            ),
            "config",
        )

    rates = lookup_rates(provider, model)
    if rates:
        cached = 0
        details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
        try:
            cached = int(details.get("cached_tokens") or 0)
        except (TypeError, ValueError):
            cached = 0
        # xAI cached input is typically ~25% of input rate when documented; use input rate
        # if we lack a per-model cached figure to avoid underestimating.
        return (
            estimate_cost_usd(
                input_tokens,
                output_tokens,
                input_per_million=rates[0],
                output_per_million=rates[1],
                cached_input_tokens=cached,
            ),
            "estimate",
        )
    return None, None


def resolve_stored_cost(config: dict[str, Any]) -> tuple[float | None, str | None]:
    """Cost for a saved connection card: prefer accumulated USD, else estimate from tokens."""
    stored = float(config.get("total_cost_usd") or 0.0)
    tokens = int(config.get("total_tokens") or 0)
    if stored > 0:
        return stored, "stored"
    if tokens <= 0:
        return 0.0, "stored"
    estimated = estimate_from_total_tokens(
        str(config.get("provider") or ""),
        str(config.get("model") or ""),
        tokens,
    )
    if estimated is None:
        return None, None
    return estimated, "estimate"


def format_usd(amount: float | None, *, estimated: bool = False, unknown: str = "") -> str:
    if amount is None:
        return unknown
    prefix = "~$" if estimated else "$"
    if amount > 0 and amount < 0.0001:
        return f"{prefix}{amount:.6f}"
    return f"{prefix}{amount:.4f}"


def reasoning_tokens_from_usage(usage: dict[str, Any] | None) -> int:
    usage = usage or {}
    details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    try:
        return max(0, int(details.get("reasoning_tokens") or 0))
    except (TypeError, ValueError):
        return 0
