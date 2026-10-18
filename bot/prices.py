from pathlib import Path

from bot.models import canonical_model

BasePrices = dict[tuple[str, int], int]


def parse_prices(text: str) -> BasePrices:
    out: BasePrices = {}
    for line in text.splitlines():
        parts = line.split("#")[0].split()
        if len(parts) < 3 or (len(parts) > 3 and parts[-1].lower() == "esim"):
            continue
        tail = parts[-1].lower() in ("sim", "esim")
        nums = parts[:-1] if tail else parts
        model = canonical_model(" ".join(nums[:-2]))
        if model and nums[-2].isdigit() and nums[-1].isdigit():
            out[(model, int(nums[-2]))] = int(nums[-1])
    return out


def load_prices(path: Path) -> BasePrices:
    return parse_prices(path.read_text(encoding="utf-8")) if path.exists() else {}


def base_price(prices: BasePrices, model: str, storage_gb: int) -> int | None:
    if storage_gb:
        return prices.get((model, storage_gb))
    known = sorted(s for m, s in prices if m == model)
    return prices[(model, known[0])] if known else None
