"""Build the deterministic set of responses from a config.

Each question is sampled independently. Columns are generated separately and
zipped into rows, so questions are uncorrelated by construction. Determinism:
the same config + seed always yields the same responses.
"""

from __future__ import annotations

import random
from typing import List, Union

from .config import Config, QuestionConfig
from .distributions import build_quota_column, largest_remainder, weighted_choices
from .models import MULTI_CHOICE, Response


def _single_column(qc: QuestionConfig, n: int, seed: int, idx: int, mode: str) -> List[str]:
    if mode == "exact":
        counts = largest_remainder(qc.distribution, n)
        return build_quota_column(counts, seed + idx)
    rng = random.Random(seed + idx)
    return weighted_choices(qc.distribution, n, rng)


def _checkbox_column(
    qc: QuestionConfig, n: int, seed: int, idx: int, mode: str
) -> List[List[str]]:
    # Each option is an independent inclusion decision across the n rows.
    labels = list(qc.distribution.keys())
    bool_cols = {}
    for opt_idx, label in enumerate(labels):
        p = qc.distribution[label]
        opt_seed = f"{seed}-{idx}-{opt_idx}"
        if mode == "exact":
            included = largest_remainder({"in": p, "out": max(0.0, 1.0 - p)}, n)["in"]
            flags = [True] * included + [False] * (n - included)
            random.Random(opt_seed).shuffle(flags)
        else:
            rng = random.Random(opt_seed)
            flags = [rng.random() < p for _ in range(n)]
        bool_cols[label] = flags

    return [[label for label in labels if bool_cols[label][i]] for i in range(n)]


def generate(config: Config) -> List[Response]:
    n = config.generation.count
    seed = config.generation.seed
    mode = config.generation.mode

    columns: dict[str, List[Union[str, List[str]]]] = {}
    for idx, qc in enumerate(config.questions):
        if qc.type in MULTI_CHOICE:
            columns[qc.entry_id] = _checkbox_column(qc, n, seed, idx, mode)
        else:
            columns[qc.entry_id] = _single_column(qc, n, seed, idx, mode)

    return [
        Response(answers={eid: columns[eid][i] for eid in columns}) for i in range(n)
    ]
