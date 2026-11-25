---
name: memory-wiki
description: Look up or record what a name, term, project, current focus, or past investigation means in the user's memory-wiki, their long-term memory. Use before guessing at unfamiliar jargon, and when asked to remember something.
---

# memory-wiki

The user keeps an [OKF](https://github.com/open-knowledge-format/open-knowledge-format/blob/main/SPEC.md) v0.3 wiki of Markdown files, by default in `~/.local/share/memory-wiki` (`wiki root` prints it). It holds four kinds of concept plus an optional `now.md` (current stack and focus, under 2,000 bytes):

| `type` | Holds |
| --- | --- |
| `Concept` | A term, idea or piece of jargon and what it means here |
| `Entity` | A person, team, host, service, tool or repository |
| `Project` | Something being built or run: its goal, state and parts |
| `Investigation` | A question, what was tried, what was found, what is open |

## Read

Use the `memory` tool (in Pi) or `wiki` tool (in omp) when you have one; otherwise run `wiki` through your shell tool:

```sh
wiki lookup <name>            # exact title or alias; near names when there is none
wiki search <words>           # full text with snippets; --type T --tag TAG --status S --stale
wiki show <id>                # one concept with its text, e.g. wiki show concepts/sparse-bundle
wiki list --type Project      # also: --tag TAG --status draft --stale --limit N
wiki recent                   # newest log.md entries
wiki now                      # current stack and focus (now.md)
```

Every result carries a status, a trust tier, and a stale mark when its review date has passed:

- `draft`: written but not confirmed by the user. `deprecated`: kept for history; follow `superseded_by`.
- `unverified`, `machine-confirmed`, `human-reviewed`: who confirmed the current text.

Say so when an answer rests on a draft, unverified or stale concept. When the wiki and the repository or the user disagree, the wiki is the stale one: follow them and offer to update it.

## Write

Record something when the user asks you to remember it, or offer to record corrections rather than writing unasked. Use `memory` (in Pi) or `wiki_capture` (in omp) when you have one; otherwise:

```sh
wiki capture --type Concept --title 'Sparse bundle' \
  --description 'A macOS disk image kept as a directory of band files.' \
  --alias sparsebundle --tag macos --source https://example.com/page \
  --body-file - <<'EOF'
Body Markdown here. Link related concepts with relative links such as
[git-guard](../entities/git-guard.md).
EOF

wiki capture --id concepts/sparse-bundle --description '…' --alias sb
wiki deprecate concepts/old-name --superseded-by sparse-bundle.md
wiki now --set <<'EOF'
## Stack
...
EOF
```

- Pass `--by <producer>/<model>` when you know it; otherwise `wiki` infers the harness when possible.
- `--description` is one sentence: it is all that a lookup returns. Put detail in the body.
- Look the name up first, and update the concept it already has instead of adding a second one.
- On update (`--id`), `--alias`, `--tag`, and `--source` **append** to existing lists by default (pass `--replace-lists` only when intentionally replacing them). `--body` / `--body-file` replaces the body.
- Link related concepts in the body with relative Markdown links, such as `[git-guard](../entities/git-guard.md)`.
- Everything you write is a draft. Only the user verifies: never run `wiki verify` or `wiki review`, and never write `verified` or `status: stable` into a file. Tell the user the id so they can verify it with `wiki verify <id>` (or `/memory verify <id>` in Pi, `/wiki verify <id>` in omp).
- Never record credentials, keys or tokens.

## Private areas

`personal` and `work` are encrypted areas that the user unlocks. They are readable only while unlocked and only by a model that runs on the user's machine; a session on a hosted model is refused. Do not try to unlock one, read its directory, or pass `--area` to reach it from such a session. Nothing about a private area goes into the default space.
