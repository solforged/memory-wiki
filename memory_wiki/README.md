# memory-wiki

Hermes memory provider that replaces the built-in MEMORY.md/USER.md
with a plaintext wiki. No database: markdown files, one advisory lock,
one JSONL audit log.

- **Day-to-day memory**: `profile/user.md` (who the user is) and
  `profile/agent.md` (the agent's notes) are living pages, injected
  into the system prompt under char budgets. `journal/YYYY-MM-DD.md`
  holds timestamped notes and one episode per session.
- **Knowledge base**: the wiki's six types (concepts, claims, sources,
  atlases, syntheses, refs) following the wiki's own `_meta/style-guide.md`
  and validated by its `_scripts/check.py`.
- **Review-first writes**: automation never edits knowledge pages.
  Auto-captured facts and proposed page changes are staged as one-file
  candidates in `_inbox/`, and reach pages only through approval
  (`wiki_apply approve`, `hermes memory-wiki review`, or your editor).

## Install

```bash
ln -s /Users/admin/src/memory-wiki/memory_wiki "$HERMES_HOME/plugins/memory-wiki"
```

In `$HERMES_HOME/config.yaml`:

```yaml
memory:
  provider: memory-wiki
  memory_enabled: false        # built-in MEMORY.md off (this replaces it)
  user_profile_enabled: false  # built-in USER.md off
plugins:
  memory-wiki:
    wiki_root: ~/wiki
```

Then scaffold the wiki (idempotent; prints diffs before patching the
wiki's check.py and style guide):

```bash
hermes memory-wiki init          # add --dry-run to preview
```

## Tools (model-facing)

- `wiki_search(query, corpus, type, limit)`: cited hybrid recall
  (0.7 embedding similarity + 0.3 lexical tf-idf, OpenClaw's split).
- `wiki_get(ref)`: read a page, a candidate id, or `status` /
  `review-queue` / `dream` / `decisions`.
- `wiki_apply(action, ...)`: `write_profile` and `append_journal` write
  directly (day-to-day memory); `stage` proposes knowledge; `approve`
  (promotes by default; re-approve to promote later), `reject` manage
  the queue;
  `supersede` replaces an outdated promoted memory in place (id = the
  old candidate id from the bullet's marker; profile/journal targets
  replace immediately, knowledge pages stay review-first). `dream`
  returns a curation plan and, only with `apply_safe=true`, promotes
  conservative high-confidence profile/agent bullets.

Per-turn recall is injected automatically (prefetch); the profile pages
ride in the system prompt. Pending candidates surface in recall with
their staged age and drop out after `candidate_stale_days`; when any
are waiting, the prompt block carries a review nudge and CLI sessions
get one status line at start.

## Auto-capture

`sync_turn` runs deterministic sentence heuristics (preference,
identity fact, decision, correction) over each user turn; matches are
staged as `pending` candidates, capped per turn and per session.
`on_pre_compress` catches unprocessed turns before compaction.
Session end appends an idempotent journal episode. Nothing auto-caught
becomes a page without review.

Optional: `llm_extraction: true` adds one structured PluginLlm call per
turn (`purpose: memory-wiki.extract`); failures degrade silently to
deterministic-only.

## Semantic recall

Embeddings are optional and local: `fastembed` (ONNX,
`BAAI/bge-small-en-v1.5` by default) must be installed in the Hermes
venv. The model loads on a background thread and vectors live in a
throwaway sqlite cache under `~/.cache/memory-wiki/` keyed by content
hash; the markdown remains the only source of truth, and the cache
rebuilds itself if deleted or if the model changes. While the model is
loading, if the import fails, or in read-only contexts
(subagent/cron), recall degrades to lexical-only. `wiki_get status`
reports the state as `embedding_state`.

## CLI

```
hermes memory-wiki init [--wiki-root PATH] [--dry-run]
hermes memory-wiki status
hermes memory-wiki review [--list | --approve ID | --reject ID --reason R |
                           --prune --days N]
hermes memory-wiki dream [--limit N] [--apply-safe]
hermes memory-wiki lint
```

## Config reference (`plugins.memory-wiki`)

| key | default | meaning |
|---|---|---|
| `wiki_root` | `~/wiki` | wiki repository root |
| `auto_capture` | `true` | deterministic per-turn extraction |
| `llm_extraction` | `false` | PluginLlm structured extraction |
| `embeddings` | `true` | hybrid semantic recall (needs fastembed) |
| `embedding_model` | `BAAI/bge-small-en-v1.5` | fastembed model name |
| `journal_episodes` | `true` | session episode at session end |
| `prefetch_limit` | `5` | max recall results per turn |
| `prefetch_char_budget` | `1200` | recall block cap (chars) |
| `profile_user_budget` | `6000` | profile/user.md chars in prompt |
| `profile_agent_budget` | `4000` | profile/agent.md chars in prompt |
| `prompt_block_budget` | `9000` | whole prompt block cap |
| `max_candidates_per_turn` | `3` | staging cap per turn |
| `max_candidates_per_session` | `40` | staging cap per session |
| `candidate_stale_days` | `30` | pending age after which recall stops surfacing a candidate |
| `dream_limit` | `50` | max pending candidates inspected by a dream pass |
| `backup_include_wiki` | `true` | include wiki_root in `hermes backup` |

## Dreaming / curation

`hermes memory-wiki dream` is the conservative consolidation pass. It
does not write by default. It groups similar pending candidates, flags
items already covered by existing pages (`reject_covered`) and
candidates pending longer than `candidate_stale_days` (`reject_stale`),
suggests better targets, and separates knowledge-page candidates from
direct profile/agent memories. Corrections that overlap an existing
memory get `supersede_existing`, never a reject suggestion: token
overlap cannot tell confirmation from contradiction. The output is a
review plan with exact follow-up commands where useful.

`--apply-safe` is deliberately narrow: it only promotes pending
high-confidence `preference` or `note` candidates whose default target
is `profile/user.md` or `profile/agent.md`, with no
duplicate/similarity/quality warnings. Corrections, claims, concepts,
refs, tastes, sources, journal decisions, lower-confidence facts,
retargets, and consolidation groups remain review-first.

## Concurrency model

Candidate files are created `O_EXCL` with unique ids (no contention);
page writes are atomic tmp+rename; read-modify-write of shared files
(profile, journal, promotion targets, decisions.jsonl) serializes on
one flock (`_inbox/.lock`, 5s timeout). Profile edits are
last-writer-wins. Not handled: multi-host git sync (git is the recovery
story), file watching, NFS, Windows.

## Development

```bash
uv run --group dev pytest        # tests live in ../tests
```

Repo layout: this directory is the plugin (symlink target); tests and
tooling live one level up.
