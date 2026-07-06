"""Memory Wiki: review-first Hermes memory provider over a plaintext wiki.

Replaces the built-in MEMORY.md/USER.md role with living pages
(profile/user.md, profile/agent.md), a daily journal, and a staged
candidate inbox in front of the wiki's knowledge types.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent.memory_provider import MemoryProvider

from . import config as mw_config
from . import scaffold

logger = logging.getLogger(__name__)

READ_ONLY_CONTEXTS = {"subagent", "cron", "flush"}


class MemoryWikiProvider(MemoryProvider):
    """Markdown-only memory provider; no database, one advisory lock."""

    def __init__(self, overrides=None):
        self._overrides = dict(overrides or {})
        self._cfg = None
        self._session_id = ""
        self._read_only = False
        self._prompt_block = ""

    # -- required contract -------------------------------------------------

    @property
    def name(self):
        return "memory-wiki"

    def is_available(self):
        return True  # stdlib only; the wiki scaffold is created on init

    def initialize(self, session_id, **kwargs):
        self._session_id = session_id
        self._read_only = kwargs.get("agent_context", "primary") in READ_ONLY_CONTEXTS
        self._cfg = mw_config.load_config(kwargs.get("hermes_home"), self._overrides)
        if not self._read_only:
            try:
                scaffold.ensure_runtime(self._cfg)
            except OSError as e:
                logger.warning("memory-wiki: cannot prepare %s: %s",
                               self._cfg.wiki_root, e)
        self._prompt_block = self._compile_prompt_block()

    def get_tool_schemas(self):
        return []  # tools land with the read/write paths

    def handle_tool_call(self, tool_name, args, **kwargs):
        return json.dumps({"ok": False, "error": f"unknown tool '{tool_name}'"})

    # -- prompt -------------------------------------------------------------

    def system_prompt_block(self):
        return self._prompt_block

    def _compile_prompt_block(self):
        if self._cfg is None:
            return ""
        root = Path(self._cfg.wiki_root)
        if not root.is_dir():
            return ""
        return ("# Memory wiki\n"
                f"Active over {root}. Recall and write tools arrive in a "
                "later phase.")

    # -- config -------------------------------------------------------------

    def get_config_schema(self):
        return mw_config.config_schema()

    def save_config(self, values, hermes_home):
        mw_config.save_plugin_block(values, hermes_home)

    def backup_paths(self):
        """Wiki path for `hermes backup`; callable without initialize()."""
        cfg = self._cfg
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
