# memory-wiki

Hermes memory provider that replaces the built-in MEMORY.md/USER.md with a
plaintext wiki. Markdown is authoritative; JSON, JSONL, QMD, and dashboard
state are derived or advisory.

This combines active memory and wiki curation in one Hermes provider. OpenClaw's
bundled `memory-wiki` has a different boundary: its active memory backend owns
recall, promotion, and dreaming, while the wiki plugin compiles entities,
claims, digests, and dashboards. See
[`OPENCLAW_PARITY.md`](../docs/OPENCLAW_PARITY.md) for the capability map.

- **Day-to-day memory**: `profile/user.md` (who the user is) and
  `profile/agent.md` (the agent's notes) are living pages, injected into the
  system prompt under char budgets. `journal/YYYY-MM-DD.md` holds timestamped
  notes and one episode per session.
- **Knowledge base**: typed concepts, claims, sources, atlases, syntheses,
  entities, refs, tastes, and generated reports, following the wiki's own
  `_meta/style-guide.md` and validated by `_scripts/check.py`.
- **Private archive**: `raw/` holds edited conversation transcripts and other
  archival evidence. It is searchable through `corpus=raw` or `corpus=all`,
  but excluded from the default curated `pages` corpus and automatic prefetch
  so unreviewed dialogue does not enter prompts or outrank durable knowledge.
- **Review-first writes**: automation never silently edits human-authored
  knowledge pages. Auto-captured facts and proposed page changes are staged in
  `_inbox/`, and reach pages only through approval (`wiki_apply approve`,
  `hermes memory-wiki review`, or your editor).
- **Derived health**: a deterministic compiler produces machine digests and
  Markdown reports. A read-only Hermes dashboard presents compiler, dream,
  queue, scheduler, and QMD state.

## Install

```bash
ln -s /Users/admin/src/memory-wiki/memory_wiki "$HERMES_HOME/plugins/memory-wiki"
```

In `$HERMES_HOME/config.yaml`:

```yaml
memory:
  provider: memory-wiki
  memory_enabled: false # built-in MEMORY.md off (this replaces it)
  user_profile_enabled: false # built-in USER.md off
plugins:
  enabled:
    - memory-wiki # required for the dashboard tab and API
  memory-wiki:
    wiki_root: ~/wiki
```

Then scaffold the wiki (idempotent; prints diffs before patching the wiki's
check.py and style guide):

```bash
hermes memory-wiki init          # add --dry-run to preview
```

The provider is exposed through Hermes's built-in `memory` toolset. Enable that
toolset on every platform that should recall from the wiki, including `cli` for
Hermes Desktop sessions. See `desktop/README.md` for the native read-only pane.

## Curation skill

The plugin owns a companion `memory-wiki` skill under
`memory_wiki/skills/research/memory-wiki/`. Expose that directory through
Hermes's external-skill mechanism so the skill appears in the normal skill
index and can be loaded automatically for wiki work:

```yaml
skills:
  external_dirs:
    - /path/to/memory-wiki/memory_wiki/skills
  disabled:
    - llm-wiki
```

The stock `llm-wiki` skill assumes a different repository architecture in
which `raw/`, `SCHEMA.md`, and `log.md` govern the whole knowledge base. This
plugin's optional `raw/` directory is only a private evidence archive; the
review-first typed pages remain canonical knowledge. Disable the stock skill
for profiles that use this plugin to avoid conflicting curation procedures.
Restart the session or gateway after changing skill configuration.

## Tools (model-facing)

- `wiki_search(query, corpus, type, granularity, as_of, include_historical,
  limit)`: cited recall through QMD or local search. `granularity` accepts
  `auto`, `page`, `claim`, `evidence`, or `heading`. `corpus=raw` searches only
  private archival records. Local semantic search blends 0.7 embedding
  similarity and 0.3 lexical tf-idf. Explicit `raw` or `all` search may return
  non-public material; automatic prefetch never does.
- `wiki_get(ref)`: read a page, candidate id, or bounded read model including
  `status`, `review`, `review-queue`, `maintenance`, `semantic-maintenance`,
  `history`, `context-pack`, `dream`, and `decisions`. It also resolves the
  stable `#claim:`, `#evidence:`, and `#heading:` refs returned by search.
- `wiki_apply(action, ...)`: `write_profile` and `append_journal` write directly
  (day-to-day memory); `stage` proposes knowledge; `approve` (promotes by
  default; re-approve to promote later), `reject` manage the queue; `supersede`
  supersedes an outdated promoted memory (id = the old candidate id from the
  bullet's marker; profile/journal targets replace immediately, knowledge
  pages stay review-first and structured claims retain lineage). `dream`
  returns a scored curation plan and, only
  with `apply_safe=true`, promotes recall-qualified profile/agent bullets.
  Direct writes accept an optional `idempotency_key` for safe tool retries.

Per-turn recall is injected automatically (prefetch); the profile pages ride in
the system prompt. Pending candidates surface in recall with their staged age
and drop out after `candidate_stale_days`; when any are waiting, the prompt
block carries a review nudge and CLI sessions get one status line at start.
Automatic prefetch excludes `raw/` and any page whose page or claim evidence
uses a non-public `privacyTier`. Access to those records requires an explicit
`wiki_search` corpus request.

### Retrieval units and traces

`auto` recall ranks claim, evidence, heading, and page units, then keeps the
best unit per page for diversity. Stable refs let callers open exactly the
matched unit. Every result includes a bounded `retrieval_trace` with `backend`,
`unit_backend`, `unit_kind`, `matched_terms`, `query_term_coverage`, and
`ranking_score`, plus applicable candidate-backend, semantic-readiness, and
temporal fields. QMD may nominate pages, but local unit scoring and visibility
rules remain authoritative.

Claims are evaluated at `as_of` (now by default). Future, expired, and
superseded units are excluded unless `include_historical=true`; invalid units
are always excluded. Claims without temporal bounds remain recallable with
state `unknown`.

## Auto-capture

`sync_turn` runs deterministic sentence heuristics (preference, identity fact,
decision, correction) over each user turn; matches are staged as `pending`
candidates, capped per turn and per session. `on_pre_compress` catches
unprocessed turns before compaction. Session end appends an idempotent journal
episode. Nothing auto-caught reaches a knowledge page without review. An
explicit dream `--apply-safe` may promote only recall-qualified, grounded
profile bullets under the narrow policy described below.

Optional: `llm_extraction: true` adds one structured PluginLlm call per turn
(`purpose: memory-wiki.extract`); failures degrade silently to
deterministic-only.

## Recall backends

`search_backend: local` uses the built-in lexical or FastEmbed path.
`search_backend: qmd` uses an isolated QMD collection and falls back to local
search after command failures or timeouts. QMD state lives outside the wiki
under `~/.cache/memory-wiki/qmd/` by default, so Markdown remains the source of
truth. Collection updates run every five minutes. `query` and `vsearch` modes
also refresh embeddings hourly. Read-only sessions can query existing state but
never initialize or update it.

```yaml
plugins:
  memory-wiki:
    search_backend: qmd
    qmd_search_mode: search # search, vsearch, or query
```

`search` is BM25-only. `query` adds expansion, vectors, and reranking, but its
first run may download large local models. Increase `qmd_timeout_seconds` for
that mode on slower machines. `wiki_get status` and `hermes memory-wiki status`
expose QMD availability and state.

QMD is only a retrieval sidecar. It ranks and excerpts Markdown, including
generated report pages, while the in-memory index enforces corpus, type, and
candidate-state visibility. It never becomes the claim or promotion source of
truth. The compiler does not depend on QMD.

### Local semantic recall

Embeddings are optional and local: `fastembed` (ONNX, `BAAI/bge-small-en-v1.5`
by default) must be installed in the Hermes venv. The model loads on a
background thread and vectors live in a throwaway sqlite cache under
`~/.cache/memory-wiki/`, keyed by page path and full canonical content. Markdown
remains the only source of truth; the cache rebuilds itself if deleted or if the
model changes. While loading, if the import fails, or in read-only contexts
(subagent/cron), recall degrades to lexical-only. `wiki_get status` reports the
state as `embedding_state`.

## CLI

```
hermes memory-wiki init [--wiki-root PATH] [--dry-run]
hermes memory-wiki status
hermes memory-wiki compile
hermes memory-wiki review [--list] [--kind KIND] [--confidence-filter LEVEL]
                          [--target-filter PATH | --effective-target PATH]
                          [--stale-only] [--min-age DAYS] [--limit N]
hermes memory-wiki review [--approve ID | --reject ID --reason R]
hermes memory-wiki review FILTERS
                          [--approve-filtered | --reject-filtered] --yes
hermes memory-wiki review --prune [--days N]
hermes memory-wiki dream [--phase light|rem|deep|all] [--limit N]
                         [--apply-safe] [--report] [--scheduled]
                         [--compile] [--quiet]
hermes memory-wiki dream --explain ID
hermes memory-wiki schedule [--enable | --disable] [--cron EXPRESSION]
hermes memory-wiki maintain [--limit N] [--json] [--include-unrelated]
hermes memory-wiki maintain --accept FINDING_ID --yes
hermes memory-wiki eval --queries PATH [--backend local|qmd|all]
                        [--baseline PATH] [--json] [--fail-on-regression]
hermes memory-wiki migrate [--plan | --apply] [--wiki-root PATH]
hermes memory-wiki lint
```

### Retrieval evaluation

`eval` runs a bounded JSONL query set against the real wiki. Each line names a
query and one or more acceptable page refs. Expected targets may also identify
a claim or a future derived unit:

```json
{"id":"atomic-notes","query":"atomic notes","expected":[{"ref":"concepts/zettelkasten.md","claim_id":"claim.notes.atomic"}]}
```

The report includes recall@1, recall@3, recall@5, MRR, zero-hit rate, and
latency. `local` uses the deterministic lexical scorer. `qmd` uses only
pre-existing read-only QMD state and reports unavailable state as an error,
without relabelling local fallback. Save `--json` output and pass it later with
`--baseline`; comparisons require the same query-set hash. Reports retain case
IDs and result refs but omit query text.

The review list, interactive reviewer, and `wiki_get review-queue` share the
same candidate metadata, routing, staleness, filters, and summary. Filtered
batch mutations require at least one filter plus `--yes`.

`maintain` compares page and claim retrieval units locally. Findings use the
exact classifications `duplicate`, `supports`, `refines`, `contradicts`,
`temporal_successor`, and `unrelated`. Listing and
`wiki_get semantic-maintenance` are read-only. `--accept ID --yes` regenerates
the finding against current content and stages one pending candidate. It never
approves, promotes, or edits a knowledge page.

The bounded read models are deliberately non-mutating: `review` prioritizes
queue and dream actions; `maintenance` summarizes existing compiler-derived
integrity work without compiling; `history` sanitizes and filters the candidate
lifecycle audit by action, target, session, or time; and `context-pack`
assembles task-scoped cited context within a character budget. Context packs
use an explicit primary corpus and optional profile, journal, or candidate
layers; they do not expose recall telemetry or accept the broad `all` corpus.

## Config reference (`plugins.memory-wiki`)

| key                               | default                  | meaning                                                    |
| --------------------------------- | ------------------------ | ---------------------------------------------------------- |
| `wiki_root`                       | `~/wiki`                 | wiki repository root                                       |
| `auto_capture`                    | `true`                   | deterministic per-turn extraction                          |
| `llm_extraction`                  | `false`                  | PluginLlm structured extraction                            |
| `search_backend`                  | `local`                  | `local`, `qmd`, or fail-soft `auto` recall                 |
| `embeddings`                      | `true`                   | hybrid semantic recall (needs fastembed)                   |
| `embedding_model`                 | `BAAI/bge-small-en-v1.5` | fastembed model name                                       |
| `qmd_command`                     | `qmd`                    | QMD executable or absolute path                            |
| `qmd_search_mode`                 | `search`                 | `search`, `vsearch`, or `query`                            |
| `qmd_timeout_seconds`             | `8`                      | QMD search timeout                                         |
| `qmd_maintenance_timeout_seconds` | `120`                    | QMD add, update, and embed timeout                         |
| `qmd_update_interval_seconds`     | `300`                    | Markdown index refresh interval                            |
| `qmd_embed_interval_seconds`      | `3600`                   | semantic embedding refresh interval                        |
| `qmd_rerank`                      | `true`                   | rerank `query` results                                     |
| `qmd_state_dir`                   | empty                    | private QMD state override                                 |
| `auto_compile`                    | `true`                   | refresh compiler artifacts after wiki mutations            |
| `journal_episodes`                | `true`                   | session episode at session end                             |
| `prefetch_limit`                  | `5`                      | max recall results per turn                                |
| `prefetch_char_budget`            | `1200`                   | recall block cap (chars)                                   |
| `profile_user_budget`             | `6000`                   | profile/user.md chars in prompt                            |
| `profile_agent_budget`            | `4000`                   | profile/agent.md chars in prompt                           |
| `prompt_block_budget`             | `9000`                   | whole prompt block cap                                     |
| `max_candidates_per_turn`         | `3`                      | staging cap per turn                                       |
| `max_candidates_per_session`      | `40`                     | staging cap per session                                    |
| `candidate_stale_days`            | `30`                     | pending age after which recall stops surfacing a candidate |
| `dream_limit`                     | `50`                     | max pending candidates inspected by a dream pass           |
| `dream_min_score`                 | `0.75`                   | minimum deep-promotion score                               |
| `dream_min_recall_count`          | `3`                      | minimum organic recalls                                    |
| `dream_min_unique_queries`        | `2`                      | minimum distinct hashed queries                            |
| `dream_recency_half_life_days`    | `14`                     | recall freshness half-life                                 |
| `dream_schedule_enabled`          | `false`                  | reconcile one managed Hermes dream job                     |
| `dream_schedule`                  | `0 3 * * *`              | managed job cron expression                                |
| `backup_include_wiki`             | `true`                   | include wiki_root in `hermes backup`                       |

## Structured claims and compiler

Typed pages use `createdAt: YYYY-MM-DD` for new creation dates. Legacy
`created` remains readable. `hermes memory-wiki init` updates the wiki checker
to accept that fallback without changing existing pages. Pages may carry a
JSON-compatible `claims` list in frontmatter. `text` is required. `id`,
`status`, numeric `confidence` from 0 to 1, `updatedAt`, `validFrom`,
`validUntil`, `supersedes`, and `evidence` are optional. Claim IDs, when
present, use
`claim.<namespace>.<local-name>`, with kebab-case inside each component.
Missing status normalizes to `supported`. Evidence may record `kind`,
`sourceId`, `path`, `lines`, `weight`, `confidence`, `privacyTier`, `note`,
`updatedAt`, `observedAt`, `excerpt`, and `excerptHash`. An excerpt hash is
`sha256:` plus the lowercase digest of the exact excerpt and is validated on
read. `validUntil` is exclusive.

```yaml
claims: [{"id":"claim.deploy.rollback-required","text":"Rollback is required before retrying a failed deploy.","status":"supported","confidence":0.9,"evidence":[{"kind":"source","sourceId":"runbook.deploy","path":"sources/deploy-runbook.md","lines":"18-24","updatedAt":"2026-07-14"}],"updatedAt":"2026-07-14"}]
```

Legacy claim pages remain valid. The compiler derives one claim from the page
title and `## Evidence` when no structured claims exist.

Promoting into `## Evidence` updates both Markdown and structured provenance.
For a page with multiple structured claims, target one explicitly with
`--field claim_id=CLAIM_ID`. Knowledge supersession retains the prior
structured claim as `status: superseded` with `validUntil`, then creates a
versioned successor with `validFrom` and `supersedes`; it does not merely edit
the prose marker. Duplicate claim IDs are reported as ambiguous by `wiki_get`.

`hermes memory-wiki compile` writes only changed files while holding the wiki
lock:

- `.memory-wiki/cache/agent-digest.json`
- `.memory-wiki/cache/claims.jsonl`
- `.memory-wiki/cache/entities.jsonl`
- `.memory-wiki/cache/relationships.jsonl`
- `reports/open-questions.md`
- `reports/contradictions.md`
- `reports/low-confidence.md`
- `reports/claim-health.md`
- `reports/stale-pages.md`
- `reports/provenance-coverage.md`
- `reports/privacy-review.md`
- `reports/temporal-integrity.md`
- `reports/entity-relationships.md`
- `reports/entity-migration-candidates.md`

Report managed blocks are replaceable. Human-authored blocks are preserved.
Freshness is fresh below 30 days, aging at 30 to 89 days, stale at 90 days, and
unknown without a usable timestamp. A repeated claim ID forms a contradiction
cluster only when its normalized text or status differs. `auto_compile` keeps
artifacts current after plugin-owned mutations.

## Schema versions and entities

`memory_wiki/page-types.json` is the versioned type registry. A migrated wiki
records its version in `.memory-wiki/schema.json`; a newer manifest is rejected
instead of guessed at. `hermes memory-wiki migrate --plan` is read-only.
`--apply` creates managed directories, updates validator/style-guide blocks,
defaults legacy raw pages without a tier to `private`, and writes the manifest.
It stops when a managed patch anchor needs manual repair.

`entities/` holds named actors with stable `entity.*` IDs and an `entityKind`.
Relationships are objects with a one-line `predicate` and a `targetId` that is
also an `entity.*` ID. Compilation reports duplicate IDs, invalid, orphaned,
and ambiguous relationships. Existing person or organization dossiers under
`refs/` are only reported as migration candidates; nothing moves them
automatically.

## Dreaming and curation

`hermes memory-wiki dream` defaults to the conservative deep planning pass and
does not write. `--phase all` runs one serialized light, REM, deep sweep:

- Light ranks and deduplicates pending candidates from organic recall evidence.
- REM derives recurring themes and a bounded set of lasting-truth suggestions.
- Deep groups overlaps, flags covered or stale items, suggests targets, scores
  promotion, and remains the only phase allowed to write via `--apply-safe`.

Light and REM reinforcement lives in bounded `.memory-wiki/dream-state.json`.
It is bound to exact candidate content and changed recall evidence, decays with
a 14-day half-life, and contributes at most `0.06` plus `0.09` to the deep
score. `DREAMS.md` is report output only and is never read as dream input.
Corrections overlapping existing memory get `supersede_existing`, not an
automatic rejection, because token overlap cannot establish contradiction.

`--apply-safe` is deliberately narrow: it only promotes pending high-confidence
`preference` or `note` candidates whose default target is `profile/user.md` or
`profile/agent.md`, whose content overlaps exact quoted user evidence from a
deterministic capture hook, and which have no duplicate/similarity/quality
warnings. Tool and LLM proposals, corrections, claims, concepts, refs, tastes,
sources, journal decisions, lower-confidence facts, retargets, and
consolidation groups remain review-first.

The promotion score mirrors OpenClaw's six base signals: relevance 0.30,
frequency 0.24, query diversity 0.15, recency 0.15, consolidation 0.10, and
conceptual richness 0.06. Score, recall-count, and unique-query gates must all
pass. Candidate recalls are kept in bounded machine state at
`_inbox/short-term-recall.json`; raw queries and session ids are never stored.
Recall evidence is bound to the exact candidate content hash. The planner
rechecks candidate state, content hash, and safe routing while holding the write
lock. `--explain ID` shows the gates. `--report` appends a human-readable
`DREAMS.md` entry, which is never a promotion input.

Manual gates use the configured defaults: score `0.75`, three recalls, and two
distinct query hashes. `--scheduled` uses fixed stricter gates: `0.80`, three,
and three. The distinction prevents a relaxed interactive config from silently
weakening unattended promotion.

## Managed schedule

`hermes memory-wiki schedule --enable` reconciles one Hermes no-agent cron job,
disabled by default and scheduled for `0 3 * * *`. `--cron EXPRESSION` changes
the schedule and accepts recurring cron expressions only. With no mutation
flag, the command reports current state.
`--disable` removes the managed job and script, including obsolete per-phase
jobs.

Each run performs ordered light, REM, and deep phases with safe apply, scheduled
gates, compilation, and quiet success output. There are never three independent
phase jobs that could overlap.

## Dashboard and Desktop

The plugin contributes a native Hermes dashboard tab at `/memory-wiki`, after
Analytics. It is a read-only overview of claim health, compiler artifacts,
reports, dream phases, managed scheduling, pending candidates, and QMD state.
The bundled UI also has a standalone legacy fallback. Both read the native
dashboard plugin API and never mutate the wiki. Restart or rescan the Hermes
dashboard after first enabling the extension.

Hermes Desktop can additionally load `desktop/plugin.js` as a native,
read-only overview pane. Its chat sessions use the same provider through the
`cli` platform toolset, so the pane and model-facing memory must both be
configured. Restart Desktop after changing plugin, skill, or toolset config.

## Safety and retry behavior

Automatic capture, model extraction, session excerpts, staging, and direct
writes reject obvious credentials and private keys. Turn and session caps apply
across deterministic and LLM extraction together. Direct profile and journal
writes carry hidden retry markers, so repeated tool deliveries do not duplicate
content. Reusing an explicit idempotency key with different content is an error.
Any successful mutation refreshes compiler output before the active search
backend, then clears prefetch state. This makes generated reports immediately
searchable and invalidates QMD maintenance state.

## Concurrency model

Candidate files are created `O_EXCL` with unique ids (no contention); page
writes are atomic tmp+rename; read-modify-write of shared files (profile,
journal, promotion targets, decisions.jsonl) serializes on one flock
(`_inbox/.lock`, 5s timeout). Profile edits are last-writer-wins. Not handled:
multi-host git sync (git is the recovery story), file watching, NFS, Windows.

## Development

```bash
uv run --group dev pytest        # tests live in ../tests
```

Repo layout: this directory is the plugin (symlink target); tests and tooling
live one level up.
