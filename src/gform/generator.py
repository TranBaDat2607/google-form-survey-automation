"""Build the deterministic set of responses from a config.

Without personas, each question is sampled independently: columns are generated
separately and zipped into rows, so questions are uncorrelated by construction.
With personas, respondents are split into archetype blocks and each block is
generated from that persona's distributions, so one respondent's answers are
correlated across questions. Either way determinism holds: the same config +
seed always yields the same responses.
"""

from __future__ import annotations

import random
from typing import Dict, List, Union

from .config import Config, Persona, QuestionConfig
from .distributions import build_quota_column, largest_remainder, weighted_choices
from .models import MULTI_CHOICE, TEXT_TYPES, Response

# Per-persona seed stride: a large prime so a persona block's per-column seeds
# (block_seed + question_index) never collide with the base scheme or with each
# other, while the within-column seed math (in the helpers below) is reused
# unchanged so existing single-persona/no-persona seeds keep reproducing.
_PERSONA_STRIDE = 100_003


def _normalize_pool(qc: QuestionConfig) -> QuestionConfig:
    """Normalize a text-question answer pool so weights sum to 1.

    This keeps unnormalized pools (e.g. {a: 2, b: 2}) bit-identical to their
    normalized form, and lets text questions reuse the single-choice sampling
    path unchanged.
    """
    total = sum(qc.distribution.values())
    return qc.model_copy(
        update={"distribution": {k: v / total for k, v in qc.distribution.items()}}
    )


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


def _generate_block(
    questions: List[QuestionConfig], n: int, seed: int, mode: str
) -> List[Response]:
    """Generate ``n`` rows over the given questions (one independent column each).

    This is the original column-wise engine, factored out so it can run once
    (no personas) or once per persona block.
    """
    columns: Dict[str, List[Union[str, List[str]]]] = {}
    for idx, qc in enumerate(questions):
        if qc.type in MULTI_CHOICE:
            columns[qc.entry_id] = _checkbox_column(qc, n, seed, idx, mode)
        else:
            # Text pools sample exactly like a single-choice question over the
            # pool texts; the per-column seed scheme (seed + idx) is shared so
            # existing seeds keep reproducing.
            if qc.type in TEXT_TYPES:
                qc = _normalize_pool(qc)
            columns[qc.entry_id] = _single_column(qc, n, seed, idx, mode)

    return [
        Response(answers={eid: columns[eid][i] for eid in columns}) for i in range(n)
    ]


def _effective_distribution(persona: Persona, qc: QuestionConfig) -> Dict[str, float]:
    """The persona's override for this question, or the base distribution."""
    return persona.distributions.get(qc.entry_id, qc.distribution)


def allocate_personas(personas: List[Persona], n: int) -> Dict[str, int]:
    """Apportion ``n`` respondents across personas by weight (sums to exactly n)."""
    return largest_remainder({p.name: p.weight for p in personas}, n)


def generate(config: Config) -> List[Response]:
    n = config.generation.count
    seed = config.generation.seed
    mode = config.generation.mode

    if not config.personas:
        return _generate_block(config.questions, n, seed, mode)

    # Persona mode: split respondents into archetype blocks and generate each
    # block from that persona's effective distributions. Each block reuses the
    # column engine with a strided per-block seed so blocks stay independent and
    # deterministic.
    counts = allocate_personas(config.personas, n)
    rows: List[Response] = []
    for pi, persona in enumerate(config.personas):
        k = counts[persona.name]
        if k == 0:
            continue
        block_seed = seed + (pi + 1) * _PERSONA_STRIDE
        eff_questions = [
            qc.model_copy(update={"distribution": _effective_distribution(persona, qc)})
            for qc in config.questions
        ]
        rows.extend(_generate_block(eff_questions, k, block_seed, mode))

    # Interleave the blocks so persona order doesn't cluster (keeps preview
    # samples representative); deterministic via a seeded shuffle.
    random.Random(seed).shuffle(rows)
    return rows
