"""Config schema, validation, and scaffolding.

Configs are plain dicts/JSON (built inline by MCP tool calls); there is no
file-based config format anymore.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from pydantic import BaseModel, Field

from .models import MULTI_CHOICE, OTHER_OPTION, TEXT_TYPES, FormSchema, QuestionType

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
    # For choice questions: option label -> probability. For text/paragraph
    # questions this doubles as the answer POOL: sample text -> weight
    # (weights are normalized at generation time, no sum-to-1 constraint).
    distribution: Dict[str, float]
    # Free text submitted when the "Other" option (key __other__) is chosen.
    other_text: str = ""


class Persona(BaseModel):
    """A respondent archetype: a share of the population that answers coherently.

    ``weight`` is this persona's share of respondents (weights across personas
    sum to 1.0). ``distributions`` maps an entry_id to that persona's own
    distribution for the question (same shape as QuestionConfig.distribution:
    option->probability for choice questions, sample-text->weight for text). A
    persona only needs to list the questions it answers differently; any entry_id
    it omits falls back to the base ``questions[].distribution``. Because all of
    one respondent's answers are drawn from a single persona, their rating,
    choices, and free text move together — the within-respondent correlation that
    independent-per-question sampling cannot produce.
    """

    name: str
    weight: float
    distributions: Dict[str, Dict[str, float]] = Field(default_factory=dict)


class Config(BaseModel):
    form: FormRef
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    submission: SubmissionConfig = Field(default_factory=SubmissionConfig)
    questions: List[QuestionConfig]
    # Optional respondent archetypes. Empty (default) => every question is sampled
    # independently (the original behavior). When present, each respondent is
    # assigned one persona and answers every question from that persona's
    # distributions (falling back to the base question distribution per entry).
    personas: List[Persona] = Field(default_factory=list)


class ConfigError(ValueError):
    """Raised when a config is internally inconsistent or doesn't match a form."""


def _validate_distribution(
    tag: str, qtype: QuestionType, distribution: Dict[str, float], other_text: str
) -> List[str]:
    """Validate one distribution (a base question's or a persona's override).

    Shared by base questions and persona overrides so both obey identical rules.
    """
    errors: List[str] = []
    if not distribution:
        if qtype in TEXT_TYPES:
            errors.append(
                f"{tag}: text questions need a pool of sample answers "
                f"(distribution maps answer text -> weight)"
            )
        else:
            errors.append(f"{tag}: distribution is empty")
        return errors
    if qtype in TEXT_TYPES:
        # Pool weights are normalized at generation time, so there is no
        # sum-to-1 constraint — only that every weight is usable.
        for label, p in distribution.items():
            if p <= 0:
                errors.append(
                    f"{tag} pool answer '{label}': weight must be > 0 (got {p})"
                )
        return errors
    for label, p in distribution.items():
        if p < 0:
            errors.append(f"{tag} option '{label}': probability cannot be negative")
    if qtype in MULTI_CHOICE:
        for label, p in distribution.items():
            if p > 1:
                errors.append(
                    f"{tag} checkbox option '{label}': inclusion probability "
                    f"must be <= 1 (got {p})"
                )
    else:
        total = sum(distribution.values())
        if abs(total - 1.0) > _SUM_TOLERANCE:
            errors.append(f"{tag}: distribution sums to {total:.4f}, must sum to 1.0")

    # Google rejects an "Other" selection with no free text, so require
    # other_text whenever __other__ can actually be chosen.
    if distribution.get(OTHER_OPTION, 0) > 0 and not other_text.strip():
        errors.append(
            f"{tag}: distribution includes the '{OTHER_OPTION}' (Other) option "
            f"but other_text is empty; set the free text to submit for it"
        )
    return errors


def _validate_personas(config: Config) -> List[str]:
    """Validate persona definitions and their per-question overrides."""
    errors: List[str] = []
    qmap = {qc.entry_id: qc for qc in config.questions}
    seen: set = set()
    total_w = 0.0
    for p in config.personas:
        ptag = f"persona '{p.name}'"
        if not p.name.strip():
            errors.append("a persona has an empty name")
        elif p.name in seen:
            errors.append(f"duplicate persona name '{p.name}'")
        seen.add(p.name)
        if p.weight <= 0:
            errors.append(f"{ptag}: weight must be > 0 (got {p.weight})")
        total_w += p.weight
        for eid, dist in p.distributions.items():
            base = qmap.get(eid)
            if base is None:
                errors.append(
                    f"{ptag}: entry_id {eid} is not a configured question"
                )
                continue
            tag = f"{ptag} [{eid}] '{base.title}'"
            errors.extend(
                _validate_distribution(tag, base.type, dist, base.other_text)
            )
    if config.personas and abs(total_w - 1.0) > _SUM_TOLERANCE:
        errors.append(
            f"persona weights sum to {total_w:.4f}, must sum to 1.0"
        )
    return errors


def validate_internal(config: Config) -> None:
    """Validate the config on its own (no network)."""
    errors: List[str] = []

    if config.generation.count <= 0:
        errors.append("generation.count must be > 0")
    if config.generation.mode not in ("exact", "probabilistic"):
        errors.append("generation.mode must be 'exact' or 'probabilistic'")

    for qc in config.questions:
        tag = f"[{qc.entry_id}] '{qc.title}'"
        errors.extend(
            _validate_distribution(tag, qc.type, qc.distribution, qc.other_text)
        )

    errors.extend(_validate_personas(config))

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
        if (qc.type in TEXT_TYPES) != (q.type in TEXT_TYPES):
            errors.append(
                f"[{qc.entry_id}] config type '{qc.type.value}' does not match "
                f"the live form question type '{q.type.value}'"
            )
            continue
        if qc.type in TEXT_TYPES:
            # Free-text answers have no fixed options to match.
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

    # Persona overrides must use real option labels too (text pools are exempt).
    for persona in config.personas:
        for eid, dist in persona.distributions.items():
            q = qmap.get(eid)
            if q is None or q.type in TEXT_TYPES:
                continue
            valid = set(q.options)
            for label in dist:
                if label not in valid:
                    errors.append(
                        f"persona '{persona.name}' [{eid}] option '{label}' does "
                        f"not match any choice on the live form. Valid options: "
                        f"{sorted(valid)}"
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
        if q.type in TEXT_TYPES:
            # Empty pool on purpose: the user must supply sample answers (and
            # weights) before the config validates. Emitting the question keeps
            # required-question coverage visible.
            questions.append(
                {
                    "entry_id": q.entry_id,
                    "title": q.title,
                    "type": q.type.value,
                    "distribution": {},
                }
            )
            continue
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
