# memory-wiki (repo)

Hermes Agent memory-provider plugin: review-first memory over a plaintext wiki,
with structured claim compilation, phased dreaming, optional QMD recall, a
read-only health dashboard, and a plugin-owned curation skill exposed through
Hermes's `skills.external_dirs`. The plugin package and full README live in
[`memory_wiki/`](memory_wiki/README.md); that directory is what gets
symlinked into `$HERMES_HOME/plugins/memory-wiki`.

See [`docs/OPENCLAW_PARITY.md`](docs/OPENCLAW_PARITY.md) for the OpenClaw
comparison and remaining boundaries.

```bash
uv run --group dev pytest   # run the test suite
```
