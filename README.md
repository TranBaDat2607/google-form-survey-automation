# gform — Google Forms MCP Server

An MCP (Model Context Protocol) server that lets an AI assistant (Claude Code,
Claude Desktop, …) **author Google Forms** through the official Forms API and
**deterministically fill them with survey responses** from user-defined
distributions — for testing/QA of forms **you own** (the form itself, response
collection, and any downstream analysis pipeline).

> ⚠️ **Responsible use.** This tool is for forms you own or are explicitly
> authorized to test. Submitting fabricated responses to someone else's live
> survey to skew results is survey fraud and violates Google's Terms of Service.
> There are intentionally **no** anti-detection features; if a form requires
> sign-in or a CAPTCHA, the tool reports and refuses rather than bypassing it.
> Filling additionally requires an explicit `i_own_this_form=true` confirmation.

## How it's wired

The server is a hybrid of two Google integrations, because each can do what the
other can't:

| Capability | Mechanism |
|---|---|
| Create forms, add questions, read structure & collected responses | **Official Forms API** (`forms.googleapis.com`, OAuth) |
| Submit responses | **Public `/formResponse` endpoint** (the official API cannot submit) |

The filling pipeline parses the public form page for its `entry.<id>` POST
field IDs — these are unrelated to the `question_id`s the authoring tools
return, which is why `import_form_schema` exists.

## Setup

### 1. Install

```powershell
pip install nmvv-gform-mcp
```

Requires Python ≥ 3.10. This puts the `gform-mcp` and `gform-auth` commands on
your PATH. (Prefer `pipx install nmvv-gform-mcp` to keep it in an isolated
environment.) To work on the source instead, clone the repo and run
`pip install -e ".[dev]"`.

### 2. Google Cloud OAuth (one-time, free — needed only for the authoring tools)

1. Create a project at <https://console.cloud.google.com/>.
2. **APIs & Services → Library** → enable the **Google Forms API**.
3. **APIs & Services → OAuth consent screen** → External, Testing; add your
   own Google account as a test user.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID →
   Desktop app**, then download the JSON.
5. Save it as `~/.gform/credentials.json` (or point `GFORM_CREDENTIALS` at it).

### 3. Authorize

```powershell
gform-auth          # opens a browser once, caches the token at ~/.gform/token.json
gform-auth --reauth # discard the cached token and sign in again
```

The MCP server never opens a browser itself — if the token is missing or
unusable, its tools return an error telling you to run `gform-auth`.

### 4. Register the server

```powershell
# Claude Code (use the full path to the script in your Python environment):
claude mcp add gform -- "C:\path\to\env\Scripts\gform-mcp.exe"
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "gform": { "command": "C:\\path\\to\\env\\Scripts\\gform-mcp.exe" }
  }
}
```

## Tools

### Authoring (official API, OAuth)

| Tool | What it does |
|---|---|
| `create_form` | Create a form (title, optional description); publishes it by default and returns `form_id`, `responder_uri`, edit URL |
| `publish_form` | Publish/unpublish, or close a form (`accepting_responses=false`) |
| `add_text_question` | Add a short-answer or paragraph question |
| `add_multiple_choice_question` | Add a radio/checkbox/dropdown question, optionally with an "Other" option |
| `get_form` | Form structure: items, question IDs, publish state, `responder_uri` |
| `get_form_responses` | Collected responses, flattened to `question_id → [values]` |

Forms created through the API start **unpublished** (Forms API change effective
June 30, 2026); `create_form` publishes explicitly so the form can accept
responses right away.

### Filling (public endpoint, no OAuth)

| Tool | What it does |
|---|---|
| `import_form_schema` | Read a public form's questions + `entry_id`s; returns a `suggested_fill_config` with uniform distributions |
| `preview_fill` | Validate a fill config and preview realized marginals + sample rows — **no submission** |
| `fill_form` | Generate + submit; requires `i_own_this_form=true`; `dry_run=true` shows payloads without sending |

A fill config is a list of per-question distributions, passed inline:

```json
[
  {"entry_id": "111111", "type": "radio",
   "distribution": {"Red": 0.6, "Blue": 0.3, "Green": 0.1}},
  {"entry_id": "333333", "type": "checkbox",
   "distribution": {"Search": 0.7, "Export": 0.4}},
  {"entry_id": "999999", "type": "radio",
   "distribution": {"Student": 0.8, "__other__": 0.2},
   "other_text": "Research assistant"},
  {"entry_id": "555555", "type": "text",
   "distribution": {"Great service": 3, "Could be better": 1}}
]
```

- Single-choice distributions (radio, dropdown, linear_scale, rating,
  grid_radio) must sum to 1.0.
- Checkbox values are independent inclusion probabilities in `[0, 1]`.
- For **text/paragraph** questions the distribution is an answer **pool**
  (sample text → weight; weights are normalized automatically).
- `__other__` selects the form's "Other" option and submits `other_text`.

**Determinism:** same config + seed ⇒ identical responses. `exact` mode turns
proportions into integer quotas (largest-remainder method) so realized
marginals match targets; `probabilistic` mode samples instead. Submission is
rate-limited with jitter (default 20/min) and writes an audit CSV to
`~/.gform/logs/`.

### Typical conversation flow

create_form → add questions → `get_form` (grab `responder_uri`) →
`import_form_schema` → `preview_fill` → `fill_form(dry_run=true)` →
`fill_form(i_own_this_form=true)` → `get_form_responses`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `GFORM_HOME` | `~/.gform` | Base directory (token, logs) |
| `GFORM_CREDENTIALS` | `$GFORM_HOME/credentials.json` | OAuth client secrets |
| `GFORM_TOKEN` | `$GFORM_HOME/token.json` | Cached authorized-user token |

## Tests

```powershell
pytest
```

All tests run offline: the importer is tested against a captured HTML fixture,
submission with the network mocked, and the official API wrappers against a
recording fake service.
