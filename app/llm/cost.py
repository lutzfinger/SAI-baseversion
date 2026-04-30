"""Cost computation from the YAML cost table.

The cost table maps `provider_id` → `model` → `{input, cached_input?, output}` in
USD per 1M tokens. Loaded once on first access; refresh by deleting the cached
instance or restarting the process.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.llm.provider import TokenUsage

DEFAULT_COST_TABLE_PATH = Path(__file__).resolve().parent / "cost_table.yaml"

# Token unit: prices are quoted per 1,000,000 tokens.
_TOKENS_PER_PRICE_UNIT = 1_000_000


class CostTable:
    """Loaded view of `cost_table.yaml`."""

    def __init__(self, *, providers: dict[str, dict[str, dict[str, float]]]) -> None:
        self._providers = providers

    def cost_for(
        self, *, provider_id: str, model: str, usage: TokenUsage
    ) -> float:
        """Compute USD cost for one call. Returns 0.0 if provider/model unknown."""

        provider = self._providers.get(provider_id)
        if not provider:
            return 0.0
        rates = provider.get(model) or provider.get("*")
        if not rates:
            return 0.0

        input_rate = float(rates.get("input", 0.0))
        cached_input_rate = float(rates.get("cached_input", input_rate))
        output_rate = float(rates.get("output", 0.0))

        non_cached_input = max(0, usage.input_tokens - usage.cached_input_tokens)

        cost = (
            non_cached_input * input_rate
            + usage.cached_input_tokens * cached_input_rate
            + usage.output_tokens * output_rate
        ) / _TOKENS_PER_PRICE_UNIT
        return round(cost, 8)

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> CostTable:
        """Load a cost table from YAML. Defaults to the bundled cost_table.yaml."""

        path = path or DEFAULT_COST_TABLE_PATH
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        providers = raw.get("providers", {})
        return cls(providers=providers)


@lru_cache(maxsize=1)
def get_default_cost_table() -> CostTable:
    """Return the bundled cost table. Cached for the life of the process."""

    return CostTable.from_yaml()
