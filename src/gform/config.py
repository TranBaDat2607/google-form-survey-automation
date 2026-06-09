"""Config schema, loading, validation, and scaffolding."""

from __future__ import annotations

from typing import Dict, List, Tuple

import yaml
from pydantic import BaseModel, Field

from .models import MULTI_CHOICE, OTHER_OPTION, FormSchema, QuestionType

# Tolerance for "distribution sums to 1.0" on single-choice questions.
_SUM_TOLERANCE = 1e-3


class FormRef(BaseModel):
    url: str


class GenerationConfig(BaseModel):
    count: int = 100
    seed: int = 42
    mode: str = "exact"  # "exact" (quota) | "probabilistic"


class SubmissionConfig(BaseModel):
    enabled: bool = False
    i_own_this_form: bool = False
    rate_limit_per_min: float = 20.0
    jitter_seconds: Tuple[float, float] = (1.0, 3.0)
    stop_on_error: bool = True


class QuestionConfig(BaseModel):
    entry_id: str
    title: str = ""
    type: QuestionType
    distribution: Dict[str, float]
    # Free text submitted when the "Other" option (key __other__) is chosen.
    other_text: str = ""


class Config(BaseModel):
    form: FormRef
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    submission: SubmissionConfig = Field(default_factory=SubmissionConfig)
    questions: List[QuestionConfig]


class ConfigError(ValueError):
    """Raised when a config is internally inconsistent or doesn't match a form."""


def load_config(path) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError("Config file must be a YAML mapping.")
    return Config(**data)


def validate_internal(config: Config) -> None:
    """Validate the config on its own (no network)."""
    errors: List[str] = []

    if config.generation.count <= 0:
        errors.append("generation.count must be > 0")
    if config.generation.mode not in ("exact", "probabilistic"):
        errors.append("generation.mode must be 'exact' or 'probabilistic'")

    for qc in config.questions:
        tag = f"[{qc.entry_id}] '{qc.title}'"
        if not qc.distribution:
            errors.append(f"{tag}: distribution is empty")
            continue
        for label, p in qc.distribution.items():
            if p < 0:
                errors.append(f"{tag} option '{label}': probability cannot be negative")
        if qc.type in MULTI_CHOICE:
            for label, p in qc.distribution.items():
                if p > 1:
                    errors.append(
                        f"{tag} checkbox option '{label}': inclusion probability "
                        f"must be <= 1 (got {p})"
                    )
        else:
            total = sum(qc.distribution.values())
            if abs(total - 1.0) > _SUM_TOLERANCE:
                errors.append(
                    f"{tag}: distribution sums to {total:.4f}, must sum to 1.0"
                )

        # Google rejects an "Other" selection with no free text, so require
        # other_text whenever __other__ can actually be chosen.
        if qc.distribution.get(OTHER_OPTION, 0) > 0 and not qc.other_text.strip():
            errors.append(
                f"{tag}: distribution includes the '{OTHER_OPTION}' (Other) option "
                f"but other_text is empty; set the free text to submit for it"
            )

    if errors:
        raise ConfigError("Config validation failed:\n  - " + "\n  - ".join(errors))


def validate_against_schema(config: Config, schema: FormSchema) -> None:
    """Validate the config against the live form structure."""
    errors: List[str] = []
    qmap = {q.entry_id: q for q in schema.questions}
    cfg_ids = {qc.entry_id for qc in config.questions}

    for qc in config.questions:
        q = qmap.get(qc.entry_id)
        if q is None:
            errors.append(
                f"entry_id {qc.entry_id} ('{qc.title}') is not a supported "
                f"question on the live form"
            )
            continue
        valid = set(q.options)
        for label in qc.distribution:
            if label not in valid:
                errors.append(
                    f"[{qc.entry_id}] option '{label}' does not match any choice "
                    f"on the live form. Valid options: {sorted(valid)}"
                )

    for q in schema.questions:
        if q.required and q.entry_id not in cfg_ids:
            errors.append(
                f"required question '{q.title}' ({q.entry_id}) has no distribution "
                f"in the config"
            )

    if errors:
        raise ConfigError(
            "Config does not match the live form:\n  - " + "\n  - ".join(errors)
        )


def scaffold_dict(schema: FormSchema, count: int, seed: int) -> dict:
    """Build a config dict from a parsed form with uniform distributions."""
    questions = []
    for q in schema.questions:
        opts = q.options
        if not opts:
            continue
        if q.type in MULTI_CHOICE:
            distribution = {opt: 0.5 for opt in opts}
        else:
            p = round(1.0 / len(opts), 6)
            distribution = {opt: p for opt in opts}
            # Absorb rounding drift into the last option so the sum is exactly 1.
            distribution[opts[-1]] = round(1.0 - p * (len(opts) - 1), 6)
        question = {
            "entry_id": q.entry_id,
            "title": q.title,
            "type": q.type.value,
            "distribution": distribution,
        }
        # If the form offers an "Other" fill-in, seed an editable default text.
        if OTHER_OPTION in opts:
            question["other_text"] = "Other"
        questions.append(question)

    return {
        "form": {"url": schema.url},
        "generation": {"count": count, "seed": seed, "mode": "exact"},
        "submission": {
            "enabled": False,
            "i_own_this_form": False,
            "rate_limit_per_min": 20,
            "jitter_seconds": [1, 3],
            "stop_on_error": True,
        },
        "questions": questions,
    }
