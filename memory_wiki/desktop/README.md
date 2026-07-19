# Native Desktop plugin

This runtime plugin adds a read-only Memory Wiki overview pane to Hermes
Desktop. It reuses the existing Python dashboard API and does not replace the
WebUI dashboard.

Enable the backend API, then link the plugin directory into the active Hermes
profile:

```bash
hermes plugins enable memory-wiki --no-allow-tool-override
memory_wiki_hermes_home="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$memory_wiki_hermes_home/desktop-plugins"
ln -s /path/to/memory-wiki/memory_wiki/desktop \
  "$memory_wiki_hermes_home/desktop-plugins/memory-wiki"
```

Hermes Desktop watches `plugin.js` and reloads it after changes. If needed,
run **Reload desktop plugins** from the command palette.

The overview pane is separate from model-facing memory. Desktop needs three
independent gates:

1. Set `memory.provider: memory-wiki`.
2. Enable the `memory-wiki` plugin for its dashboard API.
3. Enable the built-in `memory` toolset for Desktop's `cli` platform:

```bash
hermes tools enable memory --platform cli
```

Also expose `/path/to/memory-wiki/memory_wiki/skills` through
`skills.external_dirs` and disable the conflicting stock `llm-wiki` skill.
Restart Hermes Desktop and create a fresh session after any provider, skill, or
toolset change.

Verify the live gates with:

```bash
hermes memory status
hermes tools list --platform cli
HERMES_PLATFORM=cli hermes skills list --enabled-only
```

The first fresh Desktop session should expose `wiki_search`, `wiki_get`, and
`wiki_apply`; the native pane itself remains read-only.
