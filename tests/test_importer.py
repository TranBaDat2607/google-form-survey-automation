from pathlib import Path

import pytest

from gform.importer import FormAccessError, parse_form
from gform.models import QuestionType

FIXTURE = Path(__file__).parent / "fixtures" / "sample_form.html"
URL = "https://docs.google.com/forms/d/e/1FAIpQLScTESTFORMID/viewform"


def _schema():
    return parse_form(FIXTURE.read_text(encoding="utf-8"), url=URL)


def test_form_metadata():
    schema = _schema()
    assert schema.title == "My Test Form"
    assert schema.form_id == "1FAIpQLScTESTFORMID"
    assert schema.fbzx == "-9876543210987654321"


def test_supported_questions_parsed():
    types = {q.entry_id: q.type for q in _schema().questions}
    expected = {
        "111111": QuestionType.radio,
        "222222": QuestionType.dropdown,
        "333333": QuestionType.checkbox,
        "444444": QuestionType.linear_scale,
        "888888": QuestionType.rating,
    }
    for entry_id, q_type in expected.items():
        assert types[entry_id] == q_type


def test_rating_parsed_as_single_choice_over_values():
    # A rating question (Google type code 18) is structurally a radio over its
    # 1..N rating values; it must import as our `rating` type with those options.
    rating = next(q for q in _schema().questions if q.entry_id == "888888")
    assert rating.type == QuestionType.rating
    assert rating.options == ["1", "2", "3", "4", "5"]


def test_options_and_required():
    radio = next(q for q in _schema().questions if q.entry_id == "111111")
    assert radio.options == ["Red", "Blue", "Green"]
    assert radio.required is True

    scale = next(q for q in _schema().questions if q.entry_id == "444444")
    assert scale.options == ["1", "2", "3", "4", "5"]


def test_unsupported_skipped_with_warning():
    schema = _schema()
    ids = {q.entry_id for q in schema.questions}
    assert "555555" not in ids  # short-answer text question skipped
    assert any("Name" in w for w in schema.warnings)


def test_multiple_choice_grid_expanded_to_rows():
    qmap = {q.entry_id: q for q in _schema().questions}
    service = qmap["611111"]
    assert service.type == QuestionType.grid_radio
    assert service.title == "Satisfaction grid [Service]"
    assert service.options == ["Bad", "Good"]
    assert service.required is True
    assert qmap["611112"].title == "Satisfaction grid [Food]"


def test_checkbox_grid_expanded_to_rows():
    qmap = {q.entry_id: q for q in _schema().questions}
    week1 = qmap["711111"]
    assert week1.type == QuestionType.grid_checkbox
    assert week1.title == "Availability [Week 1]"
    assert week1.options == ["Mon", "Tue"]
    assert week1.required is False
    assert qmap["711112"].type == QuestionType.grid_checkbox


def test_signin_redirect_refused():
    with pytest.raises(FormAccessError):
        parse_form("<html></html>", url=URL, final_url="https://accounts.google.com/signin")


def test_missing_blob_refused():
    with pytest.raises(FormAccessError):
        parse_form("<html>no data here</html>", url=URL)
