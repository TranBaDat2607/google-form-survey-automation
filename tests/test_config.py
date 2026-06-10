import pytest

from gform.config import (
    Config,
    ConfigError,
    scaffold_dict,
    validate_against_schema,
    validate_internal,
)
from gform.models import FormSchema, OTHER_OPTION, Question, QuestionType


def _cfg_with_other(other_text=""):
    return Config(
        form={"url": "https://x/viewform"},
        generation={"count": 5, "seed": 1},
        questions=[
            {
                "entry_id": "111111",
                "title": "Role",
                "type": "radio",
                "distribution": {"Student": 0.5, OTHER_OPTION: 0.5},
                "other_text": other_text,
            }
        ],
    )


def test_other_without_text_is_rejected():
    with pytest.raises(ConfigError, match="other_text is empty"):
        validate_internal(_cfg_with_other(other_text=""))


def test_other_with_text_passes():
    validate_internal(_cfg_with_other(other_text="Researcher"))  # no raise


def _cfg_with_text(pool, q_type="text"):
    return Config(
        form={"url": "https://x/viewform"},
        generation={"count": 5, "seed": 1},
        questions=[
            {
                "entry_id": "555555",
                "title": "Name",
                "type": q_type,
                "distribution": pool,
            }
        ],
    )


def test_text_pool_empty_is_rejected():
    with pytest.raises(ConfigError, match="pool of sample answers"):
        validate_internal(_cfg_with_text({}))


def test_text_pool_exempt_from_sum_to_one():
    # Weights are normalized at generation time; {2, 3} is a valid pool.
    validate_internal(_cfg_with_text({"Alice": 2.0, "Bob": 3.0}))  # no raise


def test_text_pool_nonpositive_weight_rejected():
    with pytest.raises(ConfigError, match="weight must be > 0"):
        validate_internal(_cfg_with_text({"Alice": 1.0, "Bob": 0.0}))


def _text_schema():
    return FormSchema(
        title="T", form_id="ID", url="https://x/viewform",
        questions=[
            Question(entry_id="555555", title="Name", type=QuestionType.text,
                     options=[], required=True),
            Question(entry_id="111111", title="Color", type=QuestionType.radio,
                     options=["Red", "Blue"]),
        ],
    )


def test_schema_validation_skips_label_check_for_text():
    cfg = _cfg_with_text({"Alice": 1.0, "Bob": 1.0})
    validate_against_schema(cfg, _text_schema())  # no raise


def test_schema_validation_catches_type_mismatch():
    # Config claims radio, live question is text.
    cfg = _cfg_with_text({"Alice": 1.0}, q_type="radio")
    with pytest.raises(ConfigError, match="does not match the live form question type"):
        validate_against_schema(cfg, _text_schema())

    # Config claims text, live question is a radio.
    cfg = Config(
        form={"url": "https://x/viewform"},
        questions=[
            {"entry_id": "111111", "title": "Color", "type": "text",
             "distribution": {"whatever": 1.0}},
            {"entry_id": "555555", "title": "Name", "type": "text",
             "distribution": {"Alice": 1.0}},
        ],
    )
    with pytest.raises(ConfigError, match="does not match the live form question type"):
        validate_against_schema(cfg, _text_schema())


def test_scaffold_emits_text_question_with_empty_pool():
    data = scaffold_dict(_text_schema(), count=10, seed=1)
    text_q = next(q for q in data["questions"] if q["entry_id"] == "555555")
    assert text_q["type"] == "text"
    assert text_q["distribution"] == {}


def test_scaffold_includes_other_option_and_default_text():
    schema = FormSchema(
        title="T", form_id="ID", url="https://x/viewform",
        questions=[
            Question(entry_id="111111", title="Role", type=QuestionType.radio,
                     options=["Student", "Teacher", OTHER_OPTION]),
        ],
    )
    data = scaffold_dict(schema, count=10, seed=1)
    q = data["questions"][0]
    assert OTHER_OPTION in q["distribution"]
    assert q["other_text"] == "Other"
    # Scaffolded config is internally valid out of the box.
    validate_internal(Config(**data))
