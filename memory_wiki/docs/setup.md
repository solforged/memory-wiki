# Setup and activation

[Back to documentation index](../README.md)

This guide installs the source plugin, selects it as the Hermes memory provider,
initializes its wiki extensions, and verifies activation.

## Requirements

- Python 3.10 or newer.
- A Hermes version with external memory-provider and plugin CLI discovery.
- An existing, contracted wiki root for the `hermes memory-wiki init` command.
- A wiki whose typed pages follow the contract mirrored in
  [`schema.py`](../schema.py).

This is a source plugin, not a wheel. The plugin package is the `memory_wiki/`
directory. Tests and development metadata live one directory above it.

A normal writable provider startup can create `profile/`, `journal/`, and
`_inbox/` with their parents when the configured root is absent. That is only a
minimal runtime scaffold, not a complete contracted wiki. A mistyped root can
therefore create an unintended directory even though CLI `init` would reject
it.

## Link the plugin into Hermes

From the repository root:

```bash
HERMES_DIR="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_DIR/plugins"
ln -s "$PWD/memory_wiki" "$HERMES_DIR/plugins/memory-wiki"
```

If that destination already exists, inspect it before replacing it. The folder
name must remain `memory-wiki`, while the Python package directory is
`memory_wiki`.

## Configure the provider

In `$HERMES_HOME/config.yaml`, normally `~/.hermes/config.yaml`:

```yaml
memory:
  provider: memory-wiki
  memory_enabled: false
  user_profile_enabled: false

plugins:
  memory-wiki:
    wiki_root: ~/wiki
```

The two `false` settings disable the built-in `MEMORY.md` and `USER.md` stores.
If they remain enabled, the plugin can coexist with them and mirrors built-in
`add` events into its profile pages.

Restart running Hermes frontends after changing provider selection.

## Initialize the wiki

Preview first:

```bash
hermes memory-wiki init --dry-run
```

Then apply:

```bash
hermes memory-wiki init
```

Full CLI initialization is idempotent. It:

1. Creates `profile/`, `journal/`, and `_inbox/` when absent.
2. Seeds `profile/user.md` and `profile/agent.md` when absent.
3. Reconciles interrupted candidate operations.
4. Exact-anchor patches `_scripts/check.py` to recognize `profile`, `journal`,
   and `taste` pages.
5. Appends a marker-guarded memory section to `_meta/style-guide.md`.
6. Runs the wiki's `_scripts/check.py` when that file exists.

`--dry-run` previews file creation and patch diffs without writing. A normal run
applies first, then prints the resulting diff. Missing validator or style-guide
files are skipped. If the validator anchor has changed, the plugin prints a
manual patch and leaves the file untouched.

Ordinary writable provider startup only performs steps 1 through 3. It never
silently patches the wiki's contract files.

## Verify activation

```bash
hermes memory status
hermes memory-wiki status
hermes memory-wiki lint
```

Inside a Hermes conversation, `wiki_get` with `ref: status` reports the active
wiki root, read-only state, embedding state, page counts, candidate counts, and
prompt-block size.

## Related guides

- [Architecture and runtime](architecture.md)
- [Review and curation](review-and-curation.md)
- [Tool, CLI, and configuration reference](reference.md)
- [Operations, safety, and troubleshooting](operations.md)

