"""Offline tests for the MCP server layer.

The fill tools are exercised end-to-end against the captured viewform fixture
(network mocked with `responses`); the authoring tools' bodies are one-line
shims over forms_api (tested in test_forms_api.py with a FakeService), so here
we only verify registration, schemas, and error mapping.
"""

from pathlib import Path

import anyio
import httplib2
import pytest
import responses as responses_lib
from googleapiclient.errors import HttpError
from mcp.server.fastmcp.exceptions import ToolError

from gform.auth import AuthError
from gform.config import ConfigError, Persona, QuestionConfig
from gform.importer import FormAccessError
from gform.server import (
    fill_form,
    import_form_schema,
    mcp,
    preview_fill,
    tool_errors,
)
from gform.submitter import SubmissionNotAuthorized

FIXTURE = (Path(__file__).parent / "fixtures" / "sample_form.html").read_text(encoding="utf-8")
URL = "https://docs.google.com/forms/d/e/1FAIpQLScTESTFORMID/viewform"
POST_URL = "https://docs.google.com/forms/d/e/1FAIpQLScTESTFORMID/formResponse"

EXPECTED_TOOLS = {
    "create_form",
    "publish_form",
    "add_text_question",
    "add_multiple_choice_question",
    "get_form",
    "get_form_responses",
    "import_form_schema",
    "preview_fill",
    "fill_form",
}


# Covers every required question on the fixture form (validate_against_schema
# insists on required coverage), including a text pool for the paragraph Q.
def _questions():
    return [
        QuestionConfig(entry_id="111111", type="radio",
                       distribution={"Red": 0.5, "Blue": 0.25, "Green": 0.25}),
        QuestionConfig(entry_id="444444", type="linear_scale",
                       distribution={"1": 0.2, "2": 0.2, "3": 0.2, "4": 0.2, "5": 0.2}),
        QuestionConfig(entry_id="666666", type="paragraph",
                       distribution={"Great service": 3.0, "Could be better": 1.0}),
        QuestionConfig(entry_id="611111", type="grid_radio",
                       distribution={"Bad": 0.5, "Good": 0.5}),
        QuestionConfig(entry_id="611112", type="grid_radio",
                       distribution={"Bad": 0.5, "Good": 0.5}),
    ]


def test_all_nine_tools_registered_with_schemas():
    tools = anyio.run(mcp.list_tools)
    assert {t.name for t in tools} == EXPECTED_TOOLS

    fill = next(t for t in tools if t.name == "fill_form")
    props = fill.inputSchema["properties"]
    assert props["i_own_this_form"]["default"] is False
    assert props["dry_run"]["default"] is False
    assert set(fill.inputSchema["required"]) == {"url", "questions", "count"}


def test_tool_errors_mapping():
    def boom(exc):
        @tool_errors
        def fn():
            raise exc
        return fn

    with pytest.raises(ToolError, match="gform-auth"):
        boom(AuthError("No valid Google token. Run `gform-auth`."))()
    with pytest.raises(ToolError, match="Form not accessible"):
        boom(FormAccessError("requires sign-in"))()
    with pytest.raises(ToolError, match="i_own_this_form=true"):
        boom(SubmissionNotAuthorized("submission.i_own_this_form is false."))()
    with pytest.raises(ToolError, match="validation failed"):
        boom(ConfigError("Config validation failed:\n  - x"))()

    resp_403 = httplib2.Response({"status": "403", "reason": "Forbidden"})
    with pytest.raises(ToolError, match="gform-auth --reauth"):
        boom(HttpError(resp_403, b"{}"))()
    resp_404 = httplib2.Response({"status": "404", "reason": "Not Found"})
    with pytest.raises(ToolError, match="form_id"):
        boom(HttpError(resp_404, b"{}"))()


@responses_lib.activate
def test_import_form_schema_returns_entry_ids_and_scaffold():
    responses_lib.add(responses_lib.GET, URL, body=FIXTURE, status=200)
    result = import_form_schema(URL)

    assert result["title"] == "My Test Form"
    ids = {q["entry_id"] for q in result["questions"]}
    assert {"111111", "555555", "666666", "611111"} <= ids

    suggested = {q["entry_id"]: q for q in result["suggested_fill_config"]["questions"]}
    # Text questions are scaffolded with an empty pool to be filled in.
    assert suggested["555555"]["distribution"] == {}
    # Choice questions get uniform distributions summing to 1.
    assert sum(suggested["111111"]["distribution"].values()) == pytest.approx(1.0)


@responses_lib.activate
def test_preview_fill_marginals_and_samples():
    responses_lib.add(responses_lib.GET, URL, body=FIXTURE, status=200)
    result = preview_fill(URL, _questions(), count=4, seed=1)

    assert result["validation"] == "ok"
    assert result["marginals"]["111111"] == {"Red": 2, "Blue": 1, "Green": 1}
    assert result["marginals"]["666666"] == {"Great service": 3, "Could be better": 1}
    assert len(result["sample_rows"]) == 4


@responses_lib.activate
def test_preview_fill_with_personas_returns_mix_and_correlates():
    responses_lib.add(responses_lib.GET, URL, body=FIXTURE, status=200)
    personas = [
        Persona(name="fan", weight=0.5, distributions={
            "111111": {"Red": 1.0, "Blue": 0.0, "Green": 0.0},
            "666666": {"Best form ever": 1.0},
        }),
        Persona(name="critic", weight=0.5, distributions={
            "111111": {"Red": 0.0, "Blue": 0.0, "Green": 1.0},
            "666666": {"Needs work": 1.0},
        }),
    ]
    result = preview_fill(URL, _questions(), count=10, seed=1, personas=personas)

    assert result["persona_mix"] == {"fan": 5, "critic": 5}
    assert result["marginals"]["111111"] == {"Red": 5, "Green": 5}
    # Within-respondent correlation survives end-to-end: the rating and the
    # free-text comment come from the same persona on every row.
    for row in result["sample_rows"]:
        if row["111111"] == "Red":
            assert row["666666"] == "Best form ever"
        if row["111111"] == "Green":
            assert row["666666"] == "Needs work"


@responses_lib.activate
def test_import_form_schema_includes_persona_template():
    responses_lib.add(responses_lib.GET, URL, body=FIXTURE, status=200)
    result = import_form_schema(URL)

    assert "persona_guidance" in result
    template = result["persona_template"]
    # Wired to real entry_ids and weights sum to 1.0.
    assert sum(p["weight"] for p in template["personas"]) == pytest.approx(1.0)
    ids = {q["entry_id"] for q in result["questions"]}
    for persona in template["personas"]:
        assert set(persona["distributions"]) <= ids


@responses_lib.activate
def test_preview_fill_bad_option_label_is_tool_error():
    responses_lib.add(responses_lib.GET, URL, body=FIXTURE, status=200)
    bad = _questions()
    bad[0] = QuestionConfig(entry_id="111111", type="radio",
                            distribution={"Crimson": 1.0})
    with pytest.raises(ToolError, match="does not match any choice"):
        preview_fill(URL, bad, count=4)


@responses_lib.activate
def test_fill_form_dry_run_builds_payloads_without_gate():
    responses_lib.add(responses_lib.GET, URL, body=FIXTURE, status=200)
    result = fill_form(URL, _questions(), count=4, dry_run=True)

    assert result["dry_run"] is True
    assert result["endpoint"] == POST_URL
    assert result["attempted"] == 4
    assert result["submitted"] == 0
    assert len(result["first_payloads"]) == 3
    assert result["first_payloads"][0]["entry.666666"] in (
        "Great service", "Could be better",
    )
    assert result["log_path"] is None


@responses_lib.activate
def test_fill_form_refuses_without_ownership():
    responses_lib.add(responses_lib.GET, URL, body=FIXTURE, status=200)
    with pytest.raises(ToolError, match="i_own_this_form"):
        fill_form(URL, _questions(), count=2)


@responses_lib.activate
def test_fill_form_submits_and_writes_audit_log(tmp_path, monkeypatch):
    monkeypatch.setenv("GFORM_HOME", str(tmp_path))
    responses_lib.add(responses_lib.GET, URL, body=FIXTURE, status=200)
    responses_lib.add(
        responses_lib.POST, POST_URL,
        body="...Your response has been recorded...", status=200,
    )
    result = fill_form(
        URL, _questions(), count=2, i_own_this_form=True,
        rate_limit_per_min=6000, jitter_min_seconds=0, jitter_max_seconds=0,
    )

    assert result["submitted"] == 2
    assert result["failed"] == 0
    assert result["stopped_early"] is False
    log = Path(result["log_path"])
    assert log.exists()
    assert log.parent == tmp_path / "logs"
