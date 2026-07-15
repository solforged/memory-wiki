# OpenClaw parity

Compared against current OpenClaw documentation and source on 2026-07-14.
This project is a combined Hermes active-memory provider and wiki. OpenClaw
separates those roles: `memory-core` or QMD owns active memory, and
`memory-wiki` is a companion compiler.

| Capability | This project | Notes |
|---|---|---|
| Markdown source of truth | Implemented | Profile, journal, typed pages, candidate inbox |
| Review-first promotion | Implemented | Approval, override, reject, supersede, audit log |
| Recall telemetry | Implemented | Bounded state with hashed queries and sessions |
| Deep promotion gates | Implemented | Six weighted signals plus three independent gates |
| Dream explain/report | Implemented | Dry-run plan, safe apply, `DREAMS.md` report |
| QMD sidecar | Implemented | Private state, updates, embeds, fallback, backoff |
| Credential filtering | Partial | Common signatures across automatic and model-facing writes |
| Light and REM phases | Missing | No reflection model or phase reinforcement yet |
| Managed dream schedule | Missing | CLI is manual; Hermes scheduler integration is needed |
| Compiled machine digest | Missing | Search reads Markdown records directly |
| Structured claim evidence | Partial | Claim pages preserve evidence, but lack nested claim objects |
| Generated health dashboards | Missing | No contradiction, stale, confidence, or provenance reports |
| Bridge and per-agent vaults | Not applicable | This provider directly owns its configured wiki |

## Next tranche

1. Add a deterministic compiler that emits a compact JSON digest and claim
   JSONL without replacing human-authored blocks.
2. Generate contradiction, stale-memory, low-confidence, and provenance
   dashboards from that compiler.
3. Add opt-in scheduled light, REM, and deep sweeps only after Hermes exposes a
   stable scheduler and background-model seam.

The first two belong together. Dashboards should consume the same normalized
compiler output used by search, rather than independently scraping Markdown.

## References

- <https://docs.openclaw.ai/plugins/memory-wiki>
- <https://docs.openclaw.ai/concepts/dreaming>
- <https://docs.openclaw.ai/concepts/memory-qmd>
