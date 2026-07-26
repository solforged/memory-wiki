# Operations, safety, and troubleshooting

[Back to documentation index](../README.md)

This guide covers write integrity, recovery, privacy boundaries, current limits,
and common operational failures.

## Integrity and recovery

### Write safety

- Shared read-modify-write operations serialize on `_inbox/.lock` using
  `flock`, with a five-second timeout.
- Candidate creation uses `O_EXCL` and allocates IDs while holding the lock.
- Page and candidate rewrites fsync a temporary file, then atomically replace
  the destination.
- JSONL audit appends handle short writes and fsync before returning.
- Typed knowledge appends and new-page promotions validate the final rendered
  page before replacement.
- Marker checks make repeated promotion idempotent.

A model-facing lock timeout returns `wiki busy, retry`. The lock is a
host-local, cross-process advisory `flock`. It is not a distributed lock.

Profile edits are last-writer-wins. There is no merge engine for a human editor
and the agent changing the same page concurrently.

### Audit log

`_inbox/decisions.jsonl` records UTC timestamp, action, candidate ID, target,
session, actor, content hash, and action-specific detail. Candidate lifecycle
events are once-only by `(action, id)`. Direct writes remain repeatable.

The audit is append-only during normal operation. Pruning candidate files does
not remove audit entries.

### Startup recovery

Writable initialization reconciles candidate files, page markers, and audit
events. Marker placement is treated as commit evidence. Recovery can:

- Repair an unterminated or torn final JSONL line.
- Complete an interrupted prune.
- Mark pending or approved candidates promoted when exactly one destination
  marker proves the write completed.
- Restore `promoted_to` targets.
- Reconstruct supersession relationships.
- Restore missing lifecycle audit entries.

When files, markers, or statuses conflict, recovery logs the case as ambiguous
and does not guess. Inspect the candidate, page marker, and audit tail manually.

If the wiki is version-controlled, Git can provide broader history and
multi-file recovery. The plugin neither requires a Git repository nor runs Git
commands itself.

## Privacy and trust

This plugin creates durable memory from conversations. Treat the wiki and its
backups as sensitive user data.

- Deterministic extraction runs locally, but its pattern matching is not a
  secrets classifier. A matching sentence can be staged verbatim.
- Candidate files include the captured content and short evidence excerpts.
- `decisions.jsonl` stores metadata and hashes, not the full candidate body.
- With auto-capture enabled and `llm_extraction: true`, up to 6,000 characters
  each of the user and assistant turn are sent through the host's configured
  `PluginLlm`. That backend may be remote. The prompt asks the model not to
  extract secrets, but this is policy guidance, not a hard
  data-loss-prevention control.
- Local embeddings cover all indexed records, including profiles, journals,
  candidates, and knowledge pages. Their vector cache lives outside the wiki.
- `wiki_get` can read any `.md` path inside the wiki when the caller knows its
  relative path, subject to a 400-line window.
- Path resolution rejects absolute paths, `~`, backslashes, and traversal
  outside the wiki root.
- Subagent, cron, and flush contexts receive only read tools.

Disable `auto_capture` or `llm_extraction` when the relevant trust boundary is
not appropriate. Review `_inbox/` and backup policy as part of normal operation.

## Current limits

- Single-host POSIX operation only: no multi-host sync, distributed conflict
  resolution, NFS guarantees, or Windows support.
- No filesystem watching. Profile prompt edits normally require a new session.
- Flat type folders only. Lexical indexing considers at most 20,000 body
  characters, and exact repeated prefetch queries use a small session cache.
- Search is bounded recall, not a full-text query language. Its SQLite vector
  cache is disposable and has no supported query API.
- Deterministic and LLM extraction are bounded heuristics, not authoritative
  classifiers.
- Dreaming is manually invoked, never synthesizes knowledge pages, and checks
  the broader `profile/` prefix for explicitly targeted safe candidates.
- Stale candidates are not auto-rejected. Pruning omits superseded candidates.
- `hermes memory-wiki init` depends on an exact validator anchor and ignores
  the validator's exit code. `hermes memory-wiki lint` propagates it.
- Real Hermes contract tests skip without a Hermes source checkout, using a
  minimal provider stub for the remaining unit suite.

## Troubleshooting

### The provider is not active

1. Run `hermes memory status`.
2. Confirm the plugin link resolves to this repository's `memory_wiki/` folder.
3. Confirm `memory.provider: memory-wiki` in the active config file.
4. Check whether the config path is itself a symlink.
5. Restart all running Hermes frontends after config changes.

### The wiki root is missing

CLI `init` requires the configured root to exist. A writable provider startup
can create only the runtime folders at a missing path, so a mistyped root may
look minimally initialized. Correct `plugins.memory-wiki.wiki_root`, inspect any
accidental tree, then rerun `init --dry-run` against the intended wiki.

### The validator patch anchor is missing

The live `_scripts/check.py` differs from the contract this version expects.
Review the manual diff printed by `init --dry-run`, update the validator
deliberately, and run `hermes memory-wiki lint`. Do not treat the plugin's schema
mirror as a replacement for the live validator.

### Embeddings show `loading` or `failed`

1. Read `wiki_get status` and inspect `embedding_state`.
2. Confirm `fastembed` is installed in the Hermes runtime environment.
3. Confirm the configured model is available to that runtime.
4. Take the exact expanded `wiki_root` from `wiki_get status`, compute its first
   eight SHA-1 characters, and inspect that exact cache path:

   ```bash
   WIKI_ROOT_FOR_CACHE='/absolute/path/from/wiki_get-status'
   CACHE_TAG="$(printf %s "$WIKI_ROOT_FOR_CACHE" | shasum -a 1 | cut -c1-8)"
   ls "$HOME/.cache/memory-wiki/embeddings-$CACHE_TAG.db"
   ```

5. If that cache is corrupt, stop Hermes and remove only the exact file you
   identified. Do not delete every wiki's cache with a glob.

Recall remains lexical while embeddings are unavailable.

### A pending candidate no longer appears in recall

It may be older than `candidate_stale_days`, or the exact query may have a
cached prefetch result. It remains available through
`hermes memory-wiki review --list` and `wiki_get review-queue`. Review it,
change the query, or start a new session.

### A manual profile edit is absent from the prompt

The system-prompt block is frozen for prefix-cache stability. Start a new
session. Plugin-owned profile writes recompile the block automatically.

### Writes report `wiki busy, retry`

Another process held `_inbox/.lock` for more than five seconds. Retry after the
other writer completes. Do not delete a lock file to break an active `flock`.

### Recovery reports ambiguity

Compare:

- The candidate's status and `promoted_to` fields.
- Marker comments in possible destination pages.
- The related lines in `_inbox/decisions.jsonl`.

Recovery intentionally refuses to choose between conflicting evidence.

### Lint reports an orphan marker

The corresponding candidate file was pruned, deleted, or moved. This is a
warning because the page content and audit may still be valid. Preserve the
marker unless you have deliberately removed its provenance.

## Related guides

- [Setup and activation](setup.md)
- [Architecture and runtime](architecture.md)
- [Review and curation](review-and-curation.md)
- [Tool, CLI, and configuration reference](reference.md)
- [Development](development.md)
