"""Explicit, versioned schema migration planning and application."""

from __future__ import annotations

import json
from pathlib import Path

from . import scaffold, schema, type_registry, wikifs


MANIFEST_PATH = ".memory-wiki/schema.json"


def current_version(wiki_root):
    path = Path(wiki_root) / MANIFEST_PATH
    text = wikifs.read_text(path)
    if text is None:
        return 0
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid migration manifest: {exc}") from exc
    version = value.get("schemaVersion") if isinstance(value, dict) else None
    if not isinstance(version, int) or version < 1:
        raise ValueError("migration manifest has no supported schemaVersion")
    if version > schema.SCHEMA_VERSION:
        type_registry.require_supported_version(version, "migration manifest")
    return version


def plan_migration(wiki_root):
    """Return a no-write plan from the recorded version to this runtime."""
    root = Path(wiki_root)
    source = current_version(root)
    operations = []
    for spec in type_registry.external_types():
        if not (root / spec.directory).is_dir():
            operations.append({
                "kind": "create-directory", "path": spec.directory + "/",
                "automatic": True,
            })
    for path in sorted((root / "raw").glob("*.md")) \
            if (root / "raw").is_dir() else []:
        fm, _body = wikifs.parse_frontmatter(wikifs.read_text(path) or "")
        if isinstance(fm, dict) and not fm.get("privacyTier"):
            operations.append({
                "kind": "set-private-default",
                "path": path.relative_to(root).as_posix(),
                "field": "privacyTier", "value": "private",
                "automatic": True,
            })
    check_status, _ = scaffold._patch_check_py(root, dry_run=True)
    if check_status in ("would-apply", "anchor-missing"):
        operations.append({
            "kind": "sync-validator", "path": "_scripts/check.py",
            "automatic": check_status == "would-apply",
            "status": check_status,
        })
    style_status, _ = scaffold._append_style_guide(root, dry_run=True)
    if style_status in ("would-apply", "anchor-missing"):
        operations.append({
            "kind": "sync-style-guide", "path": "_meta/style-guide.md",
            "automatic": style_status == "would-apply",
            "status": style_status,
        })
    if source != schema.SCHEMA_VERSION:
        operations.append({
            "kind": "write-manifest", "path": MANIFEST_PATH,
            "fromVersion": source, "toVersion": schema.SCHEMA_VERSION,
            "automatic": True,
        })
    return {
        "fromVersion": source,
        "toVersion": schema.SCHEMA_VERSION,
        "operations": operations,
        "blocked": [item for item in operations if not item["automatic"]],
    }

def apply_migration(cfg):
    """Apply automatic plan items and record the resulting schema version."""
    root = Path(cfg.wiki_root)
    plan = plan_migration(root)
    if plan["blocked"]:
        paths = ", ".join(item["path"] for item in plan["blocked"])
        raise ValueError(f"migration needs manual anchor repair: {paths}")
    scaffold_result = scaffold.init_wiki(cfg, dry_run=False)
    updated = list(scaffold_result.get("created", []))
    with wikifs.wiki_lock(root):
        for item in plan["operations"]:
            if item["kind"] == "create-directory":
                path = root / item["path"]
                if not path.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                    updated.append(item["path"])
            elif item["kind"] == "set-private-default":
                path = root / item["path"]
                text = wikifs.read_text(path)
                fm, body = wikifs.parse_frontmatter(text or "")
                if not isinstance(fm, dict) or fm.get("privacyTier"):
                    continue
                fm["privacyTier"] = "private"
                rendered = wikifs.render_page(fm, body)
                errors = schema.validate_page(item["path"], rendered)
                if errors:
                    raise ValueError("; ".join(errors))
                wikifs.atomic_write(path, rendered)
                updated.append(item["path"])
        manifest = json.dumps({
            "schemaVersion": schema.SCHEMA_VERSION,
            "registry": "memory_wiki/page-types.json",
        }, indent=2, sort_keys=True) + "\n"
        if wikifs.write_if_changed(root / MANIFEST_PATH, manifest):
            updated.append(MANIFEST_PATH)
    return {
        **plan,
        "applied": True,
        "updatedFiles": sorted(set(updated)),
        "scaffold": scaffold_result,
    }
