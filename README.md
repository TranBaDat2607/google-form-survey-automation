# gform — Google Forms Survey Response Generator

A Python CLI that deterministically generates large numbers of **multiple-choice**
survey responses from user-defined distributions and submits them to a Google
Form **you own**, for testing/QA (the form itself, response collection, and any
downstream analysis pipeline).

> ⚠️ **Responsible use.** This tool is for forms you own or are explicitly
> authorized to test. Submitting fabricated responses to someone else's live
> survey to skew results is survey fraud and violates Google's Terms of Service.
> There are intentionally **no** anti-detection features; if a form requires
> sign-in or a CAPTCHA, the tool reports and refuses rather than bypassing it.

Supported question types: **multiple choice (radio), dropdown, checkboxes,
linear scale, multiple-choice grid (lưới trắc nghiệm), and checkbox grid (lưới
hộp kiểm)**. Each grid row is treated as its own question (a MC-grid row behaves
like radio; a checkbox-grid row like checkboxes). Free-text, paragraph,
date/time, and file-upload questions are out of scope.

## Install

```powershell
pip install -e ".[dev]"
```

## Workflow

```powershell
# 1. Import the form -> scaffold an editable config with uniform distributions
gform import "https://docs.google.com/forms/d/e/XXXX/viewform" -o config.yaml

# 2. Edit config.yaml: set per-option probabilities, count, seed.

# 3. Preview offline (no network, no submission): target vs. realized marginals
gform preview config.yaml

# 4. Inspect exactly what would be POSTed, without sending
gform run config.yaml --dry-run

# 5. Submit to your own form (set enabled + i_own_this_form: true first)
gform run config.yaml
```

## How it works

- **import** parses the form's embedded `FB_PUBLIC_LOAD_DATA_` to extract each
  multiple-choice question, its options, entry IDs, and required flags.
- **Determinism**: same config + seed ⇒ identical responses. In `exact` mode,
  proportions are converted to integer quotas via the largest-remainder method
  so realized marginals match your targets; `probabilistic` mode samples instead.
- Questions are sampled **independently**; checkbox options are independent
  inclusion probabilities.
- **Submission** is gated (config flags + interactive prompt), throttled with
  jitter, classifies success by Google's confirmation marker (Forms returns HTTP
  200 even on failure), and writes an audit CSV (`run-<seed>.csv`).

## Config

See [`examples/config.example.yaml`](examples/config.example.yaml). Single-choice
distributions must sum to 1.0; checkbox values are independent probabilities in
`[0, 1]`.

## Tests

```powershell
pytest
```

All unit tests run offline (the importer is tested against a captured HTML
fixture; submission is tested with the network mocked).
