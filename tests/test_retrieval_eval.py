from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from memory_wiki import schema, search
from memory_wiki.index import WikiIndex


@dataclass(frozen=True)
class QueryCase:
    name: str
    query: str
    acceptable_refs: frozenset[str]
    contract: str
    corpus: str = "pages"
    type_filter: str | None = None


@dataclass(frozen=True)
class RetrievalMetrics:
    recall_at_1: float
    recall_at_3: float
    mean_reciprocal_rank: float


@dataclass(frozen=True)
class Evaluation:
    metrics: RetrievalMetrics
    rankings: dict[str, tuple[str, ...]]
    relevant_ranks: dict[str, int | None]


def evaluate_rankings(
    cases: tuple[QueryCase, ...], rankings: dict[str, tuple[str, ...]]
) -> Evaluation:
    """Evaluate ranked refs against the first acceptable result for each case."""
    relevant_ranks: dict[str, int | None] = {}
    for case in cases:
        rank = next(
            (
                position
                for position, ref in enumerate(rankings[case.name], start=1)
                if ref in case.acceptable_refs
            ),
            None,
        )
        relevant_ranks[case.name] = rank

    count = len(cases)
    metrics = RetrievalMetrics(
        recall_at_1=sum(rank == 1 for rank in relevant_ranks.values()) / count,
        recall_at_3=sum(
            rank is not None and rank <= 3 for rank in relevant_ranks.values()
        )
        / count,
        mean_reciprocal_rank=sum(
            0.0 if rank is None else 1.0 / rank for rank in relevant_ranks.values()
        )
        / count,
    )
    return Evaluation(metrics, rankings, relevant_ranks)


def evaluate(index: WikiIndex, cases: tuple[QueryCase, ...]) -> Evaluation:
    """Run the current lexical scorer and preserve its observable ordering."""
    rankings = {}
    for case in cases:
        scored = search.score_records(
            index,
            case.query,
            corpus=case.corpus,
            type_filter=case.type_filter,
        )
        rankings[case.name] = tuple(record.rel for _score, record in scored)
    return evaluate_rankings(cases, rankings)


CASES = (
    QueryCase(
        "title",
        "lamport clocks",
        frozenset({"concepts/lamport-clocks.md"}),
        "exact title terms",
    ),
    QueryCase(
        "alias",
        "logical timestamps",
        frozenset({"concepts/lamport-clocks.md"}),
        "frontmatter alias",
    ),
    QueryCase(
        "topic",
        "distributed systems",
        frozenset({"atlases/distributed-reliability.md"}),
        "frontmatter topic",
    ),
    QueryCase(
        "heading",
        "failure recovery playbook",
        frozenset({"atlases/distributed-reliability.md"}),
        "section heading",
    ),
    QueryCase(
        "body",
        "write ahead log",
        frozenset({"concepts/durable-writes.md"}),
        "ordinary body prose",
    ),
    QueryCase(
        "deep_body",
        "merkle reconciliation",
        frozenset({"concepts/anti-entropy.md"}),
        "body terms beyond the result snippet head",
    ),
    QueryCase(
        "multi_term_tf_distractor",
        "lease renewal jitter",
        frozenset({"concepts/lease-renewal.md"}),
        "all query terms despite a high-frequency partial-match distractor",
    ),
    QueryCase(
        "multi_term_field_distractor",
        "adaptive retry budget",
        frozenset({"concepts/retry-budgeting.md"}),
        "all query terms despite a title-boosted partial-match distractor",
    ),
    QueryCase(
        "profile_corpus",
        "prefers split keyboards",
        frozenset({"profile/user.md"}),
        "profile-only retrieval",
        corpus="profile",
    ),
    QueryCase(
        "journal_corpus",
        "museum accessibility audit",
        frozenset({"journal/2026-06-15.md"}),
        "journal-only retrieval",
        corpus="journal",
    ),
    QueryCase(
        "all_corpus",
        "redwood deployment decision",
        frozenset({"journal/2026-06-16.md"}),
        "cross-corpus retrieval",
        corpus="all",
    ),
    QueryCase(
        "type_filter",
        "consensus quorum",
        frozenset({"claims/quorum-requires-majority.md"}),
        "knowledge type filter",
        type_filter="claim",
    ),
    QueryCase(
        "multiple_acceptable",
        "incident handoff checklist",
        frozenset(
            {
                "refs/incident-handoff.md",
                "concepts/on-call-handoff.md",
            }
        ),
        "either authoritative page is useful",
    ),
    QueryCase(
        "deterministic_tie",
        "quasar index",
        frozenset({"concepts/alpha-quasar.md"}),
        "equal scores break ties by stable ref ordering",
    ),
)


def _build_benchmark_wiki(tmp_path, make_page):
    root = tmp_path / "retrieval-wiki"
    for directory in schema.DIR_TYPE:
        (root / directory).mkdir(parents=True)

    make_page(
        root,
        "concepts/lamport-clocks.md",
        "concept",
        "Lamport clocks",
        topics=("event-ordering",),
        extras={"aliases": ["logical timestamps"]},
        body="A compact ordering mechanism for concurrent events.\n",
    )
    make_page(
        root,
        "concepts/distributed-glossary.md",
        "concept",
        "Distributed glossary",
        topics=("reference",),
        body=(
            "Distributed systems terminology includes Lamport clocks and a logical "
            "timestamp for ordering events.\n"
        ),
    )
    make_page(
        root,
        "atlases/distributed-reliability.md",
        "atlas",
        "Distributed reliability",
        topics=("distributed-systems",),
        body="## Failure recovery playbook\n\nDetection, containment, and repair.\n",
    )
    make_page(
        root,
        "concepts/failure-catalog.md",
        "concept",
        "Failure catalog",
        topics=("operations",),
        body="Failure recovery examples for operators; each playbook links onward.\n",
    )
    make_page(
        root,
        "concepts/durable-writes.md",
        "concept",
        "Durable database writes",
        topics=("storage",),
        body="A write ahead log preserves committed changes before data pages flush.\n",
    )
    make_page(
        root,
        "concepts/database-glossary.md",
        "concept",
        "Database glossary",
        topics=("storage",),
        body="Write paths, transaction records, and log rotation are defined here.\n",
    )
    make_page(
        root,
        "concepts/anti-entropy.md",
        "concept",
        "Anti-entropy repair",
        topics=("replication",),
        body=("Background context about replica repair and divergence. " * 12)
        + "The final protocol uses merkle reconciliation to isolate changed ranges.\n",
    )
    make_page(
        root,
        "concepts/tree-hashing.md",
        "concept",
        "Tree hashing",
        topics=("storage",),
        body="Merkle structures summarize immutable blocks.\n",
    )
    make_page(
        root,
        "concepts/lease-renewal.md",
        "concept",
        "Distributed lease operations",
        topics=("coordination",),
        body="Add randomized lease renewal jitter to prevent synchronized traffic.\n",
    )
    make_page(
        root,
        "concepts/lease-reference.md",
        "concept",
        "Lease reference",
        topics=("coordination",),
        body=("lease " * 80) + "expiration and ownership semantics.\n",
    )
    make_page(
        root,
        "concepts/retry-budgeting.md",
        "concept",
        "Backoff controls",
        topics=("resilience",),
        body="An adaptive retry budget bounds amplification during partial failure.\n",
    )
    make_page(
        root,
        "concepts/adaptive-retries.md",
        "concept",
        "Adaptive retry",
        topics=("resilience",),
        body="Adaptive clients change delay after errors.\n",
    )
    make_page(
        root,
        "profile/user.md",
        "profile",
        "User profile",
        topics=("profile",),
        body="## Preferences\n\nPrefers split keyboards for daily development.\n",
    )
    make_page(
        root,
        "profile/agent.md",
        "profile",
        "Agent profile",
        topics=("profile",),
        body="## Working agreements\n\nKeep responses concise.\n",
    )
    make_page(
        root,
        "journal/2026-06-15.md",
        "journal",
        "2026-06-15",
        topics=("journal",),
        body="Completed the museum accessibility audit with the facilities team.\n",
    )
    make_page(
        root,
        "journal/2026-06-16.md",
        "journal",
        "2026-06-16",
        topics=("journal",),
        body="Recorded the redwood deployment decision after the release review.\n",
    )
    make_page(
        root,
        "concepts/redwood-runbook.md",
        "concept",
        "Redwood runbook",
        topics=("deployment",),
        body=("Redwood deployment " * 8) + "commands and rollback steps.\n",
    )
    make_page(
        root,
        "claims/quorum-requires-majority.md",
        "claim",
        "A quorum requires a majority",
        topics=("consensus",),
        body="Consensus safety depends on intersecting quorum sets.\n",
    )
    make_page(
        root,
        "claims/consensus-is-contextual.md",
        "claim",
        "Consensus is contextual",
        topics=("coordination",),
        body="Quorum policy depends on the failure model.\n",
    )
    make_page(
        root,
        "concepts/consensus-quorum.md",
        "concept",
        "Consensus quorum",
        topics=("coordination",),
        body="A general introduction.\n",
    )
    make_page(
        root,
        "refs/incident-handoff.md",
        "ref",
        "Incident handoff checklist",
        topics=("operations",),
        body="Owner, timeline, mitigations, and next update.\n",
    )
    make_page(
        root,
        "concepts/on-call-handoff.md",
        "concept",
        "On-call handoff",
        topics=("operations",),
        body="Use the incident checklist when transferring response ownership.\n",
    )
    for rel in ("concepts/alpha-quasar.md", "concepts/beta-quasar.md"):
        make_page(
            root,
            rel,
            "concept",
            "Astronomy notes",
            topics=("astronomy",),
            body="The quasar index catalogs distant active galactic nuclei.\n",
        )

    index = WikiIndex(root)
    index.refresh()
    return index


@pytest.fixture
def benchmark_index(tmp_path, make_page):
    return _build_benchmark_wiki(tmp_path, make_page)


def test_evaluator_metrics_use_first_acceptable_rank():
    cases = (
        QueryCase("first", "q1", frozenset({"a"}), "test"),
        QueryCase("third", "q2", frozenset({"c", "also-c"}), "test"),
        QueryCase("miss", "q3", frozenset({"missing"}), "test"),
    )
    result = evaluate_rankings(
        cases,
        {
            "first": ("a", "b"),
            "third": ("x", "y", "also-c", "c"),
            "miss": ("x",),
        },
    )

    assert result.relevant_ranks == {"first": 1, "third": 3, "miss": None}
    assert result.metrics.recall_at_1 == pytest.approx(1 / 3)
    assert result.metrics.recall_at_3 == pytest.approx(2 / 3)
    assert result.metrics.mean_reciprocal_rank == pytest.approx((1 + 1 / 3) / 3)


def test_snippet_falls_back_to_body_head_for_legacy_records():
    record = SimpleNamespace(body_head="Legacy text with fallbacktoken near the end.")

    result = search.snippet(record, "fallbacktoken")

    assert "fallbacktoken" in result
    assert len(result) <= search.SNIPPET_LEN


def test_lexical_retrieval_with_term_coverage(benchmark_index):
    result = evaluate(benchmark_index, CASES)
    metrics = result.metrics
    print(
        "retrieval improved: "
        f"queries={len(CASES)} "
        f"recall@1={metrics.recall_at_1:.6f} "
        f"recall@3={metrics.recall_at_3:.6f} "
        f"mrr={metrics.mean_reciprocal_rank:.6f}"
    )
    for case in CASES:
        print(
            f"{case.name}: relevant_rank={result.relevant_ranks[case.name]} "
            f"top3={result.rankings[case.name][:3]}"
        )
    deep_score, deep_record = search.score_records(
        benchmark_index, "merkle reconciliation"
    )[0]
    deep_result = search.result_dict(
        deep_record, deep_score, "merkle reconciliation"
    )
    print(
        "deep_body presentation: "
        f"snippet_has_query={'merkle' in deep_result['snippet'].lower()}"
    )

    required_contracts = {
        "title",
        "alias",
        "topic",
        "heading",
        "body",
        "deep_body",
        "multi_term_tf_distractor",
        "multi_term_field_distractor",
        "profile_corpus",
        "journal_corpus",
        "all_corpus",
        "type_filter",
        "multiple_acceptable",
        "deterministic_tie",
    }
    assert len(CASES) >= 12
    assert {case.name for case in CASES} == required_contracts
    assert {case.corpus for case in CASES} == {
        "pages",
        "profile",
        "journal",
        "all",
    }
    assert any(case.type_filter for case in CASES)
    deep_record = benchmark_index.pages["concepts/anti-entropy.md"]
    assert deep_record.body.index("merkle reconciliation") > 400
    assert metrics.recall_at_1 == pytest.approx(1.0)
    assert metrics.recall_at_3 == pytest.approx(1.0)
    assert metrics.mean_reciprocal_rank == pytest.approx(1.0)
    prior_rank_1_winners = {
        "title": "concepts/lamport-clocks.md",
        "alias": "concepts/lamport-clocks.md",
        "topic": "atlases/distributed-reliability.md",
        "heading": "atlases/distributed-reliability.md",
        "body": "concepts/durable-writes.md",
        "deep_body": "concepts/anti-entropy.md",
        "profile_corpus": "profile/user.md",
        "journal_corpus": "journal/2026-06-15.md",
        "type_filter": "claims/quorum-requires-majority.md",
        "multiple_acceptable": "refs/incident-handoff.md",
        "deterministic_tie": "concepts/alpha-quasar.md",
    }
    assert {
        name: result.rankings[name][0] for name in prior_rank_1_winners
    } == prior_rank_1_winners
    assert any(
        token in deep_result["snippet"].lower()
        for token in search.tokenize("merkle reconciliation")
    )
    assert len(deep_result["snippet"]) <= search.SNIPPET_LEN
    assert result.rankings["deterministic_tie"][:2] == (
        "concepts/alpha-quasar.md",
        "concepts/beta-quasar.md",
    )

