# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`gform` is a Python **MCP server** (stdio, FastMCP) that authors Google Forms
through the official Forms API and deterministically generates/submits survey
responses from user-defined distributions to forms the user owns (testing/QA).
It is **intentionally non-evasive**: there are no proxy/UA rotation, CAPTCHA,
or sign-in bypass features. If a form requires authentication the tool reports
and refuses rather than working around it, and `fill_form` refuses without an
explicit `i_own_this_form=true`. Preserve this stance when changing the
importer, submitter, or server.

## Commands

```powershell
pip install -e ".[dev]"   # install with dev/test extras (pytest, responses)
pytest                     # run all tests (fully offline — see below)
pytest tests/test_importer.py::test_name   # single test

gform-auth                 # one-time interactive Google OAuth (token -> ~/.gform/token.json)
gform-mcp                  # run the MCP server on stdio (registered via `claude mcp add`)
```

## Architecture

Two halves that complement each other — the official API cannot submit
responses, and the public endpoint cannot author forms:

1. **Authoring** (OAuth): `auth.py` + `forms_api.py`, surfaced as the
   `create_form` / `publish_form` / `add_text_question` /
   `add_multiple_choice_question` / `get_form` / `get_form_responses` tools.
2. **Filling** (no auth): the original pipeline import → config → generate →
   submit, surfaced as `import_form_schema` / `preview_fill` / `fill_form`.
   The `entry.<id>` POST field IDs it scrapes are **unrelated** to the
   authoring `question_id`s.

Modules under `src/gform/`:

- **server.py** — FastMCP app wiring all 9 tools; `gform-mcp` entry point.
  A `tool_errors` decorator maps AuthError / FormAccessError / ConfigError /
  SubmissionNotAuthorized / googleapiclient HttpError / ValueError to readable
  `ToolError`s (never tracebacks). **Never print to stdout here** — stdio is
  the MCP transport; logging goes to stderr. The discovery service is cached
  module-level. Fill tools always hit the live form first to validate labels
  and capture a fresh `fbzx` token. `preview_fill`/`fill_form` take an optional
  `personas` arg (see config.py); `import_form_schema` also returns
  `persona_guidance` + a `persona_template` wired to the form's entry_ids to
  steer the agent toward realistic, correlated data, and `preview_fill` echoes
  the allocated `persona_mix`.

- **auth.py** — OAuth credential cache under `GFORM_HOME` (default `~/.gform`):
  `credentials.json` (client secret), `token.json` (cached token), `logs/`.
  `get_credentials(interactive=False)` is what the server uses — it loads/
  refreshes the token and raises `AuthError` with run-`gform-auth` instructions
  rather than ever opening a browser. The interactive flow lives only in the
  standalone `gform-auth` entry point (`main()`); keep it that way (a browser
  flow inside the stdio server would block and corrupt the protocol). Tokens
  whose stored scopes don't cover `SCOPES` are treated as absent.

- **forms_api.py** — Thin wrappers over forms.googleapis.com; every function
  takes the discovery `service` as first arg so tests inject a recording fake.
  `create_form` publishes explicitly after creating (API-created forms start
  unpublished since the June 30, 2026 Forms API change; legacy forms that
  reject `setPublishSettings` are reported as `legacy_default`). `createItem`
  appends by computing the index from `forms.get` when no index is given.
  `list_responses` flattens answers to `question_id -> [values]`.

- **importer.py** — Fetches the viewform page and parses the undocumented
  `FB_PUBLIC_LOAD_DATA_` JSON blob. All brittle nested-index access is isolated
  here behind `_safe_index` and guarded by a fixture test. The resolved
  (post-redirect) URL is stored so short links (`forms.gle/...`) become the
  canonical `docs.google.com/.../viewform` from which the `/formResponse`
  submit endpoint is derived. Google internal type codes map to our types via
  `TYPE_CODE_MAP` in models.py.

- **models.py** — Defines the supported question types. **Grids (type code 7)
  are decomposed at import time into one `Question` per row** (`grid_radio` /
  `grid_checkbox`). A rating question (code 18) maps onto `rating`
  (single-choice). **Text questions (codes 0/1) import as `text`/`paragraph`**
  with `options=[]`; they are in `TEXT_TYPES`, deliberately NOT in
  `SINGLE_CHOICE`/`MULTI_CHOICE`. Unsupported codes (date, time, file-upload,
  layout) are skipped on import with a warning.

  **"Other" (fill-in-the-blank) options** are detected by the per-option flag
  `opt[4] == 1` and mapped to the `OTHER_OPTION` sentinel (`__other__`). When
  selected, `build_payload` rewrites the value to `OTHER_SUBMIT_VALUE`
  (`__other_option__`) plus a sibling `entry.<id>.other_option_response`
  field; `validate_internal` requires a non-empty `other_text` whenever
  `__other__` has positive probability.

- **config.py** — Pydantic config models (no file format; configs are built
  inline from MCP tool args). For **text questions, `distribution` doubles as
  the answer pool** (sample text → weight): `validate_internal` requires a
  non-empty pool with weights > 0 and exempts it from the sum-to-1 rule;
  `validate_against_schema` skips the option-label check for text but errors
  on text-vs-choice type mismatches; `scaffold_dict` emits text questions with
  an empty `{}` pool for the user to fill in. Per-question distribution checks
  live in the reusable `_validate_distribution` helper, applied to both base
  questions and persona overrides.

  **Personas** (optional `Config.personas`) are respondent archetypes: each has
  a `weight` (share of respondents, summing to 1.0) and `distributions`
  (entry_id → its own distribution, falling back to the base question
  distribution for any entry it omits). They exist so a respondent's answers
  **correlate** across questions (rating + free text agree), which independent
  per-question sampling cannot do. The agent authors them — the server only
  validates/generates. `other_text` stays per-question (shared across personas).

- **generator.py / distributions.py** — Without personas, each question is
  sampled **independently** (the `_generate_block` column engine). `exact` mode:
  largest-remainder quotas + seeded shuffle; `probabilistic`: seeded weighted
  sampling. Text pools are normalized then routed through the same single-choice
  column path. **With personas**, `generate` apportions respondents to personas
  via `largest_remainder`, runs `_generate_block` once per persona over its
  *effective* distributions with a strided block seed
  (`seed + (persona_index+1) * 100_003`), then seeded-shuffles the rows.
  **Determinism is a core invariant**: same config + seed ⇒ identical responses;
  per-column seeds are `base seed + question index` (checkbox options:
  `"{seed}-{idx}-{opt_idx}"`) — preserve this scheme so existing seeds keep
  reproducing. The no-persona path is bit-identical to before the persona work.

- **submitter.py** — Ownership gate in `_check_gate` (`submission.enabled` AND
  `i_own_this_form`), seeded-jitter rate limiting, audit CSV. **Success
  classification is non-obvious**: Forms returns HTTP 200 for both success and
  some validation failures; `is_success` treats a populated questions list at
  `data[1][1]` as the language-independent failure signal (a confirmation page
  omits it). `build_payload` takes `text_entry_ids` so a literal `"__other__"`
  text answer is never rewritten to the Other magic value.

## Testing

All tests run **offline**: the importer against a captured HTML fixture
(`tests/fixtures/sample_form.html`), submission with the network mocked via
`responses`, the official-API wrappers against a hand-rolled recording
`FakeService` (asserting exact request bodies), and the server's tool registry
via FastMCP's `list_tools`. Do not introduce tests that make real network
calls. When changing `FB_PUBLIC_LOAD_DATA_` parsing, update or re-capture the
fixture rather than hardcoding indices in tests.
