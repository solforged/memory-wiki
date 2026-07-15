"""Configuration for the memory-wiki provider.

Options live in ``<hermes_home>/config.yaml`` under ``plugins.memory-wiki``.
YAML parsing is lazy and fail-soft: inside the Hermes runtime PyYAML is
always present; anywhere else we fall back to defaults so the module
stays importable stdlib-only.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_KEY = "memory-wiki"
DEFAULT_WIKI_ROOT = "~/wiki"


@dataclass
class MemoryWikiConfig:
    wiki_root: Path = field(default_factory=lambda: Path(DEFAULT_WIKI_ROOT).expanduser())
    auto_capture: bool = True
    llm_extraction: bool = False
    search_backend: str = "local"
    embeddings: bool = True
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    qmd_command: str = "qmd"
    qmd_search_mode: str = "search"
    qmd_timeout_seconds: int = 8
    qmd_maintenance_timeout_seconds: int = 120
    qmd_update_interval_seconds: int = 300
    qmd_embed_interval_seconds: int = 3600
    qmd_rerank: bool = True
    qmd_state_dir: str = ""
    journal_episodes: bool = True
    prefetch_limit: int = 5
    prefetch_char_budget: int = 1200
    profile_user_budget: int = 6000
    profile_agent_budget: int = 4000
    prompt_block_budget: int = 9000
    max_candidates_per_turn: int = 3
    max_candidates_per_session: int = 40
    candidate_stale_days: int = 30
    dream_limit: int = 50
    dream_min_score: float = 0.75
    dream_min_recall_count: int = 3
    dream_min_unique_queries: int = 2
    dream_recency_half_life_days: int = 14
    backup_include_wiki: bool = True


def load_config(hermes_home=None, overrides=None):
    """Config from config.yaml's plugins.memory-wiki block + overrides."""
    data = {}
    if hermes_home:
        data.update(read_plugin_block(hermes_home))
    if overrides:
        data.update(overrides)
    cfg = MemoryWikiConfig()
    for f in dataclasses.fields(MemoryWikiConfig):
        if f.name not in data:
            continue
        raw = data[f.name]
        if f.name == "wiki_root":
            cfg.wiki_root = Path(str(raw)).expanduser()
        elif isinstance(getattr(cfg, f.name), bool):
            setattr(cfg, f.name, _as_bool(raw))
        elif isinstance(getattr(cfg, f.name), int):
            try:
                setattr(cfg, f.name, int(raw))
            except (TypeError, ValueError):
                logger.debug("memory-wiki: bad int for %s: %r", f.name, raw)
        elif isinstance(getattr(cfg, f.name), float):
            try:
                setattr(cfg, f.name, float(raw))
            except (TypeError, ValueError):
                logger.debug("memory-wiki: bad float for %s: %r", f.name, raw)
        else:
            setattr(cfg, f.name, str(raw))
    return cfg


def _as_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def read_plugin_block(hermes_home):
    """The raw plugins.memory-wiki mapping, or {} on any failure."""
    cfg_path = Path(hermes_home).expanduser() / "config.yaml"
    if not cfg_path.is_file():
        return {}
    try:
        import yaml
    except ImportError:
        logger.debug("memory-wiki: pyyaml unavailable, using defaults")
        return {}
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        block = (raw.get("plugins") or {}).get(CONFIG_KEY) or {}
        return dict(block) if isinstance(block, dict) else {}
    except Exception as e:
        logger.debug("memory-wiki: failed reading %s: %s", cfg_path, e)
        return {}


def save_plugin_block(values, hermes_home):
    """Merge values into config.yaml under plugins.memory-wiki."""
    import yaml  # runtime path only; hermes always ships pyyaml

    cfg_path = Path(hermes_home).expanduser() / "config.yaml"
    raw = {}
    if cfg_path.is_file():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    plugins = raw.setdefault("plugins", {})
    block = plugins.setdefault(CONFIG_KEY, {})
    block.update(values or {})
    cfg_path.write_text(yaml.safe_dump(raw, default_flow_style=False, sort_keys=False),
                        encoding="utf-8")


def config_schema():
    """Fields prompted by `hermes memory setup`, kept to the minimum."""
    return [{
        "key": "wiki_root",
        "description": "Path to your wiki repository",
        "default": DEFAULT_WIKI_ROOT,
        "required": True,
    }]
