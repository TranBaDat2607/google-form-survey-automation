"""Offline tests for the official-API wrappers.

A hand-rolled FakeService records every (method, kwargs) call and returns
canned responses, so the tests assert the EXACT request bodies the Forms API
expects without any googleapiclient HTTP machinery.
"""

import pytest

from gform.forms_api import (
    add_choice_question,
    add_text_question,
    create_form,
    get_form,
    list_responses,
    set_publish_settings,
)


class _Call:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeResponses:
    def __init__(self, recorder, canned):
        self._recorder = recorder
        self._canned = canned

    def list(self, **kwargs):
        self._recorder.append(("responses.list", kwargs))
        return _Call(self._canned.get("responses.list", {}))


class _FakeForms:
    def __init__(self, recorder, canned):
        self._recorder = recorder
        self._canned = canned

    def create(self, **kwargs):
        self._recorder.append(("create", kwargs))
        return _Call(self._canned.get("create", {}))

    def get(self, **kwargs):
        self._recorder.append(("get", kwargs))
        return _Call(self._canned.get("get", {}))

    def batchUpdate(self, **kwargs):
        self._recorder.append(("batchUpdate", kwargs))
        return _Call(self._canned.get("batchUpdate", {}))

    def setPublishSettings(self, **kwargs):
        self._recorder.append(("setPublishSettings", kwargs))
        return _Call(self._canned.get("setPublishSettings", {}))

    def responses(self):
        return _FakeResponses(self._recorder, self._canned)


class FakeService:
    def __init__(self, canned=None):
        self.calls = []
        self._canned = canned or {}

    def forms(self):
        return _FakeForms(self.calls, self._canned)

    def call(self, name):
        return [kwargs for method, kwargs in self.calls if method == name]


def test_create_form_minimal_publishes():
    svc = FakeService(
        {
            "create": {"formId": "F1", "responderUri": "https://docs.google.com/forms/d/e/X/viewform"},
            "setPublishSettings": {
                "publishSettings": {
                    "publishState": {"isPublished": True, "isAcceptingResponses": True}
                }
            },
        }
    )
    result = create_form(svc, "My Survey")

    assert svc.call("create") == [{"body": {"info": {"title": "My Survey"}}}]
    assert svc.call("setPublishSettings") == [
        {
            "formId": "F1",
            "body": {
                "publishSettings": {
                    "publishState": {"isPublished": True, "isAcceptingResponses": True}
                }
            },
        }
    ]
    # No description -> no batchUpdate.
    assert svc.call("batchUpdate") == []
    assert result == {
        "form_id": "F1",
        "responder_uri": "https://docs.google.com/forms/d/e/X/viewform",
        "edit_url": "https://docs.google.com/forms/d/F1/edit",
        "published": True,
        "accepting_responses": True,
    }


def test_create_form_description_and_document_title():
    svc = FakeService({"create": {"formId": "F1"}})
    create_form(svc, "T", description="About this", document_title="Doc", publish=False)

    assert svc.call("create") == [
        {"body": {"info": {"title": "T", "documentTitle": "Doc"}}}
    ]
    assert svc.call("batchUpdate") == [
        {
            "formId": "F1",
            "body": {
                "requests": [
                    {
                        "updateFormInfo": {
                            "info": {"description": "About this"},
                            "updateMask": "description",
                        }
                    }
                ]
            },
        }
    ]
    assert svc.call("setPublishSettings") == []


def test_set_publish_settings_close_form():
    svc = FakeService(
        {
            "setPublishSettings": {
                "publishSettings": {
                    "publishState": {"isPublished": True, "isAcceptingResponses": False}
                }
            }
        }
    )
    result = set_publish_settings(svc, "F1", published=True, accepting_responses=False)
    assert result == {"form_id": "F1", "published": True, "accepting_responses": False}


def test_add_text_question_appends_at_end():
    svc = FakeService(
        {
            "get": {"items": [{"itemId": "a"}, {"itemId": "b"}]},
            "batchUpdate": {
                "replies": [{"createItem": {"itemId": "i3", "questionId": ["q3"]}}]
            },
        }
    )
    result = add_text_question(svc, "F1", "Your feedback", required=True, paragraph=True)

    assert svc.call("batchUpdate") == [
        {
            "formId": "F1",
            "body": {
                "requests": [
                    {
                        "createItem": {
                            "item": {
                                "title": "Your feedback",
                                "questionItem": {
                                    "question": {
                                        "required": True,
                                        "textQuestion": {"paragraph": True},
                                    }
                                },
                            },
                            "location": {"index": 2},
                        }
                    }
                ]
            },
        }
    ]
    assert result == {"item_id": "i3", "question_id": "q3", "index": 2}


def test_add_text_question_explicit_index_skips_get():
    svc = FakeService(
        {"batchUpdate": {"replies": [{"createItem": {"itemId": "i", "questionId": ["q"]}}]}}
    )
    result = add_text_question(svc, "F1", "Name", index=0)
    assert svc.call("get") == []
    assert result["index"] == 0


def test_add_choice_question_radio_with_other():
    svc = FakeService(
        {
            "get": {},  # empty form -> index 0
            "batchUpdate": {
                "replies": [{"createItem": {"itemId": "i1", "questionId": ["q1"]}}]
            },
        }
    )
    add_choice_question(
        svc, "F1", "Role", ["Student", "Teacher"], choice_type="radio", include_other=True
    )
    body = svc.call("batchUpdate")[0]["body"]
    question = body["requests"][0]["createItem"]["item"]["questionItem"]["question"]
    assert question["choiceQuestion"] == {
        "type": "RADIO",
        "options": [{"value": "Student"}, {"value": "Teacher"}, {"isOther": True}],
    }


def test_add_choice_question_type_mapping_and_validation():
    svc = FakeService(
        {"batchUpdate": {"replies": [{"createItem": {"itemId": "i", "questionId": ["q"]}}]}}
    )
    add_choice_question(svc, "F1", "Pick", ["A", "B"], choice_type="dropdown", index=0)
    question = svc.call("batchUpdate")[0]["body"]["requests"][0]["createItem"]["item"][
        "questionItem"
    ]["question"]
    assert question["choiceQuestion"]["type"] == "DROP_DOWN"

    with pytest.raises(ValueError, match="choice_type"):
        add_choice_question(svc, "F1", "Q", ["A"], choice_type="slider")
    with pytest.raises(ValueError, match="non-empty"):
        add_choice_question(svc, "F1", "Q", [])
    with pytest.raises(ValueError, match="non-empty"):
        add_choice_question(svc, "F1", "Q", ["A", "  "])
    with pytest.raises(ValueError, match="unique"):
        add_choice_question(svc, "F1", "Q", ["A", "A"])
    with pytest.raises(ValueError, match="dropdown"):
        add_choice_question(svc, "F1", "Q", ["A"], choice_type="dropdown", include_other=True)


def test_get_form_projection():
    svc = FakeService(
        {
            "get": {
                "formId": "F1",
                "info": {"title": "T", "description": "D", "documentTitle": "Doc"},
                "responderUri": "https://docs.google.com/forms/d/e/X/viewform",
                "publishSettings": {
                    "publishState": {"isPublished": True, "isAcceptingResponses": True}
                },
                "items": [
                    {
                        "itemId": "i1",
                        "title": "Name",
                        "questionItem": {
                            "question": {"questionId": "q1", "textQuestion": {}}
                        },
                    },
                    {
                        "itemId": "i2",
                        "title": "Role",
                        "questionItem": {
                            "question": {
                                "questionId": "q2",
                                "required": True,
                                "choiceQuestion": {
                                    "type": "RADIO",
                                    "options": [{"value": "A"}, {"isOther": True}],
                                },
                            }
                        },
                    },
                    {"itemId": "i3", "title": "Section"},
                ],
            }
        }
    )
    result = get_form(svc, "F1")
    assert result["title"] == "T"
    assert result["responder_uri"].endswith("/viewform")
    assert result["publish_state"] == {"isPublished": True, "isAcceptingResponses": True}

    name, role, section = result["items"]
    assert name == {
        "item_id": "i1", "title": "Name", "question_id": "q1",
        "required": False, "kind": "text",
    }
    assert role["kind"] == "radio"
    assert role["options"] == ["A"]
    assert role["has_other"] is True
    assert section == {"item_id": "i3", "title": "Section", "kind": "layout"}


def test_list_responses_flattens_answers():
    svc = FakeService(
        {
            "responses.list": {
                "responses": [
                    {
                        "responseId": "r1",
                        "createTime": "2026-06-10T00:00:00Z",
                        "lastSubmittedTime": "2026-06-10T00:00:01Z",
                        "answers": {
                            "q1": {
                                "questionId": "q1",
                                "textAnswers": {"answers": [{"value": "Red"}]},
                            },
                            "q2": {
                                "questionId": "q2",
                                "textAnswers": {
                                    "answers": [{"value": "A"}, {"value": "B"}]
                                },
                            },
                        },
                    }
                ],
                "nextPageToken": "tok",
            }
        }
    )
    result = list_responses(svc, "F1", page_size=50, since_timestamp="2026-06-01T00:00:00Z")

    assert svc.call("responses.list") == [
        {"formId": "F1", "pageSize": 50, "filter": "timestamp > 2026-06-01T00:00:00Z"}
    ]
    assert result["next_page_token"] == "tok"
    r = result["responses"][0]
    assert r["response_id"] == "r1"
    assert r["answers"] == {"q1": ["Red"], "q2": ["A", "B"]}
