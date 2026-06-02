"""
Cost of a unit of processing, in USD.

Single source of truth for what an extraction costs, so the fund ledger charges
real money. Two cost sources today:
  * Anthropic tokens — priced per model, per million tokens (MTok).
  * Mistral OCR pages — priced per page.

Prices are list prices as of 2026-06; update here when they change (one place).
Model lookup is prefix-based so a minor version bump (claude-haiku-4-5 →
claude-haiku-4-6) keeps pricing without a code change, and an unknown model is
charged at a conservative high tier (Opus) + logged, so cost is never silently
undercounted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Per-MTok list prices: (input, output). Cache read = 0.1x input, cache write
# (5-min) = 1.25x input — the standard Anthropic multipliers.
_MTOK = 1_000_000
_MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4":   (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4":  (1.0, 5.0),
    # legacy / fallbacks
    "claude-3-5-haiku": (0.80, 4.0),
    "claude-3-5-sonnet": (3.0, 15.0),
}
_CACHE_READ_MULT = 0.10
_CACHE_WRITE_MULT = 1.25
# Charged when a model name matches nothing — conservative (highest tier) so we
# over-reserve rather than overspend. _unknown_models collects names seen.
_FALLBACK_PRICE = (15.0, 75.0)

# Mistral OCR: ~$1 per 1000 pages.
MISTRAL_OCR_PER_PAGE = 0.001

_unknown_models: set[str] = set()


def _model_price(model: str) -> tuple[float, float]:
    for prefix, price in _MODEL_PRICES.items():
        if model.startswith(prefix):
            return price
    if model not in _unknown_models:
        _unknown_models.add(model)
        print(f"   ⚠ pricing: unknown model '{model}' — charging Opus-tier fallback "
              f"(${_FALLBACK_PRICE[0]}/${_FALLBACK_PRICE[1]} per MTok). Add it to pricing._MODEL_PRICES.")
    return _FALLBACK_PRICE


@dataclass
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, *, input_tokens: int = 0, output_tokens: int = 0,
            cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read_tokens
        self.cache_write_tokens += cache_write_tokens


@dataclass
class Usage:
    """Accumulates the token/page usage of one unit of work (e.g. extracting one
    meeting), across however many model calls and OCR pages it took."""
    by_model: dict[str, ModelUsage] = field(default_factory=dict)
    mistral_pages: int = 0

    def record_anthropic(self, model: str, usage) -> None:
        """Add one Anthropic response's `.usage` (the SDK usage object or a dict
        with input_tokens / output_tokens / cache_* fields)."""
        get = (usage.get if isinstance(usage, dict) else lambda k, d=0: getattr(usage, k, d) or 0)
        mu = self.by_model.setdefault(model, ModelUsage())
        mu.add(
            input_tokens=get("input_tokens", 0),
            output_tokens=get("output_tokens", 0),
            cache_read_tokens=get("cache_read_input_tokens", 0),
            cache_write_tokens=get("cache_creation_input_tokens", 0),
        )

    def record_mistral_pages(self, pages: int) -> None:
        self.mistral_pages += max(0, pages)

    def merge(self, other: "Usage") -> None:
        for model, mu in other.by_model.items():
            tgt = self.by_model.setdefault(model, ModelUsage())
            tgt.add(input_tokens=mu.input_tokens, output_tokens=mu.output_tokens,
                    cache_read_tokens=mu.cache_read_tokens, cache_write_tokens=mu.cache_write_tokens)
        self.mistral_pages += other.mistral_pages


def cost_usd(usage: Usage) -> float:
    """Total USD cost of accumulated usage."""
    total = usage.mistral_pages * MISTRAL_OCR_PER_PAGE
    for model, mu in usage.by_model.items():
        in_price, out_price = _model_price(model)
        total += (mu.input_tokens / _MTOK) * in_price
        total += (mu.output_tokens / _MTOK) * out_price
        total += (mu.cache_read_tokens / _MTOK) * in_price * _CACHE_READ_MULT
        total += (mu.cache_write_tokens / _MTOK) * in_price * _CACHE_WRITE_MULT
    return round(total, 6)


def cost_breakdown(usage: Usage) -> dict:
    """Structured cost detail for the ledger's meta column (audit trail)."""
    models = {}
    for model, mu in usage.by_model.items():
        sub = Usage(by_model={model: mu})
        models[model] = {
            "input_tokens": mu.input_tokens,
            "output_tokens": mu.output_tokens,
            "cache_read_tokens": mu.cache_read_tokens,
            "cache_write_tokens": mu.cache_write_tokens,
            "cost_usd": cost_usd(sub),
        }
    return {
        "models": models,
        "mistral_pages": usage.mistral_pages,
        "mistral_cost_usd": round(usage.mistral_pages * MISTRAL_OCR_PER_PAGE, 6),
        "total_usd": cost_usd(usage),
    }
