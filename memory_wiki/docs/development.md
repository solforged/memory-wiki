# Development

[Back to documentation index](../README.md)

## Test the plugin

From the repository root:

```bash
uv run --group dev pytest -q
```

The project declares no runtime dependencies. `pytest` is the only development
dependency. Hermes supplies PyYAML, `PluginLlm`, and the `MemoryProvider` base
class. Optional `fastembed` must be installed separately and is imported lazily.

The test bootstrap uses a minimal `MemoryProvider` stub when a Hermes checkout
is absent. To test signatures and the real `MemoryManager`, point
`HERMES_AGENT_SRC` at a Hermes source checkout:

```bash
HERMES_AGENT_SRC=/path/to/hermes-agent uv run --group dev pytest -q
```

One author-machine-only frontmatter compatibility probe checks the hard-coded
path `/Users/admin/wiki` when it exists. Contract-sensitive changes should
additionally run `hermes memory-wiki lint` against a safe copy of the relevant
real wiki.

## Source map

| Module | Responsibility |
| --- | --- |
| [`__init__.py`](../__init__.py) | Provider registration, lifecycle, prompt, prefetch, hooks, backup. |
| [`config.py`](../config.py) | Config defaults, loading, saving, setup schema. |
| [`scaffold.py`](../scaffold.py) | Runtime creation, recovery boundary, validator/style-guide patching. |
| [`schema.py`](../schema.py) | Wiki contract mirror, page construction, validation, markers. |
| [`wikifs.py`](../wikifs.py) | Frontmatter subset, safe paths, atomic I/O, JSONL, locking. |
| [`index.py`](../index.py) | Incremental in-memory index and corpora. |
| [`search.py`](../search.py) | Lexical and hybrid ranking, snippets, prefetch block. |
| [`embed.py`](../embed.py) | Background `fastembed` worker and disposable vector cache. |
| [`layers.py`](../layers.py) | Prompt compilation, profiles, journals, session episodes. |
| [`extract.py`](../extract.py) | Deterministic user-turn extraction. |
| [`llm_extract.py`](../llm_extract.py) | Optional structured host LLM extraction. |
| [`hooks.py`](../hooks.py) | Turn capture, compression rescue, journaling, built-in mirror. |
| [`candidates.py`](../candidates.py) | Candidate files, dedup, audit, pruning, recovery. |
| [`hints.py`](../hints.py) | Shared review queue, filters, warnings, target hints. |
| [`promote.py`](../promote.py) | Approval, rejection, routing, promotion, supersession. |
| [`dream.py`](../dream.py) | Conservative curation planner and safe apply. |
| [`tools.py`](../tools.py) | Model-facing schemas and handlers. |
| [`cli.py`](../cli.py) | `hermes memory-wiki` command surface. |

The package directory is the installed plugin target. Repository tests and
tooling live in [`../../tests/`](../../tests/) and
[`../../pyproject.toml`](../../pyproject.toml).

## Related guides

- [Architecture and runtime](architecture.md)
- [Review and curation](review-and-curation.md)
- [Tool, CLI, and configuration reference](reference.md)
- [Operations, safety, and troubleshooting](operations.md)
