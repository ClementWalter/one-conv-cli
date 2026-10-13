"""Pure metrics that rank runs; kept free of ML dependencies so they test fast."""

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence


def preference_accuracy(margins: Sequence[float]) -> float:
    """Share of held-out pairs where the model scores your accepted answer above the rejected one."""
    if not margins:
        raise ValueError("No preference pairs to score")
    return sum(1 for margin in margins if margin > 0) / len(margins)


def perplexity(mean_nll: float) -> float:
    return math.exp(mean_nll)


def violation_rate(flags: Sequence[bool]) -> float:
    if not flags:
        raise ValueError("No generations to score")
    return sum(flags) / len(flags)


def drift_table(rows: Iterable[dict]) -> dict[str, dict[int, float]]:
    """Violation rate per variant and context length, the curve a text prefix is expected to lose."""
    grouped: dict[str, dict[int, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        grouped[row["variant"]][row["context_tokens"]].append(row["violated"])
    return {variant: {length: violation_rate(flags) for length, flags in sorted(by_length.items())}
            for variant, by_length in grouped.items()}


def rank_runs(runs: list[dict], metric: str, higher_is_better: bool = True) -> list[dict]:
    """Order runs by one metric; runs that lack it sort last."""
    scored = [run for run in runs if isinstance(run.get("metrics", {}).get(metric), (int, float))]
    missing = [run for run in runs if run not in scored]
    scored.sort(key=lambda run: run["metrics"][metric], reverse=higher_is_better)
    return scored + missing
