# Architecture and runtime

[Back to documentation index](../README.md)

This guide describes how `memory-wiki` stores memory, initializes inside
Hermes, recalls relevant pages, and captures durable information from a
conversation.

## Storage layout

With `wiki_root: ~/wiki`, the plugin uses this shape:

```text
~/wiki/
├── profile/
│   ├── user.md
│   └── agent.md
├── journal/
│   └── YYYY-MM-DD.md
├── concepts/
├── claims/
├── sources/
├── atlases/
├── syntheses/
├── refs/
├── tastes/
├── _inbox/
│   ├── <candidate-id>-<slug>.md
│   ├── decisions.jsonl
│   └── .lock
├── _scripts/
│   └── check.py
└── _meta/
    └── style-guide.md
```

The seven typed knowledge kinds are `concept`, `claim`, `source`, `atlas`,
`synthesis`, `ref`, and `taste`. `profile` and `journal` are memory-specific
page types. `_inbox/` is infrastructure, not a note type.

The index only scans flat Markdown files directly inside these type folders and
`_inbox/`. Typed filenames must be kebab-case. Journal filenames use the one
sanctioned ISO-date exception.

The plugin parses the wiki's small YAML-compatible frontmatter subset, not
arbitrary YAML. Candidate `fields` additionally support one nested mapping.
The wiki's own `_scripts/check.py` remains authoritative, including rules the
plugin does not mirror, such as link checks.

The only persistent state outside the wiki is the optional embedding cache:

```text
~/.cache/memory-wiki/embeddings-<wiki-root-hash>.db
```

It is disposable and can be rebuilt from Markdown.

## Runtime lifecycle

When Hermes initializes the provider, it:

1. Loads `plugins.memory-wiki` config and resets per-session capture state.
2. Marks `subagent`, `cron`, and `flush` contexts read-only.
3. In writable contexts, ensures runtime files exist and runs recovery.
4. Builds an in-memory, incremental index from the wiki.
5. Compiles the static prompt block from instructions and the two profiles.
6. Emits one best-effort review status line when pending candidates exist.
7. Starts the optional embedding worker in writable contexts.

The provider exposes `wiki_search`, `wiki_get`, and, in writable contexts,
`wiki_apply`. Read-only contexts omit `wiki_apply`, do not scaffold, do not
capture or journal, and stay lexical-only.

Provider hooks catch and log their own failures. Capture, prefetch, journaling,
or tool bugs should not escape into the Hermes agent loop.

On shutdown, the provider signals the embedding worker to stop. When
`backup_include_wiki` is enabled, `hermes backup` receives the wiki root from
the provider.

## Recall

Recall has two layers.

### Static profile prompt

At session start the provider compiles:

- Memory instructions and write policy.
- The body of `profile/user.md`, capped by `profile_user_budget`.
- The body of `profile/agent.md`, capped by `profile_agent_budget`.
- Page and pending-candidate counts, plus a review nudge when needed.

HTML comments are removed. Empty seed sections are collapsed. Each profile and
the complete prompt block have independent character budgets. Truncation points
the model to `wiki_get` for the remainder.

The block is intentionally stable for prompt-prefix caching. It is recompiled
after plugin-owned profile writes, but not after an unrelated editor modifies a
profile. Start a new session to guarantee external edits enter the prompt.

### Per-call prefetch

Before each model call, Hermes can call `prefetch(query)`. The provider:

1. Refreshes the file index.
2. Requests an embedding-cache sync.
3. Searches all corpora.
4. Omits profiles because they are already in the static prompt.
5. Includes only pending, non-stale candidates and labels them unreviewed.
6. Returns at most `prefetch_limit` cited results within
   `prefetch_char_budget`.

The block tells the model to cite paths and open results with `wiki_get`.
Unreviewed candidates are visibly marked and must not be treated as facts.

Prefetch results are cached by session and raw query hash, with at most
16 entries. Repeating exactly the same query can therefore return the earlier
session result even after a file changes. A reset or rewind clears the cache.

### Lexical ranking

The in-memory index refreshes by path, modification time, and size. It tokenizes
up to 20,000 body characters per page, strips comments, and boosts matches in:

- Title: `+2.0`
- Alias: `+2.0`
- Topic: `+1.0`
- Heading: `+0.5`

Ranking is TF-IDF plus a query-term coverage bonus. Journal pages from the last
seven days receive a `1.5` recency multiplier. Ties use the relative path for
deterministic ordering.

Search is intentionally small. Its job is to surface a handful of promising
paths for the model to inspect, not to replace a general search engine.

### Semantic ranking

With embeddings ready, ranking blends:

```text
0.7 × rescaled embedding similarity + 0.3 × normalized lexical score
```

Semantic-only hits are allowed. Cosine similarity at or below `0.6` contributes
nothing, and the remaining range is rescaled to `0..1`.

`fastembed` is optional and imported on a background daemon thread. The default
model is `BAAI/bge-small-en-v1.5`. Install `fastembed` into the Python
environment that actually runs Hermes, not merely the development environment.

The cache embeds the full canonical title, topics, and body for every indexed
record. Keys include both path and content. Model changes clear old vectors.
Deleted or changed pages are synchronized on later uncached prefetch calls.

While the model loads, when `fastembed` is absent, after a model failure, or in
a read-only context, search falls back to lexical ranking. A cache-file failure
can still leave in-memory vectors usable for that process.

## Capture and journaling

### Deterministic capture

After a turn, `sync_turn` examines only the user's text and recognizes four
durable-memory shapes:

| Kind | Typical signal |
| --- | --- |
| `correction` | “Actually…”, “No…”, “I meant…” |
| `preference` | “I prefer…”, “I use…”, “I never…” |
| `decision` | “We decided…”, “Going forward…”, “Let's…” |
| `fact` | “I live…”, “I work as…”, “Call me…” |

The extractor is deliberately conservative:

- Inputs over 4,000 characters are treated as pastes and skipped.
- Fenced and inline code are removed before matching.
- Questions and sentences shorter than 15 characters are skipped.
- Sentences over 300 characters are truncated for extraction.
- Date-bound preferences and facts such as “today” or “next week” are skipped.
- Under-specified decisions require enough distinct content tokens.
- The first matching kind wins.
- Repeated normalized user content is processed once per session state.

Each deterministic candidate carries a quoted user-turn evidence line.
`max_candidates_per_turn` caps deterministic results only. Optional LLM
extraction has its own hard cap of five results and shares the session-wide
`max_candidates_per_session` cap.

### Optional LLM extraction

When `auto_capture` remains enabled, `llm_extraction: true` augments the
deterministic pipeline with one structured host `PluginLlm` call per turn. It
sends up to 6,000 characters each of the user and assistant text and requests
durable preferences, facts, decisions, corrections, claims, concepts, or notes.

The call uses:

- Purpose `memory-wiki.extract`
- Temperature `0`
- Maximum `512` output tokens
- Timeout `30` seconds
- Maximum five returned memories

Kinds and confidence values are normalized. Topics are capped at five. Fields
are allowlisted. Proposed targets must be a single Markdown file directly in a
recognized type folder. Any exception or malformed response silently degrades
to deterministic-only capture.

LLM output is still a proposal. It is staged with source excerpts and remains
pending until reviewed.

### Pre-compress rescue

`on_pre_compress` scans user messages that session state has not processed yet.
It stages deterministic matches before compaction and returns a short list of
already-durable candidate content so the compressor does not restate it. That
summary is kept to roughly 600 characters.

### Session journal

At session end, when there is at least one user message and two total messages,
the plugin:

1. Runs deterministic capture over any missed user content.
2. Appends one episode under `## Sessions` in the current journal when
   `journal_episodes` is enabled.

The episode records platform, an eight-character session identifier, user-turn
count, staged-candidate count and sample IDs, opening user excerpt, and closing
assistant excerpt. A session marker makes the write idempotent. The newest two
journal files are checked so a session crossing midnight is not duplicated.

### Built-in memory mirror

If built-in Hermes memory remains enabled, `on_memory_write` handles it as a
compatibility bridge:

- `add` to the built-in user target appends to `profile/user.md`.
- Other `add` targets append to `profile/agent.md`.
- Approximate duplicates are skipped.
- Replace and remove events are audited but do not mutate wiki pages.

The [recommended replacement configuration](setup.md#configure-the-provider)
disables built-in memory, so this path is normally dormant.

## Related guides

- [Setup](setup.md)
- [Review and curation](review-and-curation.md)
- [Reference](reference.md)
- [Operations](operations.md)
