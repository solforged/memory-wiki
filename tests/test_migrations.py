import json
from argparse import Namespace
from pathlib import Path

from memory_wiki import cli, migrations, schema, wikifs


def test_migration_plan_is_no_write_and_apply_is_idempotent(cfg, monkeypatch):
    root = Path(cfg.wiki_root)
    raw_dir = root / "raw"
    raw_dir.mkdir()
    raw = raw_dir / "archive.md"
    fm = {
        "type": "raw", "title": "Archive", "topics": [],
        "createdAt": "2026-07-19",
    }
    raw.write_text(wikifs.render_page(fm, "Private.\n"), encoding="utf-8")

    plan = migrations.plan_migration(root)

    assert plan["fromVersion"] == 0
    assert any(item["kind"] == "set-private-default"
               for item in plan["operations"])
    assert not (root / migrations.MANIFEST_PATH).exists()
    assert "privacyTier" not in raw.read_text(encoding="utf-8")

    result = migrations.apply_migration(cfg)

    migrated, _ = wikifs.parse_frontmatter(raw.read_text(encoding="utf-8"))
    assert migrated["privacyTier"] == "private"
    manifest = json.loads((root / migrations.MANIFEST_PATH).read_text())
    assert manifest["schemaVersion"] == schema.SCHEMA_VERSION
    assert migrations.apply_migration(cfg)["updatedFiles"] == []


def test_future_migration_manifest_is_rejected(cfg):
    path = Path(cfg.wiki_root) / migrations.MANIFEST_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schemaVersion": schema.SCHEMA_VERSION + 1}))
    try:
        migrations.plan_migration(cfg.wiki_root)
    except ValueError as exc:
        assert "newer than supported" in str(exc)
    else:
        raise AssertionError("future manifest was accepted")


def test_migrate_cli_plans_by_default(cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load_cfg", lambda *_args, **_kwargs: cfg)
    args = Namespace(wiki_root=None, apply=False, plan=False)
    assert cli._cmd_migrate(args) == 0
    assert "schema 0 ->" in capsys.readouterr().out
