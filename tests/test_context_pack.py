import pytest

from memory_wiki import context_pack


class Provider:
    def __init__(self):
        self.cfg = object()
        self.index = object()
        self.calls = []

    def search_results(self, query, **kwargs):
        self.calls.append((query, kwargs))
        corpus = kwargs["corpus"]
        if corpus == "profile":
            return [{"ref": "profile/user.md", "title": "User profile",
                     "type": "profile", "snippet": "Prefers concise plans."}]
        if corpus == "candidates":
            return [{"ref": "_inbox/c1.md", "id": "c1",
                     "type": "candidate", "status": "pending", "kind": "note",
                     "snippet": "May prefer compact release notes."}]
        return [{"ref": "concepts/testing.md", "title": "Testing",
                 "type": "concept", "snippet": "Verify the live path."}]


def test_context_pack_uses_explicit_corpora_and_disables_telemetry():
    provider = Provider()

    result = context_pack.build(
        provider, "release process", include_candidates=True, limit=10)

    assert result["corpora"] == ["pages", "profile", "candidates"]
    assert all(call[1]["record_recall"] is False for call in provider.calls)
    assert all(call[1]["source"] == "context_pack" for call in provider.calls)
    candidate = next(item for item in result["results"]
                     if item["type"] == "candidate")
    assert candidate["provisional"] is True
    assert "candidate c1, provisional pending" in result["context"]
    assert result["chars"] <= result["char_budget"]


def test_context_pack_rejects_implicit_all_corpus():
    with pytest.raises(ValueError, match="corpus"):
        context_pack.build(Provider(), "anything", corpus="all")
