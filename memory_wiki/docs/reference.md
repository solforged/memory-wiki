# Tool, CLI, and configuration reference

This guide collects the model-facing tools, operator commands, and configuration
surface for the current `memory-wiki` implementation.

[Back to documentation index](../README.md)

## Model-facing tools

All tool handlers return a JSON string with `ok: true` or `ok: false`. Input,
path, lock, and runtime errors become structured tool errors rather than agent
loop exceptions.

### `wiki_search`

Search the indexed wiki and return cited results.

| Argument | Default | Current behavior |
| --- | --- | --- |
| `query` | required | Non-empty search text. |
| `corpus` | `pages` | `pages`, `profile`, `journal`, `candidates`, or `all`. |
| `type` | none | Restrict typed pages to one knowledge type. |
| `limit` | `5` | Clamped to a maximum of `20`. |

`pages` means the seven typed knowledge kinds. Candidate search includes active
pending candidates. Results contain `ref`, `title`, `type`, score, and a
160-character query-local snippet. Claims also include confidence and maturity.
Candidates include ID, status, and kind.

### `wiki_get`

Read a safe repo-relative `.md` path, a candidate ID, or a special reference:

| `ref` | Result |
| --- | --- |
| `status` | Provider health, counts, prompt size, config subset, and `embedding_state`. |
| `review-queue` | Shared pending-candidate read model with filters and summary. |
| `dream` | Read-only curation plan. |
| `decisions` | Last 20 raw JSONL audit entries. |

Page reads accept `start_line` and `max_lines`. A single call returns at most
400 lines.

`review-queue` accepts `kind`, `confidence`, `target`, `effective_target`,
`stale_only`, `min_age`, and `limit`. Kind, confidence, and target filters are
arrays in the model-facing schema.

### `wiki_apply`

Writable contexts expose these actions:

| Action | Current behavior |
| --- | --- |
| `write_profile` | Append to or replace a section in `profile/user.md` or `profile/agent.md`. |
| `append_journal` | Add a timestamped bullet under today's `## Notes`. |
| `stage` | Create a pending candidate after validation and dedup. |
| `approve` | Apply optional review overrides, then promote by default. |
| `reject` | Reject a candidate with an optional reason. |
| `supersede` | Stage a [replacement](review-and-curation.md#supersession). Profile and journal replacements promote immediately; knowledge replacements remain pending. |
| `dream` | Return a curation plan and optionally apply safe profile promotions. |

Important defaults:

- `write_profile`: page `user`, mode `append`, section `## Notes`.
- `stage`: kind `fact`, confidence `plausible`, no explicit target.
- `approve`: `and_promote: true`.
- `dream`: no writes unless `apply_safe: true`.

Profile writes recompile the session prompt block. Journal and knowledge writes
become visible through later index refreshes, subject to the prefetch query
cache documented in [Architecture and runtime](architecture.md).

### Tool call examples

These illustrative responses are abbreviated:

```text
call: wiki_search
arguments: {"query":"review-first memory","corpus":"all","limit":3}
result: {"ok":true,"count":3,"results":[{"ref":"concepts/memory.md",...}]}

call: wiki_apply
arguments: {"action":"stage","kind":"claim","content":"Reviewed memory is safer than silent promotion."}
result: {"ok":true,"action":"stage","id":"20260726-190501-019f4a37-01","note":"pending human review"}

call: wiki_apply
arguments: {"action":"approve","id":"20260726-190501-019f4a37-01"}
result: {"ok":true,"action":"approve","result":"promoted","target":"claims/reviewed-memory-is-safer-than-silent-promotion.md"}
```

## Operator CLI

```text
hermes memory-wiki init [--wiki-root PATH] [--dry-run]
hermes memory-wiki status

hermes memory-wiki review [--list]
                          [--kind KIND]
                          [--confidence-filter LEVEL]
                          [--target-filter PATH]
                          [--effective-target PATH]
                          [--stale-only]
                          [--min-age DAYS]
                          [--limit N]

hermes memory-wiki review [--approve ID [--approve ID ...]]
                          [--reject ID [--reject ID ...]]
                          [--reason TEXT]

hermes memory-wiki review --approve ID
                          [--content TEXT]
                          [--target PATH]
                          [--section '## Heading']
                          [--confidence LEVEL]
                          [--topic TOPIC [--topic TOPIC ...]]
                          [--field KEY=VALUE [--field KEY=VALUE ...]]

hermes memory-wiki review FILTERS --approve-filtered --yes
hermes memory-wiki review FILTERS --reject-filtered --yes [--reason TEXT]
hermes memory-wiki review --prune [--days N]

hermes memory-wiki dream [--limit N] [--apply-safe]
hermes memory-wiki lint
```

Repeatable flags can select multiple kinds, confidence values, targets,
approval IDs, or rejection IDs.

### Common review workflows

List pending candidates:

```bash
hermes memory-wiki review --list
```

Run the interactive reviewer:

```bash
hermes memory-wiki review
```

Its controls are:

```text
a  approve and promote
e  edit, then approve and promote
t  retarget, then approve and promote
r  reject
s  skip
q  quit
```

`e` opens `$VISUAL`, then `$EDITOR`, using a temporary Markdown file. If neither
is configured, the CLI asks for a one-line replacement.

Approve one candidate with corrections:

```bash
hermes memory-wiki review \
  --approve CANDIDATE_ID \
  --content 'The corrected durable statement.' \
  --target claims/corrected-statement.md \
  --topic memory \
  --field stance=model
```

Override flags require exactly one `--approve` ID.

Reject one or more candidates:

```bash
hermes memory-wiki review \
  --reject ID_1 \
  --reject ID_2 \
  --reason 'transient task state'
```

Batch approval or rejection requires `--yes` and at least one filter or
`--limit`. It cannot be combined with single-ID mutation, override flags,
`--list`, or `--prune`.

```bash
hermes memory-wiki review \
  --kind preference \
  --confidence-filter confident \
  --approve-filtered \
  --yes
```

Batch execution stops at the first error. Earlier candidates remain changed;
later ones remain untouched.

Prune candidate files already marked promoted or rejected and older than 30
days by file modification time:

```bash
hermes memory-wiki review --prune --days 30
```

Pruning keeps `decisions.jsonl` and promoted page markers. It does not prune
pending, approved, or superseded candidate files.

Use `--days 1` or greater. The current CLI does not reject zero or negative
values, which can select essentially every promoted or rejected candidate file.

### Status and lint

`hermes memory-wiki status` reports the wiki root, page counts, candidate counts,
the five newest audit entries, and a config subset. Embedding worker state is
available through model-facing `wiki_get status`, not this CLI command.

`hermes memory-wiki lint`:

1. Validates every recognized page with the plugin schema.
2. Validates candidate frontmatter.
3. Warns when a page marker refers to a pruned or missing candidate.
4. Runs `_scripts/check.py` when present.
5. Returns nonzero for plugin errors or a failing external validator.

Current quirk: `init` prints the external validator result but returns success
even if that validator fails. Run `lint` when the exit status matters.

## Configuration

Options live under `plugins.memory-wiki`.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `wiki_root` | path | `~/wiki` | Intended wiki root. `~` is expanded; CLI `init` requires the path to exist. |
| `auto_capture` | bool | `true` | Enable the automatic capture pipeline, including any configured LLM augmentation. |
| `llm_extraction` | bool | `false` | Augment enabled auto-capture with one structured host LLM call per turn. |
| `embeddings` | bool | `true` | Start optional local semantic recall in writable contexts. |
| `embedding_model` | string | `BAAI/bge-small-en-v1.5` | `fastembed` model name. |
| `journal_episodes` | bool | `true` | Write one idempotent session-end journal episode. |
| `prefetch_limit` | int | `5` | Maximum recalled records in a prefetch block. |
| `prefetch_char_budget` | int | `1200` | Character budget for per-call recall. |
| `profile_user_budget` | int | `6000` | Character budget for `profile/user.md` in the prompt. |
| `profile_agent_budget` | int | `4000` | Character budget for `profile/agent.md` in the prompt. |
| `prompt_block_budget` | int | `9000` | Character budget for instructions, profiles, and status together. |
| `max_candidates_per_turn` | int | `3` | Deterministic candidates per user turn. LLM extraction has a separate cap of five. |
| `max_candidates_per_session` | int | `40` | Automatic deterministic and LLM candidates tracked per session state. Manual staging is not subject to this setting. |
| `candidate_stale_days` | int | `30` | Pending candidates older than this stop appearing in prefetch. |
| `dream_limit` | int | `50` | Pending candidates inspected by a curation pass. |
| `backup_include_wiki` | bool | `true` | Include the wiki root in `hermes backup`. |

All budgets are characters, not tokens. A candidate is stale only when its age
is strictly greater than `candidate_stale_days`; it remains visible in review.

Boolean strings accept `1`, `true`, `yes`, and `on`. Invalid integers retain
their defaults, but valid integers are not range-checked. Review nonpositive
budgets, limits, and day thresholds deliberately. Unknown keys are ignored.
Missing config, unavailable PyYAML, or read errors fail soft to defaults.

`hermes memory setup` only prompts for `wiki_root`. Advanced options must be
edited in `config.yaml`.

## Related guides

- [Setup and activation](setup.md)
- [Architecture and runtime](architecture.md)
- [Review and curation](review-and-curation.md)
- [Operations, safety, and troubleshooting](operations.md)
