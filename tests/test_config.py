import pytest

from gform.config import Config, ConfigError, scaffold_dict, validate_internal
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
