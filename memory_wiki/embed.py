"""Optional semantic recall: local embeddings over the page index.

fastembed (ONNX) is imported lazily on a background worker thread, so
the plugin stays stdlib-only at import time and sessions never block on
model loading; recall degrades to lexical-only until the model is warm
or forever if the dependency is missing. Vectors live in a throwaway
sqlite cache outside the wiki, keyed by content hash; the markdown
stays the only source of truth (OpenClaw's memory index has the same
shape). Cosine is pure Python: at wiki scale (hundreds of notes,
384 dims) that is a couple of milliseconds and saves a numpy import.
"""

from __future__ import annotations

import hashlib
import logging
import math
import sqlite3
import struct
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"

# bge cosine sits ~0.55-0.65 for unrelated text; rescale so only real
# semantic proximity contributes to the hybrid score.
SIM_FLOOR = 0.6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS chunks (
    key TEXT PRIMARY KEY, rel TEXT NOT NULL, vec BLOB NOT NULL);
"""


def default_cache_path(wiki_root):
    tag = hashlib.sha1(str(wiki_root).encode("utf-8")).hexdigest()[:8]
    return Path.home() / ".cache" / "memory-wiki" / f"embeddings-{tag}.db"


def record_text(rec):
    """The text embedded for one index record (title + topics + head)."""
    return " | ".join(x for x in (
        rec.title, " ".join(rec.topics), rec.body_head) if x)


def record_key(text):
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:20]


def _pack(vec):
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob):
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _normalize(vec):
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return list(vec)
    return [x / norm for x in vec]


class EmbedIndex:
    """Embedding cache with a single background worker.

    All model calls and sqlite access happen on the worker thread; the
    search thread only reads the in-memory vector dict (under a lock)
    and queues query embeddings through the worker-guarded model lock.
    """

    def __init__(self, wiki_index, model=DEFAULT_MODEL, cache_path=None):
        self.wiki_index = wiki_index
        self.model_name = model
        self.cache_path = Path(cache_path
                               or default_cache_path(wiki_index.root))
        self.ready = False
        self.failed = False
        self._model = None
        self._model_lock = threading.Lock()
        self._vecs = {}          # key -> unit vector (list of float)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    # -- lifecycle (search thread) -----------------------------------------

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="memory-wiki-embed", daemon=True)
            self._thread.start()

    def request_sync(self):
        self._wake.set()

    def shutdown(self):
        self._stop.set()
        self._wake.set()

    # -- scoring (search thread) --------------------------------------------

    def scores(self, query, records):
        """{rel: similarity 0..1 above SIM_FLOOR} or {} while not ready."""
        if not self.ready or self.failed:
            return {}
        try:
            with self._model_lock:
                qvec = _normalize(
                    list(map(float, next(iter(self._model.embed([query]))))))
        except Exception as e:
            logger.debug("memory-wiki: query embed failed: %s", e)
            return {}
        with self._lock:
            vecs = dict(self._vecs)
        out = {}
        for rec in records:
            v = vecs.get(record_key(record_text(rec)))
            if v is None or len(v) != len(qvec):
                continue
            sim = sum(a * b for a, b in zip(qvec, v))
            if sim > SIM_FLOOR:
                out[rec.rel] = (sim - SIM_FLOOR) / (1.0 - SIM_FLOOR)
        return out

    # -- worker ---------------------------------------------------------------

    def _run(self):
        try:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name=self.model_name)
        except Exception as e:
            self.failed = True
            logger.info("memory-wiki: embeddings unavailable (%s); "
                        "recall stays lexical-only", e)
            return
        db = None
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.cache_path)
            db.executescript(_SCHEMA)
            self._check_meta(db)
            with self._lock:
                self._vecs = {key: _unpack(blob) for key, blob in
                              db.execute("SELECT key, vec FROM chunks")}
        except Exception as e:
            logger.warning("memory-wiki: embed cache unusable: %s", e)
            db = None
        self.ready = True
        self._wake.set()  # run one initial sync
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self._sync(db)
            except Exception as e:
                logger.warning("memory-wiki: embed sync failed: %s", e)
        if db is not None:
            db.close()

    def _check_meta(self, db):
        row = db.execute("SELECT value FROM meta WHERE key='model'").fetchone()
        if row and row[0] != self.model_name:
            db.execute("DELETE FROM chunks")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('model', ?)",
                   (self.model_name,))
        db.commit()

    def _sync(self, db):
        """Embed records whose content is new; drop vectors for gone ones."""
        wanted = {}
        for rec in self.wiki_index.records("all"):
            text = record_text(rec)
            if text:
                wanted[record_key(text)] = (rec.rel, text)
        with self._lock:
            missing = [k for k in wanted if k not in self._vecs]
            gone = [k for k in self._vecs if k not in wanted]
        for keys in (missing[i:i + 32] for i in range(0, len(missing), 32)):
            with self._model_lock:
                vectors = list(self._model.embed(
                    [wanted[k][1] for k in keys]))
            rows = [(k, wanted[k][0],
                     _pack(_normalize(list(map(float, v)))))
                    for k, v in zip(keys, vectors)]
            with self._lock:
                for k, _rel, blob in rows:
                    self._vecs[k] = _unpack(blob)
            if db is not None:
                db.executemany("INSERT OR REPLACE INTO chunks VALUES (?,?,?)",
                               rows)
                db.commit()
        if gone:
            with self._lock:
                for k in gone:
                    self._vecs.pop(k, None)
            if db is not None:
                db.executemany("DELETE FROM chunks WHERE key=?",
                               [(k,) for k in gone])
                db.commit()
        if missing:
            logger.debug("memory-wiki: embedded %d record(s)", len(missing))
