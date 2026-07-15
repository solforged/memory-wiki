# OpenClaw parity

Compared against current OpenClaw documentation and source on 2026-07-14.
This project combines Hermes active memory with a companion wiki compiler.
OpenClaw splits those roles between `memory-core` or QMD and `memory-wiki`.

| Capability | Status | Notes |
|---|---|---|
| Markdown source of truth | Implemented | Profile, journal, typed pages, and candidate inbox |
| Review-first promotion | Implemented | Approval, rejection, supersession, and audit log |
| Recall telemetry | Implemented | Bounded state with hashed queries and sessions |
| QMD sidecar | Implemented | Private disposable index, maintenance, fallback, and backoff |
| Structured claims | Implemented | Nested claims, evidence, status, confidence, and freshness |
| Compiler artifacts | Implemented | Deterministic agent digest and claim JSONL |
| Health reports | Implemented | Contradictions, confidence, freshness, and provenance |
| Light, REM, and deep dreaming | Implemented | Ordered, hash-bound reinforcement; deep alone may promote |
| Deep promotion gates | Implemented | Six weighted signals plus independent score, recall, and diversity gates |
| Managed dream schedule | Implemented | One disabled-by-default Hermes no-agent cron job |
| Dream explain/report | Implemented | Dry-run plan, safe apply, and output-only `DREAMS.md` |
| Dashboard | Implemented | Read-only compiler, claims, dreaming, scheduler, queue, and QMD overview |
| Credential filtering | Partial | Common signatures cover automatic and model-facing writes |
| Bridge and per-agent vaults | Not applicable | This provider directly owns its configured wiki |

## Important boundaries

- Markdown remains authoritative. Compiler caches, dream state, QMD state, and
  the dashboard are derived or advisory.
- QMD ranks and excerpts Markdown recall. It does not own claims, compiler
  output, promotion policy, or dream state.
- Light sleep deduplicates pending memories. REM derives themes and lasting
  truths. Neither writes knowledge. Deep is the only promotion phase.
- Manual deep gates default to score `0.75`, three recalls, and two distinct
  query hashes. Scheduled deep uses fixed stricter gates: `0.80`, three, and
  three.
- Repeating an unchanged sweep does not manufacture reinforcement. Phase
  signals are bound to candidate content and changed organic recall evidence.

## Remaining differences

- The compiler emits typed-page and claim health records, not OpenClaw's full
  entity, person, organization, and relationship graph.
- Contradictions require explicit page links or a repeated claim ID with
  conflicting text or status. There is no semantic contradiction classifier.
- The dashboard is observational. Review and promotion remain CLI, tool, or
  editor workflows.
- Scheduling depends on the Hermes cron API. It fails soft when that API is not
  available.
- This single-provider design has no OpenClaw bridge migration or per-agent
  vault routing layer.

## References

- <https://docs.openclaw.ai/plugins/memory-wiki>
- <https://docs.openclaw.ai/concepts/dreaming>
- <https://docs.openclaw.ai/concepts/memory-qmd>
