import pytest
import responses as responses_lib

from gform.config import Config
from gform.generator import generate
from gform.models import Response
from gform.submitter import (
    SubmissionNotAuthorized,
    build_payload,
    response_url,
    submit_all,
)


def make_cfg(enabled=True, own=True):
    return Config(
        form={"url": "https://docs.google.com/forms/d/e/ID/viewform"},
        generation={"count": 3, "seed": 1},
        submission={
            "enabled": enabled,
            "i_own_this_form": own,
            "rate_limit_per_min": 6000,
            "jitter_seconds": [0, 0],
        },
        questions=[
            {"entry_id": "111111", "title": "C", "type": "radio", "distribution": {"Red": 1.0}},
            {"entry_id": "333333", "title": "F", "type": "checkbox", "distribution": {"A": 1.0, "B": 1.0}},
        ],
    )


def test_response_url_strips_query_and_swaps_path():
    assert (
        response_url("https://docs.google.com/forms/d/e/ID/viewform?usp=sf_link")
        == "https://docs.google.com/forms/d/e/ID/formResponse"
    )


def test_build_payload_shape():
    payload = build_payload(Response(answers={"111111": "Red", "333333": ["A", "B"]}), fbzx="ZZ")
    assert payload["entry.111111"] == "Red"
    assert payload["entry.333333"] == ["A", "B"]
    assert payload["fvv"] == "1"
    assert payload["pageHistory"] == "0"
    assert payload["fbzx"] == "ZZ"


def test_gate_blocks_when_not_enabled():
    responses = generate(make_cfg())
    with pytest.raises(SubmissionNotAuthorized):
        submit_all(responses, make_cfg(enabled=False))


def test_gate_blocks_when_not_owned():
    responses = generate(make_cfg())
    with pytest.raises(SubmissionNotAuthorized):
        submit_all(responses, make_cfg(own=False))


def test_dry_run_does_not_submit_or_need_gate():
    rows = submit_all(generate(make_cfg(enabled=False)), make_cfg(enabled=False), dry_run=True)
    assert len(rows) == 3
    assert all(r["status"] == "DRY_RUN" for r in rows)


@responses_lib.activate
def test_submit_success_detected():
    url = response_url(make_cfg().form.url)
    responses_lib.add(
        responses_lib.POST, url, body="...Your response has been recorded...", status=200
    )
    rows = submit_all(generate(make_cfg()), make_cfg(), fbzx="z")
    assert len(rows) == 3
    assert all(r["success"] for r in rows)


# A re-rendered form (failure) still carries the questions list at data[1][1].
_RERENDER_BODY = (
    '<script>var FB_PUBLIC_LOAD_DATA_ = '
    '[null,[null,[[111111,"C",null,2,[[111111,[["Red"]]]]]]]];</script>'
)
# The confirmation page embeds the blob too, but without the questions list.
_CONFIRM_BODY = "<script>var FB_PUBLIC_LOAD_DATA_ = [null,[null,null]];</script>"


@responses_lib.activate
def test_validation_failure_detected():
    url = response_url(make_cfg().form.url)
    responses_lib.add(responses_lib.POST, url, body=_RERENDER_BODY, status=200)
    rows = submit_all(generate(make_cfg()), make_cfg(), fbzx="z")
    # stop_on_error defaults True -> stops after first failed submit.
    assert rows[0]["success"] is False
    assert len(rows) == 1


@responses_lib.activate
def test_confirmation_page_with_blob_is_success():
    # Regression: the current confirmation page embeds FB_PUBLIC_LOAD_DATA_ but
    # omits the questions list; this must be classified as success, not failure.
    url = response_url(make_cfg().form.url)
    responses_lib.add(responses_lib.POST, url, body=_CONFIRM_BODY, status=200)
    rows = submit_all(generate(make_cfg()), make_cfg(), fbzx="z")
    assert len(rows) == 3
    assert all(r["success"] for r in rows)


@responses_lib.activate
def test_non_200_is_failure():
    # Invalid option values now come back as HTTP 400.
    url = response_url(make_cfg().form.url)
    responses_lib.add(responses_lib.POST, url, body=_RERENDER_BODY, status=400)
    rows = submit_all(generate(make_cfg()), make_cfg(), fbzx="z")
    assert rows[0]["success"] is False
    assert len(rows) == 1
