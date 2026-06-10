"""Thin wrappers over the official Google Forms API (forms.googleapis.com).

Every function takes the discovery ``service`` object as its first argument so
tests can inject a fake and assert the exact request bodies. These wrappers
return plain JSON-friendly dicts (simplified projections, not raw API
resources).

Note: the official API can author and read forms but cannot SUBMIT responses;
submission stays on the public /formResponse pipeline (importer/submitter),
whose entry.<id> field IDs are unrelated to the questionIds used here.
"""

from __future__ import annotations

from typing import List, Optional

from googleapiclient.errors import HttpError

# Our tool-facing names -> the API's ChoiceQuestion.type enum.
CHOICE_TYPE_MAP = {
    "radio": "RADIO",
    "checkbox": "CHECKBOX",
    "dropdown": "DROP_DOWN",
}
_API_KIND_MAP = {v: k for k, v in CHOICE_TYPE_MAP.items()}


def set_publish_settings(
    service, form_id: str, published: bool = True, accepting_responses: bool = True
) -> dict:
    """Publish/unpublish a form and toggle whether it accepts responses."""
    body = {
        "publishSettings": {
            "publishState": {
                "isPublished": published,
                "isAcceptingResponses": accepting_responses,
            }
        }
    }
    result = (
        service.forms().setPublishSettings(formId=form_id, body=body).execute()
    )
    state = (result.get("publishSettings") or {}).get("publishState") or {}
    return {
        "form_id": form_id,
        "published": state.get("isPublished", published),
        "accepting_responses": state.get("isAcceptingResponses", accepting_responses),
    }


def create_form(
    service,
    title: str,
    description: str = "",
    document_title: str = "",
    publish: bool = True,
) -> dict:
    """Create a form and (by default) publish it so it can accept responses.

    forms.create only accepts info.title/info.documentTitle; the description
    is applied with a follow-up batchUpdate. Forms created via the API start
    unpublished (API change effective June 30, 2026), so publishing is an
    explicit step here.
    """
    info = {"title": title}
    if document_title:
        info["documentTitle"] = document_title
    form = service.forms().create(body={"info": info}).execute()
    form_id = form["formId"]

    if description:
        service.forms().batchUpdate(
            formId=form_id,
            body={
                "requests": [
                    {
                        "updateFormInfo": {
                            "info": {"description": description},
                            "updateMask": "description",
                        }
                    }
                ]
            },
        ).execute()

    published_state: dict = {"published": False, "accepting_responses": False}
    if publish:
        try:
            state = set_publish_settings(service, form_id, True, True)
            published_state = {
                "published": state["published"],
                "accepting_responses": state["accepting_responses"],
            }
        except HttpError:
            # Legacy forms don't support publish settings; they accept
            # responses by default, so creation still succeeded.
            published_state = {
                "published": "legacy_default",
                "accepting_responses": "legacy_default",
            }

    return {
        "form_id": form_id,
        "responder_uri": form.get("responderUri", ""),
        "edit_url": f"https://docs.google.com/forms/d/{form_id}/edit",
        **published_state,
    }


def _create_item(service, form_id: str, item: dict, index: Optional[int]) -> dict:
    """createItem at ``index`` (append at the end when index is None)."""
    if index is None:
        form = service.forms().get(formId=form_id).execute()
        index = len(form.get("items") or [])
    reply = (
        service.forms()
        .batchUpdate(
            formId=form_id,
            body={
                "requests": [
                    {"createItem": {"item": item, "location": {"index": index}}}
                ]
            },
        )
        .execute()
    )
    created = ((reply.get("replies") or [{}])[0].get("createItem")) or {}
    question_ids = created.get("questionId") or []
    return {
        "item_id": created.get("itemId"),
        "question_id": question_ids[0] if question_ids else None,
        "index": index,
    }


def add_text_question(
    service,
    form_id: str,
    title: str,
    required: bool = False,
    paragraph: bool = False,
    index: Optional[int] = None,
) -> dict:
    """Add a short-answer (or paragraph) text question."""
    item = {
        "title": title,
        "questionItem": {
            "question": {
                "required": required,
                "textQuestion": {"paragraph": paragraph},
            }
        },
    }
    return _create_item(service, form_id, item, index)


def add_choice_question(
    service,
    form_id: str,
    title: str,
    options: List[str],
    choice_type: str = "radio",
    required: bool = False,
    include_other: bool = False,
    index: Optional[int] = None,
) -> dict:
    """Add a radio / checkbox / dropdown question."""
    api_type = CHOICE_TYPE_MAP.get(choice_type)
    if api_type is None:
        raise ValueError(
            f"choice_type must be one of {sorted(CHOICE_TYPE_MAP)} (got '{choice_type}')"
        )
    cleaned = [o.strip() for o in options]
    if not cleaned or any(not o for o in cleaned):
        raise ValueError("options must be a non-empty list of non-empty labels")
    if len(set(cleaned)) != len(cleaned):
        raise ValueError("options must be unique")
    if include_other and choice_type == "dropdown":
        raise ValueError("Google Forms does not support an 'Other' option on dropdowns")

    option_payload: List[dict] = [{"value": o} for o in cleaned]
    if include_other:
        option_payload.append({"isOther": True})

    item = {
        "title": title,
        "questionItem": {
            "question": {
                "required": required,
                "choiceQuestion": {"type": api_type, "options": option_payload},
            }
        },
    }
    return _create_item(service, form_id, item, index)


def _project_item(it: dict) -> dict:
    entry: dict = {"item_id": it.get("itemId"), "title": it.get("title", "")}
    question = (it.get("questionItem") or {}).get("question")
    if not question:
        entry["kind"] = "layout"  # section header, image, video, page break...
        return entry

    entry["question_id"] = question.get("questionId")
    entry["required"] = bool(question.get("required"))
    if "textQuestion" in question:
        paragraph = bool((question["textQuestion"] or {}).get("paragraph"))
        entry["kind"] = "paragraph" if paragraph else "text"
    elif "choiceQuestion" in question:
        cq = question["choiceQuestion"]
        entry["kind"] = _API_KIND_MAP.get(cq.get("type"), str(cq.get("type")).lower())
        raw_options = cq.get("options") or []
        entry["options"] = [o["value"] for o in raw_options if not o.get("isOther")]
        entry["has_other"] = any(o.get("isOther") for o in raw_options)
    elif "scaleQuestion" in question:
        entry["kind"] = "linear_scale"
    elif "ratingQuestion" in question:
        entry["kind"] = "rating"
    else:
        # Date/time/file-upload/etc: report the raw kind rather than hiding it.
        kinds = [k for k in question if k.endswith("Question")]
        entry["kind"] = kinds[0] if kinds else "unknown"
    return entry


def get_form(service, form_id: str) -> dict:
    """A simplified projection of forms.get, including the responderUri."""
    form = service.forms().get(formId=form_id).execute()
    info = form.get("info") or {}
    publish_state = ((form.get("publishSettings") or {}).get("publishState")) or None
    return {
        "form_id": form.get("formId", form_id),
        "title": info.get("title", ""),
        "description": info.get("description", ""),
        "document_title": info.get("documentTitle", ""),
        "responder_uri": form.get("responderUri", ""),
        "publish_state": publish_state,
        "items": [_project_item(it) for it in form.get("items") or []],
    }


def list_responses(
    service,
    form_id: str,
    page_size: int = 100,
    page_token: str = "",
    since_timestamp: str = "",
) -> dict:
    """List submitted responses, flattening answers to question_id -> [values]."""
    kwargs = {"formId": form_id, "pageSize": page_size}
    if page_token:
        kwargs["pageToken"] = page_token
    if since_timestamp:
        # The API's only supported filter.
        kwargs["filter"] = f"timestamp > {since_timestamp}"
    data = service.forms().responses().list(**kwargs).execute()

    flattened = []
    for r in data.get("responses") or []:
        answers = {}
        for qid, answer in (r.get("answers") or {}).items():
            text_answers = (answer.get("textAnswers") or {}).get("answers") or []
            answers[qid] = [a.get("value", "") for a in text_answers]
        flattened.append(
            {
                "response_id": r.get("responseId"),
                "create_time": r.get("createTime", ""),
                "last_submitted_time": r.get("lastSubmittedTime", ""),
                "respondent_email": r.get("respondentEmail", ""),
                "answers": answers,
            }
        )
    return {"responses": flattened, "next_page_token": data.get("nextPageToken", "")}
