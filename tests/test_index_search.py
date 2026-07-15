import json
import os
import time
from pathlib import Path

from memory_wiki import schema, search, wikifs
from memory_wiki.index import WikiIndex, tokenize


def make_candidate(root, cand_id, status="pending", kind="preference",
                   content="User prefers uv over pip.", slug="prefers-uv",
                   created=None):
    if created is None:
        from datetime import date
        created = date.today().isoformat()  # keep age-sensitive asserts stable
    fm = {"id": cand_id, "status": status, "kind": kind, "target": "",
          "section": "", "confidence": "plausible", "origin": "sync_turn",
          "session": "s1", "turn": 1, "created": created,
          "reviewed": "", "promoted_to": "", "fields": {}, "hash": "h" + cand_id[-2:]}
    body = f"{content}\n\n## Evidence\n\n> [user, turn 1] quote\n"
    path = root / schema.INBOX_DIR / f"{cand_id}-{slug}.md"
    wikifs.atomic_write(path, wikifs.render_page(fm, body))
    return path


CID1 = "20260705-120000-abcd1234-01"
CID2 = "20260705-120001-abcd1234-02"


def test_index_refresh_add_change_delete(fixture_wiki, make_page):
    idx = WikiIndex(fixture_wiki)
    idx.refresh()
    assert "concepts/zettelkasten.md" in idx.pages
    n0 = len(idx.pages)
    p = make_page(fixture_wiki, "concepts/spaced-repetition.md", "concept",
                  "Spaced repetition", body="Review at growing intervals.\n")
    idx.refresh()
    assert len(idx.pages) == n0 + 1
    # content change with forced mtime bump is reparsed
    fm, body = wikifs.parse_frontmatter(p.read_text())
    wikifs.atomic_write(p, wikifs.render_page(fm, "Anki flashcards forever.\n"))
    os.utime(p, ns=(time.time_ns(), time.time_ns() + 1_000_000))
    idx.refresh()
    assert "anki" in idx.pages["concepts/spaced-repetition.md"].tokens
    p.unlink()
    idx.refresh()
    assert "concepts/spaced-repetition.md" not in idx.pages


def test_index_keeps_full_body_without_expanding_lexical_cap(
        fixture_wiki, make_page):
    rel = "concepts/long-body.md"
    body = ("padding " * 3_000) + "deepuniqueterm\n"
    make_page(fixture_wiki, rel, "concept", "Long body", body=body)
    idx = WikiIndex(fixture_wiki)
    idx.refresh()
    rec = idx.pages[rel]
    assert rec.body.endswith("deepuniqueterm")
    assert "deepuniqueterm" not in rec.tokens


def test_index_candidates_and_hashes(fixture_wiki):
    (fixture_wiki / schema.INBOX_DIR).mkdir()
    make_candidate(fixture_wiki, CID1)
    make_candidate(fixture_wiki, CID2, status="rejected", slug="other")
    idx = WikiIndex(fixture_wiki)
    idx.refresh()
    cands = list(idx.records("candidates"))
    assert len(cands) == 2
    assert len(idx.pending_candidates()) == 1
    assert idx.pending_candidates()[0].cand_id == CID1
    assert idx.candidate_hashes == {"h01", "h02"}
    assert all("Evidence" not in rec.body for rec in cands)
    assert all("quote" not in rec.tokens for rec in cands)


def test_taste_page_indexed_and_searchable(fixture_wiki, make_page):
    make_page(fixture_wiki, "tastes/tastes.md", "taste", "Tastes",
              topics=("profile", "music"),
              body="Strong signal for synthwave and dark ambient.\n")
    idx = WikiIndex(fixture_wiki)
    idx.refresh()
    assert idx.counts().get("taste") == 1
    scored = search.score_records(idx, "synthwave dark ambient")
    assert scored and scored[0][1].rel == "tastes/tastes.md"


def test_corpus_and_type_filters(fixture_wiki):
    idx = WikiIndex(fixture_wiki)
    idx.refresh()
    assert all(r.ntype in schema.KNOWLEDGE_TYPES for r in idx.records("pages"))
    assert list(idx.records("journal")) == []
    only_claims = list(idx.records("pages", type_filter="claim"))
    assert [r.ntype for r in only_claims] == ["claim"]


def test_scoring_title_beats_body(fixture_wiki, make_page):
    make_page(fixture_wiki, "concepts/uv-package-manager.md", "concept",
              "uv package manager", topics=("python",),
              body="Fast Python package management from Astral.\n")
    make_page(fixture_wiki, "concepts/python-packaging.md", "concept",
              "Python packaging", topics=("python",),
              body="Ecosystem overview. Mentions uv once.\n")
    idx = WikiIndex(fixture_wiki)
    idx.refresh()
    scored = search.score_records(idx, "uv package manager")
    assert scored[0][1].rel == "concepts/uv-package-manager.md"


def test_search_no_hits_and_stopwords(fixture_wiki):
    idx = WikiIndex(fixture_wiki)
    idx.refresh()
    assert search.score_records(idx, "quantum blockchain") == []
    assert search.score_records(idx, "the and of") == []


def test_result_dict_claim_metadata(fixture_wiki):
    idx = WikiIndex(fixture_wiki)
    idx.refresh()
    scored = search.score_records(idx, "small tools frameworks")
    assert scored
    d = search.result_dict(scored[0][1], scored[0][0], "small tools")
    assert d["ref"] == "claims/small-tools-beat-frameworks.md"
    assert d["confidence"] == "likely" and d["maturity"] == "seedling"


def test_prefetch_block_shape_and_budget(fixture_wiki, cfg, make_page):
    (fixture_wiki / schema.INBOX_DIR).mkdir()
    make_candidate(fixture_wiki, CID1, content="User prefers uv over pip.")
    make_page(fixture_wiki, "concepts/uv-package-manager.md", "concept",
              "uv package manager", body="Fast Python package tool.\n")
    idx = WikiIndex(fixture_wiki)
    idx.refresh()
    block = search.prefetch_block(idx, cfg, "does the user prefer uv or pip?")
    assert block.startswith("## Wiki recall")
    assert "[concepts/uv-package-manager.md]" in block
    assert f"[cand {CID1}] (unreviewed preference)" in block
    assert len(block) <= cfg.prefetch_char_budget
    assert search.prefetch_block(idx, cfg, "zzz nothing matches") == ""


def test_prefetch_excludes_profile_and_nonpending(fixture_wiki, cfg):
    from memory_wiki import scaffold
    scaffold.ensure_runtime(cfg)
    from memory_wiki.layers import write_profile
    write_profile(cfg, "user", "Prefers uv over pip", section="## Preferences")
    make_candidate(fixture_wiki, CID2, status="rejected",
                   content="User prefers uv over pip.", slug="rej")
    idx = WikiIndex(fixture_wiki)
    idx.refresh()
    block = search.prefetch_block(idx, cfg, "prefers uv pip")
    assert "profile/user.md" not in block
    assert CID2 not in block


def test_prefetch_candidate_age_and_staleness(fixture_wiki, cfg):
    from datetime import date, timedelta
    (fixture_wiki / schema.INBOX_DIR).mkdir()
    fresh = (date.today() - timedelta(days=3)).isoformat()
    stale = (date.today() - timedelta(days=45)).isoformat()
    make_candidate(fixture_wiki, CID1, created=fresh)
    make_candidate(fixture_wiki, CID2, created=stale, slug="stale",
                   content="User prefers uv over pip always.")
    idx = WikiIndex(fixture_wiki)
    idx.refresh()
    block = search.prefetch_block(idx, cfg, "prefers uv pip")
    assert f"[cand {CID1}]" in block and "staged 3d ago" in block
    assert CID2 not in block  # older than candidate_stale_days


def test_tokenize():
    assert "zettelkasten" in tokenize("The Zettelkasten, explained!")
    assert "the" not in tokenize("the and of")


def test_provider_read_path_end_to_end(fixture_wiki, tmp_path):
    import memory_wiki
    provider = memory_wiki.MemoryWikiProvider(
        overrides={"wiki_root": str(fixture_wiki)})
    provider.initialize("sess-1", hermes_home=str(tmp_path), platform="cli",
                        agent_context="primary")
    names = [s["name"] for s in provider.get_tool_schemas()]
    assert names == ["wiki_search", "wiki_get", "wiki_apply"]

    out = json.loads(provider.handle_tool_call(
        "wiki_search", {"query": "zettelkasten"}))
    assert out["ok"] and out["results"][0]["ref"] == "concepts/zettelkasten.md"

    got = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": "concepts/zettelkasten.md"}))
    assert got["ok"] and "Atomic notes" in got["content"]
    window = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": "concepts/zettelkasten.md",
                     "start_line": 2, "max_lines": 2}))
    assert window["start_line"] == 2
    assert len(window["content"].splitlines()) == 2

    status = json.loads(provider.handle_tool_call("wiki_get", {"ref": "status"}))
    assert status["ok"] and status["pages"]["concept"] >= 1
    queue = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": "review-queue"}))
    assert queue["ok"] and queue["count"] == 0

    miss = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": "20260101-000000-deadbeef-01"}))
    assert not miss["ok"]
    escape = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": "../secrets.md"}))
    assert not escape["ok"]

    block = provider.system_prompt_block()
    assert "# Memory wiki" in block and "wiki_search" in block


def test_provider_prefetch_caches(fixture_wiki, tmp_path, monkeypatch):
    import memory_wiki
    provider = memory_wiki.MemoryWikiProvider(
        overrides={"wiki_root": str(fixture_wiki)})
    provider.initialize("sess-1", hermes_home=str(tmp_path), platform="cli")
    calls = []
    real = search.prefetch_results_block
    monkeypatch.setattr(search, "prefetch_results_block",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    b1 = provider.prefetch("zettelkasten notes", session_id="s")
    b2 = provider.prefetch("zettelkasten notes", session_id="s")
    assert b1 == b2 and len(calls) == 1
    written = json.loads(provider.handle_tool_call(
        "wiki_apply", {"action": "append_journal", "content": "Cache reset"}))
    assert written["ok"]
    provider.prefetch("zettelkasten notes", session_id="s")
    assert len(calls) == 2
    provider.prefetch("different query entirely", session_id="s")
    assert len(calls) == 3
    assert provider.prefetch("", session_id="s") == ""


def test_prefetch_records_only_candidate_results_injected(
        fixture_wiki, tmp_path, monkeypatch):
    import memory_wiki
    from memory_wiki import recall

    provider = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki), "embeddings": False,
        "prefetch_limit": 1})
    provider.initialize("recall-session", hermes_home=str(tmp_path),
                        platform="cli")
    hidden = {
        "type": "candidate", "id": "hidden", "status": "pending",
        "kind": "note", "created": wikifs.today(), "snippet": "hidden",
    }
    stale = dict(hidden, id="stale", created="2000-01-01")
    page = {"type": "concept", "ref": "concepts/zettelkasten.md",
            "title": "Zettelkasten", "snippet": "visible page"}
    monkeypatch.setattr(provider, "search_results",
                        lambda *args, **kwargs: [stale, page, hidden])
    assert "visible page" in provider.prefetch("first", session_id="s")
    assert recall.metrics(provider.cfg, "stale")["recall_count"] == 0
    assert recall.metrics(provider.cfg, "hidden")["recall_count"] == 0

    provider.clear_prefetch_cache()
    monkeypatch.setattr(provider, "search_results",
                        lambda *args, **kwargs: [hidden])
    provider.prefetch("second", session_id="s")
    assert recall.metrics(provider.cfg, "hidden")["recall_count"] == 1


def test_recall_telemetry_failures_do_not_break_search_or_prefetch(
        fixture_wiki, tmp_path, monkeypatch):
    import memory_wiki
    from memory_wiki import recall

    provider = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki), "embeddings": False})
    provider.initialize("telemetry-session", hermes_home=str(tmp_path),
                        platform="cli")
    monkeypatch.setattr(recall, "record",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            OSError("telemetry unavailable")))

    results = provider.search_results("zettelkasten", limit=1)
    assert results and results[0]["ref"] == "concepts/zettelkasten.md"

    visible = {
        "type": "candidate", "id": "visible", "status": "pending",
        "kind": "note", "created": wikifs.today(),
        "snippet": "visible candidate",
    }
    monkeypatch.setattr(provider, "search_results",
                        lambda *args, **kwargs: [visible])
    assert "visible candidate" in provider.prefetch("visible", session_id="s")


def test_mutation_invalidation_is_fail_soft(fixture_wiki, tmp_path, monkeypatch):
    import memory_wiki

    provider = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki), "embeddings": False})
    provider.initialize("mutation-session", hermes_home=str(tmp_path),
                        platform="cli")
    provider._prefetch_cache[("s", "query")] = "stale"
    calls = []

    def fail(label):
        def run(*_args):
            calls.append(label)
            raise OSError(label)
        return run

    monkeypatch.setattr(provider.index, "refresh", fail("index"))
    provider.embed = type("Embed", (), {"request_sync": fail("embed")})()
    provider.qmd = type("Qmd", (), {"invalidate": fail("qmd")})()
    monkeypatch.setattr(provider, "recompile_prompt_block", fail("prompt"))

    out = json.loads(provider.handle_tool_call(
        "wiki_apply", {"action": "append_journal", "content": "Durable"}))
    assert out["ok"]
    assert provider._prefetch_cache == {}
    assert calls == ["index", "embed", "qmd", "prompt"]


def test_prefetch_does_not_cache_results_crossing_a_mutation(
        fixture_wiki, tmp_path, monkeypatch):
    import memory_wiki

    provider = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki), "embeddings": False})
    provider.initialize("race-session", hermes_home=str(tmp_path),
                        platform="cli")
    page = {"type": "concept", "ref": "concepts/zettelkasten.md",
            "title": "Zettelkasten", "snippet": "stale result"}

    def mutate_during_search(*args, **kwargs):
        provider.on_wiki_mutation()
        return [page]

    monkeypatch.setattr(provider, "search_results", mutate_during_search)
    assert provider.prefetch("race", session_id="s") == ""
    assert provider._prefetch_cache == {}


def test_local_relevance_is_absolute_query_coverage(fixture_wiki, tmp_path):
    import memory_wiki
    provider = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki), "embeddings": False})
    provider.initialize("coverage-session", hermes_home=str(tmp_path),
                        platform="cli")
    staged = json.loads(provider.handle_tool_call("wiki_apply", {
        "action": "stage", "kind": "note",
        "content": "Red terminal themes are preferred.",
    }))
    results = provider.search_results(
        "red unrelated alpha beta", corpus="candidates", limit=5)
    result = next(item for item in results if item["id"] == staged["id"])
    assert result["relevance"] == 0.25


def test_read_only_provider_hides_apply(fixture_wiki, tmp_path):
    import memory_wiki
    provider = memory_wiki.MemoryWikiProvider(
        overrides={"wiki_root": str(fixture_wiki)})
    provider.initialize("sess-sub", hermes_home=str(tmp_path), platform="cli",
                        agent_context="subagent")
    names = [s["name"] for s in provider.get_tool_schemas()]
    assert "wiki_apply" not in names
    out = json.loads(provider.handle_tool_call("wiki_apply", {"action": "stage"}))
    assert not out["ok"] and "read-only" in out["error"]
