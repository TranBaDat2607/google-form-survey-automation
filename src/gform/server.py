"""gform MCP server (stdio).

Hybrid surface: form authoring/reading goes through the official Google Forms
API (OAuth token created once via ``gform-auth``); response *filling* reuses
the public-endpoint pipeline (importer -> generator -> submitter), because the
official API cannot submit responses.

The non-evasive stance of the original tool is preserved: sign-in-gated forms
are refused, submission requires an explicit ownership confirmation, and rate
limiting stays on.

This process must never write to stdout (it would corrupt the MCP protocol);
all logging goes to stderr.
"""

from __future__ import annotations

import functools
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import List, Literal, Optional, Tuple

import requests
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from . import forms_api
from .auth import AuthError, build_drive_service, build_forms_service, logs_dir
from .config import (
    Config,
    ConfigError,
    Persona,
    QuestionConfig,
    scaffold_dict,
    validate_against_schema,
    validate_internal,
)
from .generator import allocate_personas, generate
from .importer import FormAccessError, import_form
from .models import MULTI_CHOICE, SINGLE_CHOICE, TEXT_TYPES, FormSchema
from .submitter import SubmissionNotAuthorized, response_url, submit_all

logger = logging.getLogger("gform")

INSTRUCTIONS = """Google Forms authoring + deterministic survey filling.

Authoring tools (create_form, publish_form, add_*_question, get_form,
get_form_responses) use the official Forms API and need a cached OAuth token
(run `gform-auth` once in a terminal).

Filling tools (import_form_schema, preview_fill, fill_form) work on the
form's public responder URL and need no Google auth — but they only work on
forms that are public (no sign-in required) and that the user OWNS. fill_form
refuses to submit unless i_own_this_form=true; only pass that after the user
has explicitly confirmed ownership. There are no evasion features: sign-in
gated forms are refused, and submissions are rate-limited.

Note: the entry IDs used by the filling tools come from the public form page
and are NOT the question_ids returned by the authoring tools — always call
import_form_schema (or use its suggested_fill_config) to get them.

For realistic data, pass `personas` to preview_fill/fill_form: each respondent
is then drawn from one archetype so their answers correlate (a satisfied
respondent rates high AND writes praise). See import_form_schema's
persona_guidance/persona_template. Without personas, every question is sampled
independently.
"""

# Steering shown to the agent so it authors realistic, correlated personas
# instead of independent uniform distributions.
PERSONA_GUIDANCE = (
    "To make responses look like real survey data, define `personas` and pass "
    "them to preview_fill/fill_form. Each persona is one respondent archetype "
    "with a `weight` (share of respondents; weights sum to 1.0) and "
    "`distributions` mapping entry_id -> that persona's own distribution. "
    "Infer 2-5 archetypes from the form's topic (e.g. satisfied / neutral / "
    "unhappy) and give each a coherent, NON-UNIFORM shape: a persona's rating, "
    "choices, and free-text pool should agree with each other. A persona only "
    "needs to list questions it answers differently; omitted entry_ids fall back "
    "to the base `questions` distribution (still provide valid base distributions "
    "— the suggested_fill_config gives uniform ones to start). For free-text "
    "questions, give each persona its OWN distinct pool of on-topic sample "
    "answers; supplying ~count/persona distinct equal-weight phrasings makes each "
    "respondent's text effectively unique under exact mode."
)

mcp = FastMCP("gform", instructions=INSTRUCTIONS)

_service = None
_drive_service = None


def _get_service():
    global _service
    if _service is None:
        _service = build_forms_service()
    return _service


def _get_drive_service():
    global _drive_service
    if _drive_service is None:
        _drive_service = build_drive_service()
    return _drive_service


def _format_http_error(exc: HttpError) -> str:
    status = exc.status_code
    msg = f"Google Forms API error {status}: {exc.reason}"
    if status == 403:
        msg += (
            " — check that the Google Forms API is enabled for your Cloud "
            "project and that the token has the right scopes (run "
            "`gform-auth --reauth`)."
        )
    elif status == 404:
        msg += " — no form with this form_id is visible to the authorized account."
    return msg


def tool_errors(fn):
    """Convert known failures into readable tool errors (never tracebacks)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except AuthError as exc:
            raise ToolError(str(exc))
        except FormAccessError as exc:
            raise ToolError(f"Form not accessible: {exc}")
        except SubmissionNotAuthorized as exc:
            raise ToolError(
                f"{exc} Pass i_own_this_form=true only after the user has "
                f"explicitly confirmed they own this form."
            )
        except ConfigError as exc:
            raise ToolError(str(exc))
        except ValueError as exc:
            raise ToolError(str(exc))
        except HttpError as exc:
            raise ToolError(_format_http_error(exc))
        except requests.RequestException as exc:
            raise ToolError(f"Network error fetching the form: {exc}")

    return wrapper


# --------------------------------------------------------------------------
# Authoring tools (official Forms API, OAuth required)
# --------------------------------------------------------------------------


@mcp.tool()
@tool_errors
def create_form(
    title: str,
    description: str = "",
    document_title: str = "",
    publish: bool = True,
    public: bool = True,
) -> dict:
    """Create a new Google Form owned by the authorized account.

    By default the form is also published and set to accept responses
    (API-created forms start unpublished). Returns form_id, the responder_uri
    (public fill-out URL — feed it to import_form_schema/fill_form), and the
    edit URL.

    public=true (default) additionally sets the form's sharing to "anyone with
    the link" so it does NOT require sign-in — important for Workspace
    (organization) accounts, whose forms are otherwise org-restricted and make
    the filling tools hit a 401. If your org's admin policy forbids
    anyone-with-link sharing, this can't override it: the result reports
    public_access="blocked" with guidance to re-auth with a personal Google
    account, instead of leaving you to discover the 401 later.
    """
    result = forms_api.create_form(
        _get_service(), title, description=description,
        document_title=document_title, publish=publish,
    )
    if public and publish:
        try:
            result.update(
                forms_api.share_with_anyone(_get_drive_service(), result["form_id"])
            )
        except HttpError as exc:
            result["public_access"] = "blocked"
            result["public_access_error"] = (
                "Created and published the form, but could not make it public "
                "(no sign-in). This is usually a Google Workspace admin policy "
                "that forbids 'anyone-with-the-link' sharing. The form will "
                "require organization sign-in, so the no-auth filling tools "
                "(import_form_schema/fill_form) will get a 401. To get a "
                "fillable test form, run `gform-auth --reauth` with a PERSONAL "
                f"Google account and recreate it. Detail: {_format_http_error(exc)}"
            )
    return result


@mcp.tool()
@tool_errors
def publish_form(
    form_id: str,
    published: bool = True,
    accepting_responses: bool = True,
) -> dict:
    """Publish/unpublish a form, or toggle whether it accepts responses.

    Use accepting_responses=false to close a form without unpublishing it.
    """
    return forms_api.set_publish_settings(
        _get_service(), form_id, published=published,
        accepting_responses=accepting_responses,
    )


@mcp.tool()
@tool_errors
def add_text_question(
    form_id: str,
    title: str,
    required: bool = False,
    paragraph: bool = False,
    index: Optional[int] = None,
) -> dict:
    """Add a text question (short answer, or paragraph=true for long answer).

    Appends at the end unless an explicit 0-based index is given. Returns the
    new item_id/question_id.
    """
    return forms_api.add_text_question(
        _get_service(), form_id, title, required=required,
        paragraph=paragraph, index=index,
    )


@mcp.tool()
@tool_errors
def add_multiple_choice_question(
    form_id: str,
    title: str,
    options: List[str],
    choice_type: Literal["radio", "checkbox", "dropdown"] = "radio",
    required: bool = False,
    include_other: bool = False,
    index: Optional[int] = None,
) -> dict:
    """Add a multiple-choice question (radio buttons, checkboxes or dropdown).

    include_other=true adds a free-text "Other" option (not supported for
    dropdowns). Appends at the end unless an explicit 0-based index is given.
    """
    return forms_api.add_choice_question(
        _get_service(), form_id, title, options, choice_type=choice_type,
        required=required, include_other=include_other, index=index,
    )


@mcp.tool()
@tool_errors
def get_form(form_id: str) -> dict:
    """Get a form's structure: title, items/questions, publish state, and the
    responder_uri used by the filling tools."""
    return forms_api.get_form(_get_service(), form_id)


@mcp.tool()
@tool_errors
def get_form_responses(
    form_id: str,
    page_size: int = 100,
    page_token: str = "",
    since_timestamp: str = "",
) -> dict:
    """List responses submitted to a form you own.

    Answers come back as question_id -> list of values. since_timestamp
    (RFC3339, e.g. 2026-06-01T00:00:00Z) filters to newer submissions.
    """
    return forms_api.list_responses(
        _get_service(), form_id, page_size=page_size,
        page_token=page_token, since_timestamp=since_timestamp,
    )


# --------------------------------------------------------------------------
# Filling tools (public-endpoint pipeline, no Google auth)
# --------------------------------------------------------------------------


def _build_config(
    schema: FormSchema,
    questions: List[QuestionConfig],
    count: int,
    seed: int,
    mode: str,
    personas: Optional[List[Persona]] = None,
    i_own_this_form: bool = False,
    rate_limit_per_min: float = 20.0,
    jitter_seconds: Tuple[float, float] = (1.0, 3.0),
    stop_on_error: bool = True,
) -> Config:
    cfg = Config(
        form={"url": schema.url},
        generation={"count": count, "seed": seed, "mode": mode},
        submission={
            "enabled": True,
            "i_own_this_form": i_own_this_form,
            "rate_limit_per_min": rate_limit_per_min,
            "jitter_seconds": jitter_seconds,
            "stop_on_error": stop_on_error,
        },
        questions=questions,
        personas=personas or [],
    )
    validate_internal(cfg)
    validate_against_schema(cfg, schema)
    return cfg


def _marginals(cfg: Config, responses) -> dict:
    out: dict = {}
    for qc in cfg.questions:
        if qc.type in MULTI_CHOICE:
            counts = Counter()
            for r in responses:
                counts.update(r.answers[qc.entry_id])
        else:
            counts = Counter(r.answers[qc.entry_id] for r in responses)
        out[qc.entry_id] = dict(counts)
    return out


def _persona_template(schema: FormSchema) -> dict:
    """Build an illustrative 2-persona example wired to this form's real ids.

    The weights/shapes are placeholders to show structure — the agent should
    replace them with archetypes and sentiment that actually fit the form.
    """
    choice_q = next(
        (q for q in schema.questions if q.type in SINGLE_CHOICE and q.options), None
    )
    text_q = next((q for q in schema.questions if q.type in TEXT_TYPES), None)

    def _biased(options: List[str], heavy_first: bool) -> dict:
        heavy = options[0] if heavy_first else options[-1]
        rest = [o for o in options if o != heavy]
        share = round(0.15 / len(rest), 4) if rest else 0.0
        dist = {o: share for o in rest}
        dist[heavy] = round(1.0 - share * len(rest), 4)
        return dist

    def _persona(name: str, weight: float, heavy_first: bool, pool: dict) -> dict:
        distributions: dict = {}
        if choice_q:
            distributions[choice_q.entry_id] = _biased(choice_q.options, heavy_first)
        if text_q:
            distributions[text_q.entry_id] = pool
        return {"name": name, "weight": weight, "distributions": distributions}

    return {
        "_note": (
            "Example only — replace names, weights, and answers with ones that fit "
            "this form. Persona weights must sum to 1.0; give each persona its own "
            "distinct, on-topic text pool."
        ),
        "personas": [
            _persona("satisfied", 0.7, True,
                     {"Loved it": 1, "Great experience": 1, "Would recommend": 1}),
            _persona("unhappy", 0.3, False,
                     {"Disappointing": 1, "Needs work": 1, "Would not return": 1}),
        ],
    }


@mcp.tool()
@tool_errors
def import_form_schema(url: str) -> dict:
    """Read a public form's structure from its responder/viewform URL.

    Returns the questions with their entry_ids (the POST field IDs needed by
    preview_fill/fill_form — these are NOT the authoring question_ids) plus a
    suggested_fill_config with uniform distributions. Text questions get an
    empty pool: fill it with sample answers (text -> weight) before
    generating. For realistic, correlated data, also returns persona_guidance
    and a persona_template wired to this form's entry_ids — author personas from
    those and pass them to preview_fill/fill_form. Refuses forms that require
    sign-in.
    """
    schema = import_form(url)
    scaffold = scaffold_dict(schema, count=100, seed=42)
    return {
        "title": schema.title,
        "form_id": schema.form_id,
        "resolved_url": schema.url,
        "questions": [q.model_dump(mode="json") for q in schema.questions],
        "warnings": schema.warnings,
        "suggested_fill_config": {
            "count": 100,
            "seed": 42,
            "mode": "exact",
            "questions": scaffold["questions"],
        },
        "persona_guidance": PERSONA_GUIDANCE,
        "persona_template": _persona_template(schema),
    }


@mcp.tool()
@tool_errors
def preview_fill(
    url: str,
    questions: List[QuestionConfig],
    count: int = 100,
    seed: int = 42,
    mode: Literal["exact", "probabilistic"] = "exact",
    personas: Optional[List[Persona]] = None,
) -> dict:
    """Validate a fill config and preview the generated answers — no submission.

    For each question, distribution maps option label -> probability
    (single-choice must sum to 1.0; checkbox values are independent
    probabilities; text questions use sample-answer -> weight pools).

    Optional `personas` make responses coherent per respondent instead of
    sampling each question independently: each persona has a `weight` (share of
    respondents, summing to 1.0) and `distributions` (entry_id -> its own
    distribution, falling back to the base `questions` distribution for any
    entry it omits). Use this so one respondent's rating, choices, and free text
    move together. Returns realized marginal counts, the first 5 generated rows,
    and `persona_mix` (respondents allocated per persona).
    """
    schema = import_form(url)
    cfg = _build_config(schema, questions, count, seed, mode, personas=personas)
    responses = generate(cfg)
    result = {
        "count": count,
        "seed": seed,
        "mode": mode,
        "validation": "ok",
        "marginals": _marginals(cfg, responses),
        "sample_rows": [r.answers for r in responses[:5]],
    }
    if cfg.personas:
        result["persona_mix"] = allocate_personas(cfg.personas, count)
    return result


@mcp.tool()
@tool_errors
def fill_form(
    url: str,
    questions: List[QuestionConfig],
    count: int,
    i_own_this_form: bool = False,
    seed: int = 42,
    mode: Literal["exact", "probabilistic"] = "exact",
    personas: Optional[List[Persona]] = None,
    rate_limit_per_min: float = 20.0,
    jitter_min_seconds: float = 1.0,
    jitter_max_seconds: float = 3.0,
    stop_on_error: bool = True,
    dry_run: bool = False,
) -> dict:
    """Generate responses from the given distributions and submit them.

    OWNERSHIP GATE: refuses unless i_own_this_form=true. Only pass true after
    the user has explicitly confirmed they own (or are authorized to test)
    this form. Use dry_run=true first to inspect the payloads without
    submitting anything.

    Optional `personas` make each respondent internally coherent (see
    preview_fill); omit them for the original independent-per-question sampling.

    Submission is rate-limited (default 20/min, so 100 responses take ~6
    minutes); prefer small counts per call. Returns per-run totals and the
    audit CSV path (under ~/.gform/logs).
    """
    schema = import_form(url)
    cfg = _build_config(
        schema, questions, count, seed, mode,
        personas=personas,
        i_own_this_form=i_own_this_form,
        rate_limit_per_min=rate_limit_per_min,
        jitter_seconds=(jitter_min_seconds, jitter_max_seconds),
        stop_on_error=stop_on_error,
    )
    responses = generate(cfg)

    log_path = None
    if not dry_run:
        logs = logs_dir()
        logs.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = str(logs / f"run-{stamp}-seed{seed}.csv")

    rows = submit_all(
        responses, cfg, fbzx=schema.fbzx, dry_run=dry_run,
        log_path=log_path, form_url=schema.url,
    )

    result = {
        "dry_run": dry_run,
        "form_title": schema.title,
        "endpoint": response_url(schema.url),
        "total": count,
        "attempted": len(rows),
        "submitted": sum(1 for r in rows if r["success"] is True),
        "failed": sum(1 for r in rows if r["success"] is False),
        "stopped_early": len(rows) < count,
        "log_path": log_path,
    }
    if dry_run:
        result["first_payloads"] = [r["payload"] for r in rows[:3]]
    return result


def main() -> None:
    """Console entry point for ``gform-mcp``."""
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    logger.info("Starting gform MCP server (stdio)")
    mcp.run()


if __name__ == "__main__":
    main()
