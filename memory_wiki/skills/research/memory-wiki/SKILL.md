---
name: memory-wiki
description: Curate and query the review-first memory wiki.
version: 1.0.0
author: Aurelian
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [wiki, memory, research, curation, provenance]
    category: research
    related_skills: [obsidian]
    requires_tools: [wiki_search, wiki_get]
---

# Memory wiki curation

## Overview

Use the memory wiki as one system with three distinct layers:

- **Living memory:** `profile/user.md` and `profile/agent.md`, written directly for durable personal context and working agreements.
- **Episodic memory:** `journal/YYYY-MM-DD.md`, appended directly for dated notes and session episodes.
- **Durable knowledge:** concepts, claims, sources, atlases, syntheses, entities, refs, and tastes. Changes enter through staged candidates and human review.
- **Private archive:** `raw/` contains user-authorized edited transcripts and archival evidence. It is searchable through the `raw` or `all` corpus but excluded from the default curated `pages` corpus.

This skill replaces the generic `llm-wiki` workflow for this repository. Do not impose that skill's repository-wide `raw/`, `SCHEMA.md`, or append-only `log.md` architecture. This plugin's `raw/` is a narrow evidence archive, not the canonical knowledge layer. The authoritative local contract is `_meta/style-guide.md`, the plugin's validators, and the review queue.

## When to use

Load this skill when the user asks to:

- query, research from, ingest into, or curate the memory wiki;
- import, search, or curate user-authorized edited transcripts in the private `raw/` archive;
- preserve a source, argument, concept, claim, reading guide, or synthesis;
- review candidates, resolve duplication, supersede stale memory, or run a dream pass;
- lint, compile, audit, or assess the wiki's health;
- decide whether something belongs in profile, journal, or durable knowledge.

Don't use it merely because a substantive conversation *could* become a note. Preserve the user's conversational agency. Curate when asked, when durable capture is plainly part of the task, or after offering a specific useful capture.

## Non-negotiable boundaries

1. **Source first.** If the user supplies a URL, file, book passage, thread, or other direct source, inspect that source before treating wiki recall as evidence about it. Wiki pages can supply context, not proof of the source's current contents.
2. **Orient before writing.** Read `wiki_get("status")`, `_meta/style-guide.md`, and relevant existing pages. For health work, also read `wiki_get("compiler")`.
3. **Search before creating.** Use `wiki_search`, then open likely matches with `wiki_get`. Check aliases, neighboring concepts, and candidate overlap before choosing a target.
4. **Review first for knowledge.** Never write directly to typed knowledge pages during normal operation. Use `wiki_apply(action="stage", ...)`. A staged candidate is a proposal, not established knowledge.
5. **Direct writes are narrow.** Use `write_profile` only for durable user facts, preferences, corrections, and agent working agreements. Use `append_journal` only for dated episodic notes. Don’t use either as a shortcut around knowledge review.
6. **No self-approval by default.** Report staged candidate IDs and targets. Approve or reject only when the user explicitly asks for queue mutation or has clearly made the review decision.
7. **Preserve epistemic roles.** Keep source evidence, the user's hypothesis, rival interpretations, and agent analysis visibly distinct. Attribute private interpretations to the user rather than laundering them into neutral fact.
8. **Cite what you use.** Answers grounded in the wiki cite repo-relative paths. Open cited pages with `wiki_get`; search snippets alone aren't enough for consequential claims.
9. **Protect unrelated work.** Before bulk CLI or filesystem operations, inspect git status. Never overwrite, revert, commit, or fold in unrelated dirty files.
10. **Raw is deliberate and private.** Add archival transcripts only at the user's request. Remove system prompts, tool traces, platform identifiers, local paths, secrets, and runtime metadata; label material as edited rather than presenting it as verbatim.

## Orientation procedure

At the start of a curation task:

1. Call `wiki_get(ref="status")`.
   - Confirm `wiki_root`, `read_only`, page counts, candidate counts, search backend, compiler availability, and any reported failure.
   - A failed optional embedding backend isn't a blocker if lexical search works. State the degradation rather than pretending semantic recall succeeded.
2. Open `_meta/style-guide.md` with `wiki_get`.
3. Search the topic with `wiki_search`.
   - Use `corpus="pages"` for durable knowledge.
   - Search `profile`, `journal`, or `candidates` explicitly when those layers
     may matter. Reserve `corpus="all"`, which also includes private `raw/`, for
     user-authorized archival recall.
   - Prefer `granularity="auto"`; use `claim`, `evidence`, or `heading` when the
     task needs an exact retrieval unit.
   - For historical questions, set `as_of` deliberately. Do not include future,
     expired, or superseded claims unless the question requires history.
4. Open the relevant pages and any candidate refs returned.
5. For lint, claim-health, or broad curation, also open `wiki_get(ref="compiler")` and `wiki_get(ref="review-queue")`.

**Completion criterion:** you can name the relevant existing pages, pending overlaps, correct target type, and current provider health before proposing a write.

## Query workflow

1. Search with terms from the user's question, including likely aliases or original-language forms where useful.
2. Open enough pages to reconstruct provenance and disagreement. Don’t answer from snippets alone.
3. If the question concerns a live external source, inspect it directly and use wiki pages only as historical context.
4. Answer with the distinctions the material warrants:
   - **Source evidence**
   - **User hypothesis or attributed interpretation**
   - **Rival interpretations**
   - **Agent analysis**
5. Cite repo-relative paths inline, for example `claims/example.md` or `sources/example.md`.
6. Use `wiki_get(ref="context-pack", query="...")` when a task needs a bounded,
   cited bundle rather than an unbounded sequence of searches.
7. Don’t file every answer. Stage a synthesis only when it would be costly to reconstruct, materially connects existing pages, or the user asks to preserve it.

**Completion criterion:** every important wiki-derived claim is traceable to an opened page, and any new synthesis remains explicitly proposed until reviewed.

## Ingest and capture workflow

### 1. Inspect and identify the source

- URL or online document: use the appropriate web tool and retain the canonical URL.
- Local file: read the original file before using summaries or session history.
- Pasted passage: preserve exact quotation boundaries and any bibliographic context the user supplied.
- Book or paper: distinguish the primary text from commentary and from your interpretation.

Never fabricate missing author, date, publication, page, or URL metadata. Use explicit unknowns or omit optional fields.

### 2. Discuss significance when the task is exploratory

If the user is exploring rather than issuing a mechanical ingest command, discuss what seems important before staging a page. Keep the user's interpretation separate from yours. In automated or explicit bulk-ingest tasks, proceed without requiring a conversational pause.

### 3. Search for overlap

Search titles, authors, aliases, central concepts, claims, and URLs across durable pages and active candidates. Prefer updating the knowledge graph over creating a near-duplicate page.

### 4. Choose the smallest adequate page type

Use the page-type table in `references/page-types.md`.

- Preserve a source as `source` when the page records or guides the reading of that source.
- Use `claim` for one explicit, independently searchable proposition with evidence.
- Use `concept` for a durable explanation or conceptual history.
- Use `synthesis` for a multi-source argument or integrated analysis.
- Use `entity` for a named actor that needs a stable ID and evidenced
  relationships.
- Use `ref` for a person, organization, technical guide, reusable reference, or entity dossier.
- Use `atlas` for navigation across an established cluster.
- Use `taste` for stable collaboration, research, or aesthetic preferences that merit a typed page rather than a profile bullet.
- Use `raw` only for private archival evidence such as an edited conversation transcript. Raw records preserve context; they do not establish claims merely because an assistant said them.

Avoid pages for passing mentions. Prefer one well-scoped page with links over a spray of stubs.

### 5. Draft with provenance

For a new page, draft the complete page with valid frontmatter, sentence-case headings, internal `[[wikilinks]]`, and ordinary Markdown links for external sources. Use lowercase kebab-case filenames directly inside the type folder.

For an existing page, the normal tool path appends **one-line reviewed bullets** to a chosen section. Don’t stage page-shaped or multi-line content against an existing target; promotion will reject it. For a substantial rewrite, decompose the change into reviewable claims/additions, create a companion page, or ask the user how they want to handle an exceptional manual edit.

### 6. Stage, don’t promote

New complete page:

```text
wiki_apply(
  action="stage",
  kind="note",              # use claim or concept for those two types
  target="sources/example.md",
  confidence="likely",
  content="<complete page with frontmatter>"
)
```

One-line addition to an existing page:

```text
wiki_apply(
  action="stage",
  kind="note",
  target="refs/example.md",
  section="## Notes",
  confidence="plausible",
  content="One reviewable sentence with explicit attribution."
)
```

Use an explicit target for `source`, `atlas`, `synthesis`, `ref`, and `taste`. `reports/` is compiler-owned and cannot receive promotions.

### 7. Report the proposal

Tell the user:

- source inspected;
- existing pages consulted;
- candidate ID and `_inbox/` ref;
- intended target and page type;
- unresolved uncertainty or duplication risk;
- that it is pending review.

**Completion criterion:** every proposed change has a candidate ID, valid target, explicit provenance, and no claim that it is canonical before promotion.

## Direct memory writes

### Profile

Use `wiki_apply(action="write_profile", ...)` for facts or preferences expected to remain useful across sessions.

- `page="user"`: identity, stable preferences, current long-running focus, durable personal facts.
- `page="agent"`: working agreements, ongoing collaboration threads, stable environment notes.
- Pass a precise `section`, such as `## Preferences` or `## Working agreements`.
- Prefer compact declarative bullets. Don’t store task progress, artifact IDs, commit hashes, or facts likely stale within a week.

Use `mode="replace_section"` only when replacing the entire named section is deliberate and its full replacement text has been reviewed. Otherwise append.

### Journal

Use `wiki_apply(action="append_journal", ...)` for dated episodes, decisions with temporal context, or notes useful for later reconstruction but inappropriate for the always-loaded profile.

Use an `idempotency_key` when retrying a direct write after uncertain delivery.

**Completion criterion:** the content is in the narrow direct-write zone, compact, durable or explicitly episodic, and not a disguised knowledge-page edit.

## Review workflow

1. Read `wiki_get(ref="review-queue")`, using filters when the queue is large.
2. Open each candidate under consideration by ID or `_inbox/` ref.
3. Search and open its effective target plus similar pages.
4. Recommend one of:
   - approve as written;
   - approve with an explicit content, target, section, confidence, topics, or fields override;
   - reject with a one-line reason;
   - leave pending for more evidence;
   - supersede an older promoted item.
5. If the user asked only for review, present recommendations and wait for decisions. If the user explicitly authorized mutation, apply exact candidate IDs one at a time and stop on failure.
6. Remember that `approve` promotes immediately by default. Use `and_promote=false` only for a deliberate two-stage approval.
7. After mutation, re-open the target and review queue to verify final state.

Never bulk-approve broad filters merely because the candidates look related. Exact IDs preserve the audit boundary.

**Completion criterion:** every mutated candidate has an explicit reviewed decision, the target contains the expected marker/content, and the queue reflects the new state.

## Corrections and supersession

When a remembered item is outdated:

1. Search for and open the canonical page.
2. Find the old candidate marker comment and candidate ID.
3. Open the old candidate by ID to verify status and target.
4. Call `wiki_apply(action="supersede", id="<old-id>", content="<replacement>")`.
5. Profile and journal replacements may apply immediately. Knowledge-page replacements remain pending review.
6. Re-open the page or candidate and verify that the old assertion wasn't silently duplicated.

Full pages promoted as page-shaped candidates can't be superseded through this bullet-level path. Don’t pretend otherwise; propose a reviewed companion/replacement strategy.

For structured claims, preserve history: the replacement gets `validFrom` and
`supersedes`, while the prior claim becomes `superseded` with an exclusive
`validUntil`. Evidence excerpts require a matching `sha256:` `excerptHash`.
Never overwrite a prior claim merely to make current recall look tidy.

## Entity workflow

- Search `entities/` and `refs/`, including aliases, before creating a node.
- Entity IDs use `entity.<namespace>`-style lowercase stable identifiers.
- A relationship is an object with `predicate` and `targetId`; the target is an
  entity ID, not a path or title.
- Treat `reports/entity-migration-candidates.md` as review advice only. Do not
  move a `refs/` page without an explicit curation decision.
- Compile after graph changes and resolve duplicate IDs, invalid targets,
  orphaned targets, and ambiguous relationships.

## Curation, dreaming, and maintenance

- Use `wiki_get(ref="dream")` for a read-only deep curation plan.
- Use `wiki_get(ref="semantic-maintenance")` for read-only `duplicate`,
  `supports`, `refines`, `contradicts`, `temporal_successor`, and `unrelated`
  findings.
- Semantic findings are content- and classifier-bound. Regenerate before any
  decision. The user-facing write path is an explicitly confirmed CLI
  acceptance, which stages a pending candidate and does not approve or promote
  it.
- Use `wiki_apply(action="dream", phase="deep"|"all", ...)` when the user asks to run a phase.
- Treat `light` and `rem` as reinforcement/analysis, not promotion authority.
- `apply_safe=true` is narrow but still a write. Use it only with explicit authorization.
- Scheduled policy and dream reports are operational choices, not defaults to enable casually.
- Staged dream suggestions are not established facts until promoted.
- Use `wiki_get(ref="review")`, `maintenance`, or `history` for bounded,
  privacy-aware operational views. None of these read models authorizes a
  write.

## Lint, compile, and health

For a comprehensive health check, use the CLI and repository validator when terminal access is available:

```bash
cd <wiki_root>
python3 _scripts/check.py
hermes memory-wiki lint
hermes memory-wiki compile
hermes memory-wiki migrate --plan
git diff --check
git status --short
```

Then read `wiki_get(ref="status")` and `wiki_get(ref="compiler")` again.

Check at least:

- schema and filename errors;
- broken or malformed wikilinks from `_scripts/check.py`;
- missing evidence, low-confidence claims, stale pages, and contradiction clusters;
- temporal lineage and entity relationship integrity;
- pending/approved candidates and stale review items;
- compiler output freshness;
- search backend degradation;
- unexpected changes to human-authored content or unrelated dirty files.

Compiler-owned `reports/` and `.memory-wiki/cache/` are derived. Don’t manually rewrite generated blocks. Compilation may refresh them, so inspect diffs before committing.

**Completion criterion:** validators return zero or every failure is reported precisely; status/compiler agree with filesystem counts; and all changed files are accounted for.

## Common pitfalls

1. **Loading `llm-wiki` too.** Its architecture conflicts with this repository even though both can use a directory named `raw/`. Use this skill alone for memory-wiki curation.
2. **Answering from recall snippets.** Search narrows the field; `wiki_get` supplies citeable content.
3. **Treating a candidate as fact.** Pending and approved-but-unpromoted material is still provisional.
4. **Approving your own proposal automatically.** Staging and review are separate acts.
5. **Sending a full page to an existing target.** Existing targets accept one-line append candidates, not page replacement.
6. **Using profile as a junk drawer.** Keep procedural details in skills, episodic details in journals, and reusable knowledge in typed pages.
7. **Editing generated reports.** Fix source pages or compiler logic, then compile.
8. **Creating duplicate entity pages.** Search aliases and refs before choosing a slug.
9. **Flattening disagreement.** Preserve attribution, uncertainty, and rival interpretations.
10. **Ignoring a dirty worktree.** Verification and commits must account for pre-existing user changes.

## Verification checklist

- [ ] `wiki_get("status")` confirmed the active root and health.
- [ ] `_meta/style-guide.md` and relevant pages were opened.
- [ ] Existing pages and active candidates were searched before writing.
- [ ] Direct source material was inspected before historical recall was used.
- [ ] The destination layer and page type are correct.
- [ ] Durable knowledge was staged, not silently written or self-approved.
- [ ] Candidate IDs, targets, and provisional status were reported.
- [ ] Wiki-derived claims cite opened repo-relative paths.
- [ ] Validators and compiler were run when the task changed structure or many pages.
- [ ] Unrelated dirty files were preserved.
