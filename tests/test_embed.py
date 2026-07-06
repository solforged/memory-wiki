"""EmbedIndex tests run against a fake fastembed: deterministic hashed
bag-of-words vectors with a synonym table, so semantic-only recall is
testable without the real model or network."""

import hashlib
import sys
import time
import types

import pytest

from memory_wiki import search
from memory_wiki.embed import EmbedIndex, record_key, record_text
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
            return all(record_key(record_text(r)) in ei._vecs for r in recs)
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
