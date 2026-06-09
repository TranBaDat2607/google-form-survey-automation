"""Deterministic conversion of proportions into concrete answer columns."""

from __future__ import annotations

import random
from typing import Dict, List


def largest_remainder(proportions: Dict[str, float], n: int) -> Dict[str, int]:
    """Apportion ``n`` items across options by the largest-remainder method.

    Returns integer counts that sum to exactly ``n`` and match the target
    proportions as closely as integer rounding allows. Ties in the fractional
    remainder are broken by option label so the result is deterministic.
    """
    total = sum(proportions.values())
    if total <= 0:
        raise ValueError("distribution proportions must sum to a positive value")

    exact = {k: (v / total) * n for k, v in proportions.items()}
    floors = {k: int(v // 1) for k, v in exact.items()}
    remainder = n - sum(floors.values())

    # Hand out the leftover units to the largest fractional parts first.
    order = sorted(proportions, key=lambda k: (-(exact[k] - floors[k]), k))
    for i in range(remainder):
        floors[order[i % len(order)]] += 1
    return floors


def build_quota_column(counts: Dict[str, int], seed) -> List[str]:
    """Expand integer counts into a flat list and deterministically shuffle it."""
    column: List[str] = []
    for value, count in counts.items():
        column.extend([value] * count)
    random.Random(seed).shuffle(column)
    return column


def weighted_choices(proportions: Dict[str, float], n: int, rng: random.Random) -> List[str]:
    """Draw ``n`` independent samples from the distribution (probabilistic mode)."""
    keys = list(proportions.keys())
    weights = list(proportions.values())
    return rng.choices(keys, weights=weights, k=n)
