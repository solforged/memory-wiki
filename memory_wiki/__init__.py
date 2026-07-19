"""Memory Wiki: review-first Hermes memory provider over a plaintext wiki.

Replaces the built-in MEMORY.md/USER.md role with living pages
(profile/user.md, profile/agent.md), a daily journal, and a staged
candidate inbox in front of the wiki's knowledge types.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import threading

from agent.memory_provider import MemoryProvider

from . import config as mw_config
from . import hooks, layers, scaffold, tools
from .index import WikiIndex

logger = logging.getLogger(__name__)

READ_ONLY_CONTEXTS = {"subagent", "cron", "flush"}
_PREFETCH_CACHE_MAX = 16


class MemoryWikiProvider(MemoryProvider):
    """Markdown-only memory provider; no database, one advisory lock."""

    def __init__(self, overrides=None):
        self._overrides = dict(overrides or {})
        self.cfg = None
        self.index = None
        self.embed = None
        self.qmd = None
        self.last_search_backend = "local"
        self.read_only = False
        self.platform = ""
        self.state = hooks.SessionState()
        self._session_id = ""
        self._prompt_block = ""
        self._prefetch_cache = {}
        self._prefetch_lock = threading.Lock()
        self._mutation_generation = 0
        self._status_callback = None
        self._hermes_home = None

    # -- required contract -------------------------------------------------

    @property
    def name(self):
        return "memory-wiki"

    @property
    def session_id(self):
        return self._session_id

    def is_available(self):
        return True  # stdlib only; the wiki scaffold is created on init

    def initialize(self, session_id, **kwargs):
        self._session_id = session_id
        self.platform = kwargs.get("platform", "")
        self.state = hooks.SessionState()
        self.read_only = kwargs.get("agent_context", "primary") in READ_ONLY_CONTEXTS
        hermes_home = kwargs.get("hermes_home")
        self._hermes_home = self._resolve_hermes_home(hermes_home)
        self.cfg = mw_config.load_config(self._hermes_home, self._overrides)
        if not self.read_only:
            try:
                scaffold.ensure_runtime(self.cfg)
            except OSError as e:
                logger.warning("memory-wiki: cannot prepare %s: %s",
                               self.cfg.wiki_root, e)
            if bool(getattr(self.cfg, "auto_compile", True)):
                try:
                    self._compile_wiki()
                except Exception as e:
                    logger.warning("memory-wiki: initial compile failed: %s", e)
            try:
                self._reconcile_schedule()
            except Exception as e:
                logger.warning(
                    "memory-wiki: dream schedule reconcile failed: %s", e)
        self.index = WikiIndex(self.cfg.wiki_root)
        try:
            self.index.refresh()
        except OSError as e:
            logger.warning("memory-wiki: index refresh failed: %s", e)
        self.recompile_prompt_block()
        self._status_callback = kwargs.get("status_callback")
        self._nudge_review()
        backend = str(getattr(self.cfg, "search_backend", "local")).lower()
        if backend not in ("local", "qmd", "auto"):
            logger.warning("memory-wiki: unknown search_backend %r; using local",
                           backend)
            backend = "local"
        if backend in ("qmd", "auto"):
            from .qmd import QmdBackend
            self.qmd = QmdBackend(self.cfg, self.index,
                                  read_only=self.read_only)
        # semantic layer: worker thread owns the model; read-only
        # contexts (subagent/cron) stay lexical to avoid model spin-up
        if backend == "local":
            self._start_embed()

    def get_tool_schemas(self):
        return tools.tool_schemas(read_only=self.read_only)

    def handle_tool_call(self, tool_name, args, **kwargs):
        if self.cfg is None or self.index is None:
            return json.dumps({"ok": False, "error": "provider not initialized"})
        return tools.handle(self, tool_name, args)

    # -- prompt -------------------------------------------------------------

    def system_prompt_block(self):
        return self._prompt_block

    def recompile_prompt_block(self):
        """Called at init and after our own profile writes; the block is
        otherwise frozen for the session (prefix-cache friendly)."""
        if self.cfg is None or not Path(self.cfg.wiki_root).is_dir():
            self._prompt_block = ""
            return
        counts = self.index.counts() if self.index else {}
        pending, oldest = self._pending_stats()
        pages = sum(v for k, v in counts.items() if k != "candidate")
        self._prompt_block = layers.compile_prompt_block(
            self.cfg, pending_count=pending, page_count=pages,
            oldest_pending_days=oldest)

    def _pending_stats(self):
        """(reviewable count, age in days of the oldest candidate)."""
        if self.index is None:
            return 0, None
        from . import wikifs
        ages = [wikifs.days_since(r.created)
                for r in self.index.records("candidates")
                if r.status in ("pending", "approved")]
        oldest = max((a for a in ages if a is not None), default=None)
        return len(ages), oldest

    def _nudge_review(self):
        """One status line at session start when candidates are waiting."""
        if self.read_only or not self._status_callback:
            return
        pending, oldest = self._pending_stats()
        if not pending:
            return
        age = f", oldest {oldest}d" if oldest else ""
        try:
            self._status_callback(
                f"memory-wiki: {pending} candidate(s) awaiting review or "
                f"promotion{age} "
                "- hermes memory-wiki review")
        except Exception as e:
            logger.debug("memory-wiki: status callback failed: %s", e)

    # -- recall ---------------------------------------------------------------

    def search_results(self, query, *, corpus="pages", type_filter=None,
                       limit=5, source="wiki_search", session_id="",
                       record_recall=True):
        """Search through QMD when configured, with local fallback."""
        self.index.refresh()
        results = None
        if self.qmd is not None:
            results = self.qmd.search(query, corpus=corpus,
                                      type_filter=type_filter, limit=limit)
        if results is None:
            self._start_embed()
            if self.embed is not None:
                self.embed.request_sync()
            from . import search
            scored = search.hybrid_scores(
                self.index, self.embed, query, corpus=corpus,
                type_filter=type_filter)
            results = []
            for score, rec in scored[:limit]:
                item = search.result_dict(rec, score, query)
                semantic_ready = bool(
                    self.embed is not None and self.embed.ready
                    and not self.embed.failed)
                relevance = (min(1.0, max(0.0, score)) if semantic_ready
                             else search.query_coverage(query, rec))
                item["relevance"] = round(relevance, 4)
                item["backend"] = "local"
                results.append(item)
            self.last_search_backend = "local"
        else:
            self.last_search_backend = "qmd"
            from .index import tokenize
            query_terms = sorted(set(tokenize(query)))
            for item in results:
                if not isinstance(item, dict):
                    continue
                item.setdefault("retrieval_trace", {
                    "backend": "qmd",
                    "unit_backend": "page",
                    "unit_kind": "page",
                    "query_terms": query_terms,
                    "ranking_score": item.get(
                        "relevance", item.get("score", 0.0)),
                })
        if record_recall and not self.read_only and results:
            self._record_recall(
                results, query, session_id=session_id or self._session_id,
                source=source)
        return results

    def _record_recall(self, results, query, *, session_id, source):
        """Persist recall telemetry without breaking successful recall."""
        try:
            from . import recall
            recall.record(self.cfg, results, query, session_id=session_id,
                          source=source)
        except Exception as e:
            logger.warning("memory-wiki: recall telemetry failed: %s", e)

    def _start_embed(self):
        """Start local semantic recall once, including after QMD fallback."""
        if self.embed is not None or self.read_only or self.cfg is None \
                or not self.cfg.embeddings:
            return
        from .embed import EmbedIndex
        self.embed = EmbedIndex(self.index, model=self.cfg.embedding_model)
        self.embed.start()

    def prefetch(self, query, *, session_id=""):
        """Per-turn recall block. Fires before every API call, so results
        are cached per (session, query)."""
        if self.cfg is None or self.index is None or not (query or "").strip():
            return ""
        key = (session_id or self._session_id,
               hashlib.sha1(query.encode("utf-8", "replace")).hexdigest()[:16])
        with self._prefetch_lock:
            cached = self._prefetch_cache.get(key)
            generation = self._mutation_generation
        if cached is not None:
            return cached
        try:
            from . import search
            results = search.provider_unit_results(
                self, query, corpus="prefetch", granularity="auto",
                limit=max(self.cfg.prefetch_limit * 3, 10),
                source="prefetch", session_id=session_id,
                record_recall=False)
            visible = []
            block = search.prefetch_results_block(
                results, self.cfg, selected=visible)
            if not self.read_only and visible:
                self._record_recall(
                    visible, query,
                    session_id=session_id or self._session_id,
                    source="prefetch")
        except Exception as e:
            logger.warning("memory-wiki: prefetch failed: %s", e)
            return ""
        with self._prefetch_lock:
            if generation != self._mutation_generation:
                return ""
            if len(self._prefetch_cache) >= _PREFETCH_CACHE_MAX:
                self._prefetch_cache.pop(next(iter(self._prefetch_cache)))
            self._prefetch_cache[key] = block
        return block

    def set_session_id(self, session_id):
        self._session_id = session_id

    def clear_prefetch_cache(self):
        with self._prefetch_lock:
            self._prefetch_cache.clear()

    def on_wiki_mutation(self, *, force_compile=False):
        """Invalidate all derived recall state after a successful write."""
        with self._prefetch_lock:
            self._mutation_generation += 1
            self._prefetch_cache.clear()
        compile_step = (self._compile_wiki if force_compile or bool(
            getattr(self.cfg, "auto_compile", True)) else None)
        steps = (
            ("compile", compile_step),
            ("index refresh", self.index.refresh if self.index is not None
             else None),
            ("embedding sync", self.embed.request_sync
             if self.embed is not None else None),
            ("QMD invalidation", self.qmd.invalidate
             if self.qmd is not None else None),
            ("prompt recompilation", self.recompile_prompt_block),
        )
        for label, step in steps:
            if step is None:
                continue
            try:
                step()
            except Exception as e:
                logger.warning("memory-wiki: %s after mutation failed: %s",
                               label, e)

    def _compile_wiki(self):
        from . import compiler
        return compiler.compile_wiki(self.cfg.wiki_root)

    def _reconcile_schedule(self):
        from . import scheduler
        result = scheduler.reconcile(self.cfg, self._hermes_home)
        if result.get("error"):
            raise RuntimeError(result["error"])
        return result

    @staticmethod
    def _resolve_hermes_home(value=None):
        if value:
            return Path(value).expanduser()
        try:
            from hermes_constants import get_hermes_home
            return Path(get_hermes_home()).expanduser()
        except Exception:
            return Path.home() / ".hermes"

    # -- lifecycle hooks (delegated; must never raise into the agent loop) ----

    def sync_turn(self, user_content, assistant_content, *, session_id="",
                  messages=None):
        try:
            hooks.sync_turn(self, user_content, assistant_content,
                            session_id or self._session_id, messages)
        except Exception as e:
            logger.warning("memory-wiki: sync_turn failed: %s", e)

    def on_pre_compress(self, messages):
        try:
            return hooks.on_pre_compress(self, messages)
        except Exception as e:
            logger.warning("memory-wiki: on_pre_compress failed: %s", e)
            return ""

    def on_session_end(self, messages):
        try:
            hooks.on_session_end(self, messages)
        except Exception as e:
            logger.warning("memory-wiki: on_session_end failed: %s", e)

    def on_memory_write(self, action, target, content, metadata=None):
        try:
            hooks.on_memory_write(self, action, target, content, metadata)
        except Exception as e:
            logger.warning("memory-wiki: on_memory_write failed: %s", e)

    def on_session_switch(self, new_session_id, *, parent_session_id="",
                          reset=False, rewound=False, **kwargs):
        try:
            hooks.on_session_switch(self, new_session_id, parent_session_id,
                                    reset, rewound)
        except Exception as e:
            logger.warning("memory-wiki: on_session_switch failed: %s", e)

    # -- config -------------------------------------------------------------

    def get_config_schema(self):
        return mw_config.config_schema()

    def save_config(self, values, hermes_home):
        mw_config.save_plugin_block(values, hermes_home)

    def backup_paths(self):
        """Wiki path for `hermes backup`; callable without initialize()."""
        cfg = self.cfg
        if cfg is None:
            try:
                from hermes_constants import get_hermes_home
                cfg = mw_config.load_config(get_hermes_home(), self._overrides)
            except Exception:
                cfg = mw_config.load_config(None, self._overrides)
        if not cfg.backup_include_wiki:
            return []
        return [str(cfg.wiki_root)]

    def shutdown(self):
        if self.embed is not None:
            self.embed.shutdown()


def register(ctx):
    """Entry point for the memory plugin discovery system."""
    ctx.register_memory_provider(MemoryWikiProvider())
