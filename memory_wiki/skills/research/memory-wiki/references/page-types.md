# Memory wiki page types

Use this reference while drafting a new typed page. `_meta/style-guide.md` and the live validator remain authoritative if they differ.

## Shared rules

- Place pages directly inside a recognized type folder. Nested paths fail validation.
- Use lowercase kebab-case filenames: `^[a-z0-9]+(-[a-z0-9]+)*\.md$`.
- Start frontmatter at byte zero with `---`.
- Every typed page uses `type`, `title`, `topics`, and `createdAt`.
- Use an ISO `createdAt` date: `YYYY-MM-DD`.
- Existing pages with legacy `created` remain valid; new pages use `createdAt`.
- `topics` is currently warning-only in the plugin mirror but remains a repository convention.
- Use `[[wikilinks]]` internally and ordinary Markdown links externally.
- `reports/` is compiler-owned and doesn't accept promoted candidates.

## Type selection

| Folder | Type | Use for | Extra required fields |
|---|---|---|---|
| `concepts/` | `concept` | Durable concept, definition, conceptual history, or explanatory topic | None |
| `claims/` | `claim` | One explicit proposition meant to be independently searchable and evaluated | `stance`, `confidence`, `maturity` |
| `sources/` | `source` | Source record, reading guide, annotated primary text, paper/book dossier | `author`, `medium` |
| `atlases/` | `atlas` | Navigational map across an established page cluster | None |
| `syntheses/` | `synthesis` | Multi-source argument, comparison, or integrated analysis | `status` |
| `refs/` | `ref` | Person, organization, entity dossier, technical guide, reusable reference | None |
| `tastes/` | `taste` | Stable collaboration, research, writing, or aesthetic preference | None |
| `reports/` | `report` | Generated health/status report | Compiler-owned |

Living-memory types are not staged as ordinary knowledge pages:

| Folder | Type | Write path |
|---|---|---|
| `profile/` | `profile` | `wiki_apply(action="write_profile", ...)` |
| `journal/` | `journal` | `wiki_apply(action="append_journal", ...)` |

## Allowed enums

```text
stance: external | candidate | model
confidence: unassessed | speculative | plausible | likely | confident
maturity: seedling | budding | evergreen
status: draft | published
medium: book | textbook | paper | course | video | post |
        podcast | game | thread | wiki
```

Candidate confidence is separate and does not allow `unassessed`:

```text
speculative | plausible | likely | confident
```

## Minimal templates

### Concept

```markdown
---
type: concept
title: Example concept
topics: [example]
createdAt: YYYY-MM-DD
---

# Example concept

## Definition

## History or mechanism

## Open questions

## Related

- [[related-page]]
```

### Claim

```markdown
---
type: claim
title: Example proposition
stance: candidate
confidence: plausible
maturity: seedling
topics: [example]
supports: []
contradicts: []
createdAt: YYYY-MM-DD
---

# Example proposition

State one proposition precisely.

## Evidence

- Evidence with source, location, and attribution.

## Qualifications

## Related

- [[related-page]]
```

Use `stance: external` for a proposition attributed to a source or actor. Use `stance: model` for the agent's explicitly marked analysis. Don't silently recast the user's hypothesis as either institutional fact or model conclusion.

### Source

```markdown
---
type: source
title: Source title
author: Author or attributed creator
medium: book
year: 0000
url: https://example.com
topics: [example]
createdAt: YYYY-MM-DD
---

# Source title

## Bibliographic note

## Scope

## Key passages or findings

Keep quotation, paraphrase, and interpretation visibly distinct.

## Connections

- [[related-page]]
```

`year` and `url` are optional. Never invent them. The `medium` value must use the enum above.

### Atlas

```markdown
---
type: atlas
title: Topic atlas
topics: [example]
createdAt: YYYY-MM-DD
---

# Topic atlas

## Orientation

## Primary sources

## Concepts and claims

## Syntheses and open questions
```

Create an atlas only after a page cluster exists. An atlas navigates; it shouldn't duplicate every page's substance.

### Synthesis

```markdown
---
type: synthesis
title: Integrated question or argument
status: draft
claims: []
topics: [example]
createdAt: YYYY-MM-DD
---

# Integrated question or argument

## Question

## Evidence

## User hypothesis

## Rival interpretations

## Analysis

## Open questions
```

Use `status: draft` until the argument and provenance have been reviewed. `claims` may be an empty list, a legacy scalar reference list, or a valid structured-claim list.

### Ref

```markdown
---
type: ref
title: Reference title
topics: [example]
createdAt: YYYY-MM-DD
---

# Reference title

## Overview

## Relevant details

## Relationships

- [[related-page]]
```

A ref may include an optional external `url`. Use refs for reusable entities and guides, not argumentative synthesis.

### Taste

```markdown
---
type: taste
title: Stable preference domain
topics: [collaboration]
createdAt: YYYY-MM-DD
---

# Stable preference domain

## Preferences

## Boundaries

## Examples
```

Use a taste page when a coherent preference domain needs explanation and examples. Use a compact profile bullet for simple always-needed preferences.

## Structured claims

Typed pages may carry a JSON-compatible `claims` list in frontmatter. Each claim object may use:

```text
id, text, status, confidence, evidence, updatedAt
```

- `text` is required.
- `id`, when present, uses `claim.<namespace>.<local-name>` with kebab-case
  inside each component.
- Numeric `confidence` must be finite and between `0` and `1`.
- `evidence` must be a list.

Evidence objects may use:

```text
kind, sourceId, path, lines, weight, confidence,
privacyTier, note, updatedAt
```

Example:

```yaml
claims: [{"id":"claim.example.precise-claim","text":"A precise claim.","status":"supported","confidence":0.9,"evidence":[{"kind":"source","sourceId":"source.example","path":"sources/example.md","lines":"18-24","updatedAt":"YYYY-MM-DD"}],"updatedAt":"YYYY-MM-DD"}]
```

Prefer stable claim IDs. Repeated IDs with differing normalized text or status become contradiction clusters in compiler output.

## Staging map

| Destination | Candidate `kind` | Target guidance |
|---|---|---|
| `claims/` | `claim` | Explicit new path or default claim routing |
| `concepts/` | `concept` | Explicit new path or default concept routing |
| `sources/` | `note` | Explicit `sources/<slug>.md` required |
| `atlases/` | `note` | Explicit `atlases/<slug>.md` required |
| `syntheses/` | `note` | Explicit `syntheses/<slug>.md` required |
| `refs/` | `note` | Explicit `refs/<slug>.md` required |
| `tastes/` | `note` or `preference` | Explicit `tastes/<slug>.md` for full pages |

For a new page, stage the complete page and a non-existing target. For an existing target, stage one single-line addition and a precise section. Page-shaped or multi-line candidates cannot append to existing pages.
