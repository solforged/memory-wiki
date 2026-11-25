# Maintaining this wiki

This directory is an [Open Knowledge Format](https://github.com/open-knowledge-format/open-knowledge-format/blob/main/SPEC.md) v0.3 bundle. This file has no `type`, so it is not a concept.

## What goes where

| `type` | Directory | Holds |
| --- | --- | --- |
| `Concept` | `concepts/` | A term, idea or piece of jargon and what it means here |
| `Entity` | `entities/` | A person, team, host, service, tool or repository |
| `Project` | `projects/` | Something being built or run: its goal, state and parts |
| `Investigation` | `investigations/` | A question, what was tried, what was found, what is open |

`now.md` (optional, capped at 2,000 bytes) holds the small always-on working context: current stack and active focus bullets, with `stale_after` frontmatter and no `type`.

`personal/` and `work/` are private areas with the same layout inside. They are encrypted disk images, empty while locked. A private concept may link to the default space. Nothing in the default space links into a private area or mentions what it holds.

## Rules

- Read with `wiki lookup <name>`, `wiki search <words>`, `wiki show <id>`, `wiki recent`, and `wiki now`. They report each concept's status, trust tier and staleness; say so when you rely on a draft, unverified or stale concept.
- Write with `wiki capture`, `wiki deprecate`, or `wiki now --set`, which keep `index.md` and `log.md` current and commit atomically. Pass `--by <producer>/<model>`. Everything `wiki capture` writes is `status: draft`.
- Only a person verifies. Never run `wiki verify` or `wiki review`, and never write `verified` or `status: stable` yourself.
- One concept per file, named for what it defines. Put the definition in `description`, in one sentence; that sentence is what a lookup returns. Put other names it goes by in `x-memory-wiki-aliases`.
- Link concepts with relative Markdown links, such as `[git-guard](../entities/git-guard.md)`. `[[wikilinks]]` are not links in OKF.
- Record where a claim comes from in `sources`. Set `stale_after` only when a review date is known.
- To retire a concept, run `wiki deprecate <id> --superseded-by <path>` (keeping the file with `status: deprecated`).
- Keep keys you do not know. Do not reformat frontmatter you did not change.
