# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`gform` is a Python CLI that deterministically generates multiple-choice survey
responses from user-defined distributions and submits them to a Google Form you
own (testing/QA). It is **intentionally non-evasive**: there are no proxy/UA
rotation, CAPTCHA, or sign-in bypass features. If a form requires authentication
the tool reports and refuses rather than working around it. Preserve this stance
when changing the importer or submitter.

## Commands

```powershell
pip install -e ".[dev]"   # install with dev/test extras (pytest, responses)
pytest                     # run all tests (fully offline — see below)
pytest tests/test_importer.py::test_name   # single test
```

CLI (installed as the `gform` entry point → `gform.cli:main`):

```powershell
gform import "<viewform-url>" -o config.yaml   # scaffold config from a live form
gform preview config.yaml                       # offline: target vs realized marginals + sample rows
gform run config.yaml --dry-run                 # build POST payloads, submit nothing
gform run config.yaml                            # submit (requires ownership gate, see below)
```

## Architecture

The pipeline is **import → config → generate → submit**, one module per stage
under `src/gform/`, with `models.py` holding the shared Pydantic types
(`QuestionType`, `Question`, `FormSchema`, `Response`) that flow between them.

- **importer.py** — Fetches the viewform page and parses the undocumented
  `FB_PUBLIC_LOAD_DATA_` JSON blob. All brittle nested-index access is isolated
  here behind `_safe_index` and guarded by a fixture test. The resolved
  (post-redirect) URL is stored so short links (`forms.gle/...`) become the
  canonical `docs.google.com/.../viewform` from which the `/formResponse` submit
  endpoint is derived. Google internal type codes map to our types via
  `TYPE_CODE_MAP` in models.py.

- **models.py** — Defines the supported question types. **Grids (type code 7)
  are decomposed at import time into one `Question` per row**: a multiple-choice
  grid row becomes `grid_radio`, a checkbox grid row becomes `grid_checkbox`.
  `SINGLE_CHOICE` vs `MULTI_CHOICE` sets drive distribution semantics everywhere
  downstream. A rating question (type code 18) is structurally a radio over its
  1..N rating values, so it maps onto `rating` (single-choice) via the generic
  importer path. Unsupported codes (text, date, time, file-upload, layout) are
  skipped on import with a warning.

  **"Other" (fill-in-the-blank) options** are detected in the importer by the
  per-option flag `opt[4] == 1` (their label is empty) and mapped to the
  `OTHER_OPTION` sentinel (`__other__`) rather than leaking in as an empty-label
  choice. `__other__` is a normal selectable option in the config (it gets a
  probability like any other). Each question may carry an `other_text` string;
  when `__other__` is selected, `build_payload` rewrites the value to Google's
  `OTHER_SUBMIT_VALUE` (`__other_option__`) and adds a sibling
  `entry.<id>.other_option_response=<other_text>` field. `validate_internal`
  requires `other_text` to be non-empty whenever `__other__` has positive
  probability (Google rejects an empty Other response).

- **config.py** — YAML config schema + three validation layers:
  `validate_internal` (offline: single-choice distributions sum to 1.0 within
  `_SUM_TOLERANCE`; checkbox values are independent probabilities in [0,1]),
  `validate_against_schema` (option labels must match the live form exactly;
  required questions must be present), and `scaffold_dict` (generates a config
  with uniform distributions, absorbing rounding drift into the last option).

- **generator.py / distributions.py** — Each question is sampled
  **independently** (columns generated separately, then zipped into rows, so
  questions are uncorrelated by construction). Two modes:
  - `exact` (default): proportions → integer quotas via the **largest-remainder
    method** (`largest_remainder`), then a seeded shuffle. Realized marginals
    match targets as closely as integer rounding allows.
  - `probabilistic`: seeded weighted sampling.
  Checkbox questions treat each option as an independent inclusion decision.
  **Determinism is a core invariant**: same config + seed ⇒ identical responses.
  Per-column/per-option seeds are derived from the base seed + index — preserve
  this seeding scheme so existing seeds keep reproducing.

- **submitter.py** — Two-part ownership gate (`submission.enabled` AND
  `submission.i_own_this_form`, checked in `_check_gate`; `run` adds an
  interactive `YES` prompt unless `--yes`). Rate-limited with seeded jitter.
  **Success classification is non-obvious**: Forms returns HTTP 200 for both
  success and (some) validation failures. The current confirmation page embeds
  `FB_PUBLIC_LOAD_DATA_` too, so its mere presence is *not* a failure signal.
  The two outcomes differ structurally instead: a re-rendered form (failure)
  still carries the questions list at `data[1][1]`, while the confirmation page
  omits it — so `is_success` treats a populated questions list as the
  language-independent failure signal. Writes a per-run audit CSV
  (`run-<seed>.csv`).

- **cli.py** — Typer app wiring the stages together. Note `run` always hits the
  live form first (even before submitting) to confirm accessibility, validate
  option labels, and capture a fresh `fbzx` token.

## Testing

All tests run **offline**: the importer is tested against a captured HTML
fixture (`tests/fixtures/sample_form.html`), and submission is tested with the
network mocked via the `responses` library. Do not introduce tests that make
real network calls. When changing `FB_PUBLIC_LOAD_DATA_` parsing, update or
re-capture the fixture rather than hardcoding indices in tests.
