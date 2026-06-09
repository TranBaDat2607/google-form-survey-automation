"""Shared data models for parsed forms and generated responses."""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional, Union

from pydantic import BaseModel, Field


# Config-facing label for a multiple-choice "Other" (fill-in-the-blank) option,
# and the magic value Google expects in the POST body when it is selected. When
# "Other" is chosen the submit also needs a sibling
# `entry.<id>.other_option_response=<text>` field carrying the free text.
OTHER_OPTION = "__other__"
OTHER_SUBMIT_VALUE = "__other_option__"


class QuestionType(str, Enum):
    """The choice-based question types we support.

    Grid questions (multiple-choice grid / checkbox grid) are decomposed at
    import time into one question PER ROW: a grid row behaves exactly like a
    radio (grid_radio) or a checkbox (grid_checkbox) over its column options.
    """

    radio = "radio"
    dropdown = "dropdown"
    checkbox = "checkbox"
    linear_scale = "linear_scale"
    rating = "rating"                # star/icon rating; single choice over 1..N
    grid_radio = "grid_radio"        # one row of a multiple-choice grid
    grid_checkbox = "grid_checkbox"  # one row of a checkbox grid


# Google Forms internal type codes (from FB_PUBLIC_LOAD_DATA_) -> our types.
# Type 7 (grids) is handled specially in the importer (it expands into rows and
# the radio-vs-checkbox flag lives per-row), so it is not in this map. A rating
# question (code 18) is structurally a radio over its rating values (1..N), so
# it maps straight onto our single-choice handling. Codes not covered (0/1 text,
# 9 date, 10 time, 13 file, 6/8/11 layout) are intentionally skipped on import.
TYPE_CODE_MAP: Dict[int, QuestionType] = {
    2: QuestionType.radio,
    3: QuestionType.dropdown,
    4: QuestionType.checkbox,
    5: QuestionType.linear_scale,
    18: QuestionType.rating,
}

# Types that take exactly one answer.
SINGLE_CHOICE = {
    QuestionType.radio,
    QuestionType.dropdown,
    QuestionType.linear_scale,
    QuestionType.rating,
    QuestionType.grid_radio,
}

# Types that take a set of answers (independent per-option inclusion).
MULTI_CHOICE = {QuestionType.checkbox, QuestionType.grid_checkbox}


class Question(BaseModel):
    """A single multiple-choice question parsed from a live form."""

    entry_id: str
    title: str
    type: QuestionType
    options: List[str]
    required: bool = False


class FormSchema(BaseModel):
    """The parsed structure of a Google Form."""

    title: str
    form_id: str
    url: str
    fbzx: Optional[str] = None
    questions: List[Question]
    warnings: List[str] = Field(default_factory=list)


class Response(BaseModel):
    """One generated response: entry_id -> a single value or a list (checkbox)."""

    answers: Dict[str, Union[str, List[str]]]
