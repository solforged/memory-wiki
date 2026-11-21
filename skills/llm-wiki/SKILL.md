---
name: llm-wiki
description: Look up or record what a name, term, project or past investigation means in the user's wiki, their long-term memory. Use before guessing at unfamiliar jargon, and when asked to remember something.
---

# llm-wiki

The user keeps an [OKF](https://github.com/open-knowledge-format/open-knowledge-format/blob/main/SPEC.md) wiki of Markdown files, by default in `~/.local/share/llm-wiki` (`wiki root` prints it). It holds four kinds of concept:

| `type` | Holds |
| --- | --- |
| `Concept` | A term, idea or piece of jargon and what it means here |
| `Entity` | A person, team, host, service, tool or repository |
| `Project` | Something being built or run: its goal, state and parts |
| `Investigation` | A question, what was tried, what was found, what is open |

## Read

Use the `wiki` tool when you have it; otherwise the `wiki` command through `bash`:

```sh
wiki lookup <name>            # exact title or alias; near names when there is none
wiki search <words>           # full text; --type Concept|Entity|Project|Investigation
wiki show <id>                # one concept with its text, e.g. wiki show concepts/sparse-bundle
wiki list --type Project
```

Every result carries a status, a trust tier, and a stale mark when its review date has passed:

- `draft`: written but not confirmed by the user. `deprecated`: kept for history; follow `superseded_by`.
- `unverified`, `machine-confirmed`, `human-reviewed`: who confirmed the current text.

Say so when an answer rests on a draft, unverified or stale concept. When the wiki and the repository or the user disagree, the wiki is the stale one: follow them and offer to update it.

## Write

Record something when the user asks you to remember it, or when you had to work out what a name means and it will come up again. Use the `wiki_capture` tool when you have it; otherwise:

```sh
wiki capture --by <producer>/<model> --type Concept --title 'Sparse bundle' \
  --description 'A macOS disk image kept as a directory of band files.' \
  --alias sparsebundle --tag macos --source https://example.com/page --body-file notes.md
wiki capture --by <producer>/<model> --id concepts/sparse-bundle --description '…'   # update
```

- `--description` is one sentence: it is all that a lookup returns. Put detail in the body.
- Look the name up first, and update the concept it already has instead of adding a second one.
- Link related concepts in the body with relative Markdown links, such as `[git-guard](../entities/git-guard.md)`.
- Everything you write is a draft. Only the user verifies: never run `wiki verify`, and never write `verified` or `status: stable` into a file. Tell the user the id so they can run `/wiki verify <id>`.
- Never record credentials, keys or tokens.

## Private areas

`personal` and `work` are encrypted areas that the user unlocks. They are readable only while unlocked and only by a model that runs on the user's machine; a session on a hosted model is refused. Do not try to unlock one, read its directory, or pass `--area` to reach it from such a session. Nothing about a private area goes into the default space.
