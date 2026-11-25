# memory-wiki

A personal wiki that agents use as long-term memory. The wiki is an [Open Knowledge Format](https://github.com/open-knowledge-format/open-knowledge-format/blob/main/SPEC.md) v0.3 bundle: a directory of Markdown files with YAML frontmatter, in its own Git repository. This repository holds what reads and writes it:

| Part | Path | Does |
| --- | --- | --- |
| Library, `wiki` CLI, and harness hooks | `src/`, `bin/wiki.js` | Look up, search, capture, deprecate, review/verify, `now.md`, lint, private areas, and `agy`/`claude` lifecycle hooks |
| Agent Skill | `skills/memory-wiki/` | Shared skill for Antigravity, Pi, Claude Code, and omp |
| omp extension | `omp/extension.ts` | `wiki` and `wiki_capture` tools, `/wiki`, prompt recall, and local-model guards |
| Tern plugin | `tern/` | Picker with preview, lock and unlock commands, lookups for Carly |

Pi integrates with `wiki` directly through `pi-mono`'s `memory` extension (`now.md` in the cached system prompt, prompt recall, `memory` tool with confirmed previews, `/memory`, and private-area guards).

## Install

Needs Node.js 22 or later. Private areas need macOS.

```sh
node scripts/install.js        # installs deps, links wiki CLI, skill, omp, and Tern plugins
wiki init                      # first machine only; other Macs clone your wiki repo instead
```

`scripts/install.js` is idempotent (supports `--dry-run`) and never auto-creates sparse bundles on a new host; instead it reports which private areas are missing so you can restore them.

Pi, omp, and the Antigravity CLI discover `~/.agents/skills/memory-wiki` directly; Claude Code and the Antigravity App read it when `~/.agents/skills/` is bridged into their skill folders. The wiki is `$MEMORY_WIKI_HOME`, else `memory-wiki` under `$XDG_DATA_HOME` (`~/.local/share`).

### Antigravity (`agy`) hooks

Add a `memory-wiki` entry to `~/.gemini/config/hooks.json` so Antigravity loads `now.md` at `SessionStart`, runs deduplicated prompt recall on step 1 of each user turn (`PreInvocation`), auto-approves read-only `wiki` commands, and hard-blocks `wiki verify` and private-area access (`PreToolUse`):

```json
{
  "memory-wiki": {
    "SessionStart": [
      { "type": "command", "command": "~/.local/bin/wiki hook agy", "timeout": 5 }
    ],
    "PreInvocation": [
      { "type": "command", "command": "~/.local/bin/wiki hook agy", "timeout": 5 }
    ],
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": "~/.local/bin/wiki hook agy", "timeout": 5 }
        ]
      }
    ]
  }
}
```

Claude Code supports the same guardrails and recall with `wiki hook claude` on `SessionStart`, `UserPromptSubmit`, and `PreToolUse`.

## What the wiki holds

A concept is one file. `wiki capture` writes four types, each to its own directory:

| `type` | Directory | Holds |
| --- | --- | --- |
| `Concept` | `concepts/` | A term, idea or piece of jargon and what it means here |
| `Entity` | `entities/` | A person, team, host, service, tool or repository |
| `Project` | `projects/` | Something being built or run |
| `Investigation` | `investigations/` | A question, what was tried and found, what is open |

An optional `now.md` at the bundle root (capped at 2,000 bytes) holds the small always-on working context (stack and active focus) injected at session start. Files of any other `type` are read like these. `description` is the one sentence a lookup returns, and `x-memory-wiki-aliases` (plus legacy `x-llm-wiki-aliases`) lists other names the concept answers to. `wiki --help` lists every command.

## Drafts and verification

Whatever an agent writes is `status: draft`, with `generated` naming the actor. A draft is found by `wiki lookup` and `wiki search`, marked as a draft, and is never recalled into a prompt. Promote drafts with `wiki verify <id>`, batch-review all pending drafts with `wiki review`, or use `/memory verify <id>` in Pi / `/wiki verify <id>` in omp. That records a `verified` event under your `human:` actor and sets `status: stable`.

When an agent revises a verified concept it returns to draft. Its earlier verification stays in the file as history and no longer counts, as OKF §5.3 requires. To retire a concept without deleting history, use `wiki deprecate <id> --superseded-by <path>`.

Every change is serialized with a lockfile and recorded as one Git commit in the wiki's repository, plus a `log.md` entry and regenerated `index.md` files. If Git refuses the commit, for example through a hook, the change is undone.

These commits are not signed, whatever `commit.gpgSign` says: they are made unattended, and a signature would say you wrote them. They use the wiki repository's Git identity, or `memory-wiki <memory-wiki@localhost>` where it has none. Your Git hooks run on them as on any commit.

## Private areas

`.memory-wiki.yml` in the wiki names the private areas, `personal` and `work` by default. Each is an AES-256 encrypted sparse bundle under `.vaults/`, mounted at `<wiki>/<name>` while unlocked. Locked, the mountpoint is an empty directory: no file names and no text are on disk outside the image.

Each area's `passphrase` setting is a command that prints its passphrase. By default that is `op read` on an existing item in the 1Password `Private` vault (`llm-wiki personal` / `llm-wiki work`), so each unlock asks for your approval:

```sh
op item create --category password --vault Private --title 'llm-wiki work' \
  --generate-password='letters,digits,32'
wiki area create work
wiki area unlock work
wiki area lock work            # or: wiki area lock --all
```

- Every command — including `lint` and `index` — covers a private area only when it names it (`--area work` or `--area all`).
- Setting `MEMORY_WIKI_DEFAULT_ONLY=1` disables all private area reads, writes, unlocks, restores, and syncs in the CLI itself.
- Each area has its own Git repository, `index.md`, `log.md`, and `.gitattributes` (`log.md merge=union`) inside the image. The default space records nothing about it, and `wiki lint` reports any link from the default space into an area as an error.
- The area's repository sets `guard.exempt`, which a git-guard that supports it reads as "skip the term checks, refuse every push".

### Syncing and backing up private areas across Macs

Never sync `.vaults/*.sparsebundle` through live file sync (iCloud, Synology Drive, Syncthing) or copy an image while unlocked. Instead, each Mac keeps its own local sparse bundle and exchanges committed Git history (including binary attachments such as PDFs, SVGs, and PNGs) as per-host `age`-encrypted Git bundles (`<host>.bundle.age`) on a WebDAV server (such as a Synology NAS over Tailscale) or a local directory:

1. Generate an `age` keypair per area (`age-keygen`), store the private `AGE-SECRET-KEY-1...` in a custom `age_identity` field on the area's 1Password item, and add the public recipient and identity command to `.memory-wiki.yml`:

```yaml
areas:
  personal:
    image: .vaults/personal.sparsebundle
    size: 2g
    passphrase: [op, read, "op://Private/llm-wiki personal/password"]
    age_recipient: age1...
    age_identity: [op, read, "op://Private/llm-wiki personal/age_identity"]
```

2. Keep machine-local transport settings (such as your NAS WebDAV URL and credential command) in the gitignored `.memory-wiki.local.yml` so internal hostnames never enter your wiki Git repository:

```yaml
sync:
  remote: https://dsm.example.ts.net/memory-wiki
  username: memory-wiki
  password: [op, read, "op://Private/NAS memory-wiki WebDAV/password"]
```

3. Sync from an unlocked area, or sync as you lock:

```sh
wiki area sync personal          # fetch & merge peers, then upload <host>.bundle.age
wiki area lock --sync personal   # sync first, then lock
wiki area restore personal       # on a second Mac: create a fresh local image from the newest bundle
```

Exports encrypt to the public `age1...` recipient (no extra Touch ID prompt) and skip uploading when `HEAD` is unchanged. Peer bundles are decrypted only inside the mounted encrypted volume — never in `/tmp` — and `index.md` merge conflicts regenerate automatically. Back up the NAS share with Btrfs Snapshot Replication + Hyper Backup (or offsite snapshots) for 3-2-1 recovery.

### Only local models read them

Across harnesses, private areas are open only to a model that runs on this machine (a provider listed in `local_providers` answering on loopback, with every configured auxiliary role also local in `omp`):

- **Antigravity (`agy`) & Claude Code**: `PreToolUse` hooks block paths into private areas (including `..` traversals and ancestor-cwd relative paths), the wiki root while an area is unlocked, `wiki --area`, `wiki area unlock`, and `wiki verify`/`review`.
- **Pi & omp**: enforce the same `tool_call` guards, refuse private reads/writes on hosted models, track session taint, and withhold context if a tainted session switches to a hosted model.

These are guardrails for cooperative agents, not a sandbox:

- While an area is unlocked, every process running as you can read it. Keep unlock windows short.
- A command that builds a private path dynamically at run time is not recognized.
- Use a dedicated all-local profile (`omp --profile private --offline --no-session` or `--session-dir` inside the unlocked area) for private work so transcripts and auxiliary roles stay local.

The Tern plugin shows private entries in its picker while their area is unlocked. Its Carly functions read the default space only.

## Develop

```sh
aube test       # includes a test that creates and mounts a real encrypted image
aube run lint
```
