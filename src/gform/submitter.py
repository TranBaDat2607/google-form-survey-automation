"""Submit generated responses to a live form you own.

Safeguards: a two-part ownership gate, conservative rate-limiting with jitter,
and success classification by confirmation marker (Forms returns HTTP 200 even
on validation failure). There is deliberately no proxy/UA rotation or any other
anti-detection behaviour.
"""

from __future__ import annotations

import csv
import json
import random
import time
from datetime import datetime, timezone
from typing import Callable, List, Optional, Set

import requests

from .config import Config
from .importer import DEFAULT_UA, _FB_RE
from .models import OTHER_OPTION, OTHER_SUBMIT_VALUE, TEXT_TYPES, Response

# Markers Google renders on the (English) "response recorded" confirmation page.
CONFIRM_MARKERS = (
    "freebirdFormviewerViewResponseConfirmationMessage",
    "Your response has been recorded",
)


class SubmissionNotAuthorized(PermissionError):
    """Raised when the ownership gate is not satisfied."""


def response_url(form_url: str) -> str:
    """Derive the /formResponse POST endpoint from a viewform URL."""
    base = form_url.split("?", 1)[0]
    if "/viewform" in base:
        return base.replace("/viewform", "/formResponse")
    if base.endswith("/formResponse"):
        return base
    return base.rstrip("/") + "/formResponse"


def build_payload(
    response: Response,
    fbzx: Optional[str] = None,
    other_texts: Optional[dict] = None,
    text_entry_ids: Optional[Set[str]] = None,
) -> dict:
    """Build the urlencoded POST body for one response.

    Single-choice answers map to one value; checkbox answers map to a list,
    which requests serializes as repeated entry.<id>=... keys. Free-text
    answers (entry ids in ``text_entry_ids``) are submitted verbatim.

    The "Other" sentinel (OTHER_OPTION) is rewritten to Google's
    OTHER_SUBMIT_VALUE, and a sibling ``entry.<id>.other_option_response`` field
    is added carrying the free text from ``other_texts`` (entry_id -> text).
    """
    other_texts = other_texts or {}
    text_entry_ids = text_entry_ids or set()
    data: dict = {}
    for eid, val in response.answers.items():
        key = f"entry.{eid}"
        if eid in text_entry_ids:
            # A literal "__other__" in a text answer must not be rewritten.
            data[key] = val
            continue
        if isinstance(val, list):
            chose_other = OTHER_OPTION in val
            data[key] = [
                OTHER_SUBMIT_VALUE if v == OTHER_OPTION else v for v in val
            ]
        else:
            chose_other = val == OTHER_OPTION
            data[key] = OTHER_SUBMIT_VALUE if chose_other else val
        if chose_other:
            data[f"{key}.other_option_response"] = other_texts.get(eid, "")

    data["fvv"] = "1"
    data["pageHistory"] = "0"
    if fbzx:
        data["fbzx"] = fbzx
    return data


def is_success(text: str) -> bool:
    """Decide whether a 200 response represents a recorded submission.

    Google returns HTTP 200 for both success and (some) validation failures.
    The mere presence of FB_PUBLIC_LOAD_DATA_ is *no longer* a failure signal:
    the current confirmation page also embeds the blob. The two outcomes differ
    structurally instead — a re-rendered form (failure) still carries the
    questions list at ``data[1][1]``; the confirmation page omits it. So a
    populated questions list is the language-independent failure signal.
    """
    if any(marker in text for marker in CONFIRM_MARKERS):
        return True
    if "FB_PUBLIC_LOAD_DATA_" not in text:
        # No blob at all: a minimal confirmation page.
        return True
    match = _FB_RE.search(text)
    if not match:
        return True
    try:
        data = json.loads(match.group(1))
        questions = data[1][1]
    except (ValueError, IndexError, TypeError, KeyError):
        # Blob present but no parseable questions container -> not a re-rendered
        # form, so treat as a confirmation page.
        return True
    return not questions


def _check_gate(config: Config) -> None:
    sub = config.submission
    if not sub.enabled:
        raise SubmissionNotAuthorized(
            "submission.enabled is false. Set it to true to submit."
        )
    if not sub.i_own_this_form:
        raise SubmissionNotAuthorized(
            "submission.i_own_this_form is false. Only submit to forms you own "
            "or are authorized to test."
        )


def submit_all(
    responses: List[Response],
    config: Config,
    fbzx: Optional[str] = None,
    dry_run: bool = False,
    log_path: Optional[str] = None,
    on_progress: Optional[Callable[[int, object, dict], None]] = None,
    form_url: Optional[str] = None,
) -> List[dict]:
    """Submit (or, with dry_run, just build) every response.

    ``form_url`` should be the resolved viewform URL (e.g. from the imported
    schema); it falls back to config.form.url. This matters for short links,
    which must be resolved before deriving the /formResponse endpoint.

    Returns a list of per-row result dicts; also writes them to log_path if given.
    """
    if not dry_run:
        _check_gate(config)

    sub = config.submission
    url = response_url(form_url or config.form.url)
    delay = 60.0 / sub.rate_limit_per_min if sub.rate_limit_per_min > 0 else 0.0
    jitter_rng = random.Random(config.generation.seed)

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA})

    other_texts = {qc.entry_id: qc.other_text for qc in config.questions}
    text_ids = {qc.entry_id for qc in config.questions if qc.type in TEXT_TYPES}

    rows: List[dict] = []
    for i, resp in enumerate(responses):
        payload = build_payload(resp, fbzx, other_texts, text_ids)

        if dry_run:
            rows.append(
                {"index": i, "timestamp": "", "status": "DRY_RUN", "success": "", "payload": payload}
            )
            if on_progress:
                on_progress(i, "DRY_RUN", payload)
            continue

        ts = datetime.now(timezone.utc).isoformat()
        try:
            r = session.post(url, data=payload, timeout=30)
            status = r.status_code
            ok = r.status_code == 200 and is_success(r.text)
        except requests.RequestException as exc:
            status = f"ERROR:{type(exc).__name__}"
            ok = False

        rows.append(
            {"index": i, "timestamp": ts, "status": status, "success": ok, "payload": payload}
        )
        if on_progress:
            on_progress(i, status, payload)

        if not ok and sub.stop_on_error:
            break

        if i < len(responses) - 1:
            time.sleep(delay + jitter_rng.uniform(sub.jitter_seconds[0], sub.jitter_seconds[1]))

    if log_path:
        _write_log(log_path, rows)
    return rows


def _format_answers(payload: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in payload.items() if k.startswith("entry."))


def _write_log(path: str, rows: List[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["index", "timestamp", "status", "success", "answers"])
        for r in rows:
            writer.writerow(
                [r["index"], r["timestamp"], r["status"], r["success"], _format_answers(r["payload"])]
            )
