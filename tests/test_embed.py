"""EmbedIndex tests run against a fake fastembed: deterministic hashed
bag-of-words vectors with a synonym table, so semantic-only recall is
testable without the real model or network."""

import hashlib
import sys
import time
import types

import pytest

from memory_wiki import search
from memory_wiki.embed import (
    EmbedIndex, record_key, record_text, unit_key, unit_text,
)
from memory_wiki.index import WikiIndex

SYNONYMS = {"automobile": "car", "auto": "car"}
DIMS = 64
EMBED_BATCHES = []


def fake_vec(text):
    v = [0.0] * DIMS
    for tok in text.lower().replace("|", " ").split():
        tok = SYNONYMS.get(tok.strip(".,!?"), tok.strip(".,!?"))
        i = int(hashlib.md5(tok.encode()).hexdigest(), 16) % DIMS
        v[i] += 1.0
    return v


class FakeModel:
    def __init__(self, model_name=""):
        self.model_name = model_name

    def embed(self, texts):
        texts = list(texts)
        EMBED_BATCHES.append(texts)
        return iter([fake_vec(t) for t in texts])


@pytest.fixture
def fake_fastembed(monkeypatch):
    mod = types.ModuleType("fastembed")
    mod.TextEmbedding = FakeModel
    monkeypatch.setitem(sys.modules, "fastembed", mod)
    EMBED_BATCHES.clear()
    return mod


def wait_for(cond, timeout=3.0, what="condition"):
    t0 = time.time()
    while not cond():
        assert time.time() - t0 < timeout, f"timed out waiting for {what}"
        time.sleep(0.01)


def wait_synced(ei, idx):
    def done():
        recs = [r for r in idx.records("all") if record_text(r)]
        with ei._lock:
            return all(record_key(r) in ei._vecs for r in recs)
    wait_for(lambda: ei.ready or ei.failed, what="worker start")
    assert not ei.failed
    wait_for(done, what="vector sync")


@pytest.fixture
def car_wiki(fixture_wiki, make_page):
    make_page(fixture_wiki, "concepts/car-upkeep.md", "concept", "Car upkeep",
              body="Wash the car weekly; the car wants care, car wax too.\n")
    idx = WikiIndex(fixture_wiki)
    idx.refresh()
    return fixture_wiki, idx


def test_semantic_hit_without_lexical_overlap(fake_fastembed, car_wiki,
                                              cfg, tmp_path):
    _root, idx = car_wiki
    # lexical scoring alone cannot connect 'automobile' to the car page
    assert not any(rec.rel == "concepts/car-upkeep.md" for _s, rec in
                   search.score_records(idx, "automobile"))
    ei = EmbedIndex(idx, cache_path=tmp_path / "emb.db")
    ei.start()
    wait_synced(ei, idx)
    scored = search.hybrid_scores(idx, ei, "automobile", corpus="pages")
    assert scored and scored[0][1].rel == "concepts/car-upkeep.md"
    block = search.prefetch_block(idx, cfg, "automobile", embedder=ei)
    assert "[concepts/car-upkeep.md]" in block
    ei.shutdown()


def test_hybrid_degrades_without_dependency(monkeypatch, car_wiki, tmp_path):
    monkeypatch.setitem(sys.modules, "fastembed", None)  # forces ImportError
    _root, idx = car_wiki
    ei = EmbedIndex(idx, cache_path=tmp_path / "emb.db")
    ei.start()
    wait_for(lambda: ei.failed, what="import failure")
    assert ei.scores("anything", list(idx.records("all"))) == {}
    lexical = search.score_records(idx, "car wax")
    hybrid = search.hybrid_scores(idx, ei, "car wax")
    assert [r.rel for _s, r in hybrid] == [r.rel for _s, r in lexical]
    ei.shutdown()


def test_cache_survives_restart_and_model_change(fake_fastembed, car_wiki,
                                                 tmp_path):
    _root, idx = car_wiki
    cache = tmp_path / "emb.db"
    ei1 = EmbedIndex(idx, cache_path=cache)
    ei1.start()
    wait_synced(ei1, idx)
    first_run = sum(len(b) for b in EMBED_BATCHES)
    assert first_run > 0 and cache.is_file()
    ei1.shutdown()

    EMBED_BATCHES.clear()
    ei2 = EmbedIndex(idx, cache_path=cache)
    ei2.start()
    wait_synced(ei2, idx)
    assert EMBED_BATCHES == []  # everything served from the cache
    ei2.shutdown()

    ei3 = EmbedIndex(idx, model="some-other-model", cache_path=cache)
    ei3.start()
    wait_synced(ei3, idx)
    assert sum(len(b) for b in EMBED_BATCHES) == first_run  # full re-embed
    ei3.shutdown()


def test_deep_body_edit_reembeds_full_content(fake_fastembed, fixture_wiki,
                                               make_page, tmp_path):
    rel = "concepts/deep-content.md"
    filler = " ".join(f"padding{i}" for i in range(80))
    make_page(fixture_wiki, rel, "concept", "Deep content",
              body=f"{filler}\n" + "automobile " * 30)
    idx = WikiIndex(fixture_wiki)
    idx.refresh()
    before = idx.pages[rel]
    assert "automobile" not in before.body_head
    old_key = record_key(before)

    ei = EmbedIndex(idx, cache_path=tmp_path / "emb.db")
    ei.start()
    wait_synced(ei, idx)
    EMBED_BATCHES.clear()

    make_page(fixture_wiki, rel, "concept", "Deep content",
              body=f"{filler}\n" + "bicycle " * 30)
    idx.refresh()
    after = idx.pages[rel]
    new_key = record_key(after)
    assert new_key != old_key
    assert "bicycle" not in after.body_head
    ei.request_sync()

    def replaced():
        with ei._lock:
            return new_key in ei._vecs and old_key not in ei._vecs

    wait_for(replaced, what="deep-content re-embed")
    assert any(record_text(after) in batch for batch in EMBED_BATCHES)
    assert rel in ei.scores("bicycle", [after])
    ei.shutdown()


def test_identical_content_at_different_paths_has_distinct_records(
        fake_fastembed, fixture_wiki, make_page, tmp_path):
    rels = ("concepts/shared-a.md", "concepts/shared-b.md")
    for rel in rels:
        make_page(fixture_wiki, rel, "concept", "Shared knowledge",
                  body="car " * 20)
    idx = WikiIndex(fixture_wiki)
    idx.refresh()
    records = [idx.pages[rel] for rel in rels]
    assert record_text(records[0]) == record_text(records[1])
    assert record_key(records[0]) != record_key(records[1])

    ei = EmbedIndex(idx, cache_path=tmp_path / "emb.db")
    ei.start()
    wait_synced(ei, idx)
    assert set(ei.scores("automobile", records)) == set(rels)
    with ei._lock:
        assert all(record_key(rec) in ei._vecs for rec in records)
    ei.shutdown()


def test_provider_respects_embeddings_flag(fixture_wiki, tmp_path):
    import memory_wiki
    p = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki), "embeddings": False})
    p.initialize("sess", hermes_home=str(tmp_path), platform="cli")
    assert p.embed is None
    ro = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki)})
    ro.initialize("sub", hermes_home=str(tmp_path), platform="cli",
                  agent_context="subagent")
    assert ro.embed is None  # read-only contexts stay lexical


def test_embedding_cache_indexes_claim_units(
        fake_fastembed, fixture_wiki, make_page, tmp_path):
    path = make_page(
        fixture_wiki, "concepts/unit-car.md", "concept", "Vehicle guidance",
        body="A short summary.\n")
    from memory_wiki import wikifs
    fm, body = wikifs.parse_frontmatter(path.read_text(encoding="utf-8"))
    fm["claims"] = [{
        "id": "claim.vehicle.care",
        "text": "car car car car car car car car car car maintenance",
        "evidence": [],
    }]
    wikifs.atomic_write(path, wikifs.render_page(fm, body))
    index = WikiIndex(fixture_wiki)
    index.refresh()
    unit = next(
        unit for unit in index.units("pages", granularity="claim")
        if unit.page_ref == "concepts/unit-car.md")
    assert "maintenance" in unit_text(unit)

    embed = EmbedIndex(index, cache_path=tmp_path / "unit-emb.db")
    embed.start()
    wait_for(lambda: embed.ready or embed.failed, what="unit worker start")
    assert not embed.failed

    def synced():
        with embed._lock:
            return unit_key(unit) in embed._vecs

    wait_for(synced, what="unit vector sync")
    scored = search.hybrid_unit_scores(
        index, embed, "automobile", granularity="claim")
    assert scored and scored[0][1].ref == unit.ref
    embed.shutdown()
