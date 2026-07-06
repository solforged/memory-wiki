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
        self.read_only = False
        self.platform = ""
        self.state = hooks.SessionState()
        self._session_id = ""
        self._prompt_block = ""
        self._prefetch_cache = {}

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
        self.cfg = mw_config.load_config(kwargs.get("hermes_home"), self._overrides)
        if not self.read_only:
            try:
                scaffold.ensure_runtime(self.cfg)
            except OSError as e:
                logger.warning("memory-wiki: cannot prepare %s: %s",
                               self.cfg.wiki_root, e)
        self.index = WikiIndex(self.cfg.wiki_root)
        try:
            self.index.refresh()
        except OSError as e:
            logger.warning("memory-wiki: index refresh failed: %s", e)
        self.recompile_prompt_block()

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
        pending = sum(1 for r in (self.index.records("candidates")
                                  if self.index else ())
                      if r.status == "pending")
        pages = sum(v for k, v in counts.items() if k != "candidate")
        self._prompt_block = layers.compile_prompt_block(
            self.cfg, pending_count=pending, page_count=pages)

    # -- recall ---------------------------------------------------------------

    def prefetch(self, query, *, session_id=""):
        """Per-turn recall block. Fires before every API call, so results
        are cached per (session, query)."""
        if self.cfg is None or self.index is None or not (query or "").strip():
            return ""
        key = (session_id or self._session_id,
               hashlib.sha1(query.encode("utf-8", "replace")).hexdigest()[:16])
        cached = self._prefetch_cache.get(key)
        if cached is not None:
            return cached
        try:
            self.index.refresh()
            from . import search
            block = search.prefetch_block(self.index, self.cfg, query)
        except Exception as e:
            logger.warning("memory-wiki: prefetch failed: %s", e)
            return ""
        if len(self._prefetch_cache) >= _PREFETCH_CACHE_MAX:
            self._prefetch_cache.pop(next(iter(self._prefetch_cache)))
        self._prefetch_cache[key] = block
        return block

    def set_session_id(self, session_id):
        self._session_id = session_id

    def clear_prefetch_cache(self):
        self._prefetch_cache = {}

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
        pass  # all writes are synchronous at hook time; nothing to flush


def register(ctx):
    """Entry point for the memory plugin discovery system."""
    ctx.register_memory_provider(MemoryWikiProvider())
