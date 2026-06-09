"""Fetch a Google Form and parse its structure from FB_PUBLIC_LOAD_DATA_.

The FB_PUBLIC_LOAD_DATA_ layout is undocumented and version-fragile, so all of
the brittle index access is isolated here behind ``parse_form`` and guarded by a
fixture-based test (tests/test_importer.py).
"""

from __future__ import annotations

import json
import re
from typing import Optional

import requests

from .models import FormSchema, OTHER_OPTION, Question, QuestionType, TYPE_CODE_MAP

# A plain, current browser UA so Google serves the normal public form page.
# This is NOT an evasion measure; the tool refuses sign-in-gated forms outright.
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# The blob is always terminated by ";</script>".
_FB_RE = re.compile(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(.*?);\s*</script>", re.DOTALL)
_FBZX_RE = re.compile(r'name="fbzx"\s+value="([^"]+)"')

# File-upload questions force sign-in; flag them rather than silently dropping.
_FILE_UPLOAD_CODE = 13
# Grids (multiple-choice grid + checkbox grid) share this code; each row of the
# grid is a separate answer entry, and r[11] flags radio (0) vs checkbox (1).
_GRID_CODE = 7
_GRID_CHECKBOX_FLAG_INDEX = 11
# An option is the free-text "Other" choice when opt[4] == 1 (its label is "").
_OTHER_FLAG_INDEX = 4


def _option_label(opt) -> str:
    """Label for one raw option, mapping the "Other" choice to its sentinel."""
    if len(opt) > _OTHER_FLAG_INDEX and opt[_OTHER_FLAG_INDEX] == 1:
        return OTHER_OPTION
    return str(opt[0])


class FormAccessError(RuntimeError):
    """Raised when a form is private, sign-in gated, closed, or unparseable."""


def fetch_form(url: str, session: Optional[requests.Session] = None, timeout: int = 30):
    """GET the viewform page, following redirects."""
    session = session or requests.Session()
    resp = session.get(
        url, headers={"User-Agent": DEFAULT_UA}, timeout=timeout, allow_redirects=True
    )
    resp.raise_for_status()
    return resp


def extract_fbzx(html: str) -> Optional[str]:
    """Pull the per-load fbzx token from the page's hidden input."""
    m = _FBZX_RE.search(html)
    return m.group(1) if m else None


def _safe_index(seq, *path):
    """Index into nested lists, returning None on any missing/None step."""
    cur = seq
    for key in path:
        try:
            cur = cur[key]
        except (IndexError, KeyError, TypeError):
            return None
    return cur


def _parse_grid(title: str, rows) -> list:
    """Expand a grid (type 7) into one Question per row.

    Each row `r`: r[0]=row entry id, r[1]=column options, r[2]=required,
    r[3][0]=row label, r[11]=[1] for checkbox grid / [0] for multiple-choice.
    """
    questions = []
    for r in rows:
        entry_id = str(r[0])
        raw_cols = r[1] or []
        columns = [str(col[0]) for col in raw_cols if col]
        required = bool(r[2]) if len(r) > 2 else False

        row_label = ""
        if len(r) > 3 and r[3]:
            row_label = str(r[3][0]) if r[3][0] is not None else ""

        flag = _safe_index(r, _GRID_CHECKBOX_FLAG_INDEX, 0)
        q_type = QuestionType.grid_checkbox if flag == 1 else QuestionType.grid_radio

        full_title = f"{title} [{row_label}]" if row_label else title
        questions.append(
            Question(
                entry_id=entry_id,
                title=full_title,
                type=q_type,
                options=columns,
                required=required,
            )
        )
    return questions


def parse_form(html: str, url: str, final_url: Optional[str] = None) -> FormSchema:
    """Parse viewform HTML into a FormSchema.

    Raises FormAccessError if the form requires sign-in or has no parseable data.
    """
    if final_url and "accounts.google.com" in final_url:
        raise FormAccessError(
            "This form redirects to Google sign-in; it requires authentication "
            "and is not supported."
        )

    match = _FB_RE.search(html)
    if not match:
        raise FormAccessError(
            "Could not locate FB_PUBLIC_LOAD_DATA_. The form may require sign-in, "
            "be closed, or not be a public Google Form."
        )

    data = json.loads(match.group(1))

    title = _safe_index(data, 3) or ""
    form_id = _safe_index(data, 14) or ""
    raw_questions = _safe_index(data, 1, 1) or []

    questions = []
    warnings = []
    for q in raw_questions:
        q_title = _safe_index(q, 1) or ""
        type_code = _safe_index(q, 3)
        entries = _safe_index(q, 4)

        # Section headers, images and page breaks have no answer container.
        if entries is None:
            continue
        if type_code == _FILE_UPLOAD_CODE:
            warnings.append(
                f"Skipped '{q_title}': file-upload questions require sign-in."
            )
            continue

        if type_code == _GRID_CODE:
            # q[4] holds one element per grid row.
            questions.extend(_parse_grid(q_title, entries))
            continue

        q_type = TYPE_CODE_MAP.get(type_code)
        if q_type is None:
            warnings.append(
                f"Skipped '{q_title}': unsupported question type (code {type_code})."
            )
            continue

        entry = entries[0]
        entry_id = str(entry[0])
        raw_opts = entry[1] or []
        options = [_option_label(opt) for opt in raw_opts if opt]
        required = bool(entry[2]) if len(entry) > 2 else False

        questions.append(
            Question(
                entry_id=entry_id,
                title=q_title,
                type=q_type,
                options=options,
                required=required,
            )
        )

    return FormSchema(
        title=str(title),
        form_id=str(form_id),
        url=url,
        fbzx=extract_fbzx(html),
        questions=questions,
        warnings=warnings,
    )


def import_form(url: str, session: Optional[requests.Session] = None) -> FormSchema:
    """Fetch and parse a form in one step.

    The schema stores the *resolved* URL (after following redirects), so short
    links like forms.gle/... become the canonical docs.google.com/.../viewform
    URL that the /formResponse submit endpoint is derived from.
    """
    resp = fetch_form(url, session=session)
    return parse_form(resp.text, url=str(resp.url), final_url=str(resp.url))
