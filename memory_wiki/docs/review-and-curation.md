# Review and curation

This guide explains how memory candidates are staged, reviewed, promoted,
superseded, and conservatively curated.

For executable operator recipes, see
[Common review workflows](reference.md#common-review-workflows).

[Back to documentation index](../README.md)

## Candidate lifecycle

A candidate is one Markdown file plus lifecycle events in
`_inbox/decisions.jsonl`. A typical file looks like:

```markdown
---
id: 20260726-190501-019f4a37-01
status: pending
kind: preference
target: ""
section: ""
confidence: plausible
origin: sync_turn
session: 019f4a37-example
turn: 3
created: 2026-07-26
reviewed: ""
promoted_to: ""
topics: []
fields: {}
hash: 0123456789abcdef
---
I prefer concise implementation notes.

## Evidence

> [user, turn 3] "I prefer concise implementation notes."
```

Candidate IDs contain a UTC timestamp, the first eight hexadecimal session
characters, and a two-digit sequence. Files are created exclusively, so an
existing path is never overwritten.

Staged content is preserved verbatim up to 50,000 characters. This includes
multiline Markdown and complete pages with frontmatter. Evidence is appended at
the final line-anchored `## Evidence` section. Complete-page candidates are
validated when staged and should be given a new-page target.

Allowed candidate kinds are:

```text
preference  fact  decision  correction  claim  concept  note
```

Allowed confidence values are:

```text
speculative  plausible  likely  confident
```

Allowed `fields` keys are:

```text
author  medium  year  url  aliases  ref  supports  contradicts
stance  maturity  status
```

Unknown field keys are dropped. Paths escaping the wiki root are refused.

The main status transitions are:

```text
pending  -> approved -> promoted -> superseded
pending  -> rejected
approved -> rejected
```

In practice, both pending and approved candidates can be rejected. A promoted
candidate is superseded only through a replacement. Rejected and superseded
candidates cannot be approved again. Re-stage corrected content instead.

### Duplicate checks

Staging rejects:

- The same normalized content hash as an active pending, approved, or promoted
  candidate.
- High token overlap with an existing profile bullet.
- High token overlap with an existing knowledge-page title.
- Content with no indexable tokens.

Rejected and superseded candidates do not block re-staging. Supersession skips
the advisory profile and knowledge-title overlap checks because replacement
text is expected to overlap. An exact hash collision with another active
candidate is still rejected.

## Review and promotion

Review is available through the operator CLI or the model-facing tools. Both
use the same queue builder, filters, route resolution, staleness calculation,
and advisory hints.

Queue items can include:

- Effective target and section.
- Age and stale status.
- `truncated` or `needs_context` warnings.
- Existing pages that may already cover the content.
- Similar pending candidate IDs.
- Heuristic target suggestions for tastes, URLs, and configuration notes.

Hints do not mutate anything. Token overlap cannot determine truth or detect a
contradiction reliably, so review remains the authority.

### Default promotion routes

| Candidate kind | Default destination |
| --- | --- |
| `preference` | `profile/user.md`, under `## Preferences` |
| `fact` | `profile/user.md`, under `## Notes` |
| `correction` | `profile/user.md`, under `## Notes` |
| `note` | `profile/agent.md`, under `## Notes` |
| `decision` | Today's journal, under `## Notes` |
| `claim` | A new page in `claims/` |
| `concept` | A new page in `concepts/` |

Use an explicit target for a source, ref, taste, synthesis, atlas, existing
knowledge page, or any non-default destination.

If an explicit target exists, promotion appends under the requested section or
`## Notes`. If a recognized `.md` target does not exist, promotion creates a
page whose type comes from the target folder.

Append targets accept one-line, non-page content only. Multiline or complete
page content must target a new page. New-page collisions fail rather than
inventing suffixed filenames.

Before appending to an existing typed knowledge page or creating a new page,
the plugin renders and validates the result. Direct profile and journal writes
use their own structured writers but do not call `validate_page`. Source pages
require `author` and `medium`. Generated claims receive default stance,
confidence, and maturity fields plus evidence and open-question sections.
Complete-page candidates keep their own frontmatter and body, then receive a
promotion marker.

### Approval

Approval can override content, target, section, confidence, topics, and
allowlisted fields. It normally changes `pending` to `approved`, records the
decision, then promotes immediately.

The model tool can set `and_promote: false` to stop at `approved`. Calling
approve again promotes it. Promotion is idempotent: a candidate marker already
present at its destination returns `already_promoted`.

### Rejection

Rejection records an optional reason, marks the candidate `rejected`, and keeps
the file for audit and possible pruning. Promoted or superseded candidates
cannot be rejected.

### Supersession

`supersede` replaces an outdated promoted bullet:

1. The old candidate must be promoted, bullet-shaped, and have a recorded
   promotion target.
2. A replacement candidate is staged against the same destination.
3. Profile and journal replacements promote immediately.
4. Knowledge-page replacements remain pending for review.
5. Promotion swaps the marked old line for the new line.
6. The old candidate becomes `superseded` and points to the replacement.

Complete-page promotions cannot be superseded through this action. Edit the
page directly or stage a replacement page. If a human deleted the old marker,
promotion cannot prove the line to replace and appends the new content instead.

### Marker comments

Promoted bullets carry:

```html
<!-- memory-wiki: <candidate-id> -->
```

New pages carry:

```html
<!-- memory-wiki: promoted <candidate-id> -->
```

Keep these comments when manually editing. They support idempotency,
supersession, lint warnings, and recovery.

## Dreaming and curation

`dream` is a deterministic review planner. It is not an autonomous synthesis
or knowledge-writing loop, and it has no scheduler.

A pass inspects the first `dream_limit` pending candidates in deterministic
filename order. It can suggest:

- `reject_stale`
- `reject_covered`
- `supersede_existing`
- `consolidate_similar`
- `retarget`
- `review_knowledge`
- `promote_profile`
- `needs_review`

Similar candidates form transitive groups. Covered corrections are never given
a reject recommendation because overlap may represent contradiction rather
than duplication.

Without `hermes memory-wiki dream --apply-safe` at the CLI, or
`wiki_apply(action="dream", apply_safe=true)` through the model tool, dreaming
performs no writes. Safe apply never rejects, retargets, consolidates, or
promotes knowledge pages. It only approves candidates that are:

- Kind `preference` or `note`.
- Confidence `likely` or `confident`.
- One line and not a complete page.
- Routed to a path beginning with `profile/`.
- Free of duplicate, similarity, quality, or retarget warnings.

Normal routing limits that to `profile/user.md` and `profile/agent.md`. The
current implementation checks the broader `profile/` prefix when an explicit
target is supplied.

## Related guides

- [Architecture](architecture.md)
- [Reference](reference.md)
- [Operations](operations.md)
