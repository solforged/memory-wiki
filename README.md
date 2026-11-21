# llm-wiki

A personal wiki that agents use as on-demand memory. The wiki is an [Open Knowledge Format](https://github.com/open-knowledge-format/open-knowledge-format/blob/main/SPEC.md) v0.3 bundle: a directory of Markdown files with YAML frontmatter, in its own Git repository. This repository holds what reads and writes it:

| Part | Path | Does |
| --- | --- | --- |
| Library and `wiki` CLI | `src/`, `bin/wiki.js` | Look up, search, capture, verify, lint, and manage private areas |
| omp extension and skill | `omp/extension.ts`, `skills/llm-wiki/` | `wiki` and `wiki_capture` tools, `/wiki`, recall of entries a prompt names |
| Tern plugin | `tern/` | Picker with preview, lock and unlock commands, lookups for Carly |

## Install

Needs Node.js 22 or later. Private areas need macOS.

```sh
aube install
ln -s "$PWD/bin/wiki.js" ~/.local/bin/wiki
wiki init                      # creates ~/.local/share/llm-wiki
omp plugin link "$PWD"         # extension and skill, in new omp sessions
tern plugin link "$PWD/tern"
```

The wiki is `$LLM_WIKI_HOME`, else `llm-wiki` under `$XDG_DATA_HOME` (`~/.local/share`).

## What the wiki holds

A concept is one file. `wiki capture` writes four types, each to its own directory:

| `type` | Directory | Holds |
| --- | --- | --- |
| `Concept` | `concepts/` | A term, idea or piece of jargon and what it means here |
| `Entity` | `entities/` | A person, team, host, service, tool or repository |
| `Project` | `projects/` | Something being built or run |
| `Investigation` | `investigations/` | A question, what was tried and found, what is open |

Files of any other `type` are read like these. `description` is the one sentence a lookup returns, and `x-llm-wiki-aliases` lists other names the concept answers to. `wiki --help` lists every command.

## Drafts and verification

Whatever an agent writes is `status: draft`, with `generated` naming the model. A draft is found by `wiki lookup` and `wiki search`, marked as a draft, and is never recalled into a prompt. You promote it with `wiki verify <id>` in a terminal, or `/wiki verify <id>` in omp. That records a `verified` event under your `human:` actor and sets `status: stable`.

When an agent revises a verified concept it returns to draft. Its earlier verification stays in the file as history and no longer counts, as OKF §5.3 requires.

Every change is one Git commit in the wiki's repository, plus a `log.md` entry and regenerated `index.md` files. If Git refuses the commit, for example through a hook, the change is undone.

These commits are not signed, whatever `commit.gpgSign` says: they are made unattended, and a signature would say you wrote them. They use the wiki repository's Git identity, or `llm-wiki <llm-wiki@localhost>` where it has none. Your Git hooks run on them as on any commit.

## Private areas

`.llm-wiki.yml` in the wiki names the private areas, `personal` and `work` by default. Each is an AES-256 encrypted sparse bundle under `.vaults/`, mounted at `<wiki>/<name>` while unlocked. Locked, the mountpoint is an empty directory: no file names and no text are on disk outside the image.

Each area's `passphrase` setting is a command that prints its passphrase. By default that is `op read` on an item in the 1Password `Private` vault, so each unlock asks for your approval. Create the item, then the area:

```sh
op item create --category password --vault Private --title 'llm-wiki work' \
  --generate-password='letters,digits,32'
wiki area create work
wiki area unlock work
wiki area lock work            # or: wiki area lock --all
```

- A read covers a private area only when it names it: `wiki lookup nightjar --area work`, or `--area all`.
- Each area has its own Git repository, `index.md` and `log.md` inside the image. The default space records nothing about it, and `wiki lint` reports any link from the default space into an area as an error.
- The area's repository sets `guard.exempt`, which a git-guard that supports it reads as "skip the term checks, refuse every push".

### Only local models read them

In omp, private areas are open only to a model that runs on this machine: one whose provider is listed in `local_providers` (default `llama.cpp`, `lm-studio`, `ollama`, `local`) and that answers on a loopback address. For any other model the extension:

- refuses `wiki` and `wiki_capture` calls that name a private area;
- blocks tool calls whose input reaches into one: a path into the area, the whole wiki directory while an area is unlocked, `wiki --area`, or `wiki area unlock`;
- refuses `/wiki unlock` unless the session's model and the `default`, `smol`, `slow`, `task` and `plan` roles are all local;
- once a session holds private text, stops prompts to a hosted model, and replaces the context of any request that still goes to one;
- blocks subagents that would run on a hosted model from such a session.

An area that omp unlocked is locked again when the session ends, or after 30 minutes without use.

These are guardrails for cooperative agents, not a sandbox:

- While an area is unlocked, every process running as you can read it. That includes other agents and Carly in Tern, which has file access without an approval step. Keep unlock windows short.
- A command that builds a private path at run time is not recognized.
- omp makes some requests on its own with other model roles, such as compaction and titles. The role check at `/wiki unlock` is what covers those. Use a dedicated omp profile whose roles are all local for private work.
- What a local session reads from a private area is written to that session's transcript in plaintext. Put the profile's session directory inside the unlocked area (`omp --session-dir`), or run it with `--no-session`.

The Tern plugin shows private entries in its picker while their area is unlocked. Its Carly functions read the default space only.

## Develop

```sh
aube test       # includes a test that creates and mounts a real encrypted image
aube run lint
```
