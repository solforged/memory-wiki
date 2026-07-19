import re
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parents[1] / "memory_wiki"
DESKTOP_PLUGIN = PLUGIN_ROOT / "desktop" / "plugin.js"


def test_native_desktop_plugin_contract():
    text = DESKTOP_PLUGIN.read_text(encoding="utf-8")

    assert re.search(r"const\s+ID\s*=\s*['\"]memory-wiki['\"]", text)
    assert re.search(r"\bid\s*:\s*ID\b", text)
    assert "ctx.rest('/overview')" in text
    assert re.search(r"\barea\s*:\s*['\"]panes['\"]", text)


def test_native_desktop_plugin_uses_only_runtime_imports():
    text = DESKTOP_PLUGIN.read_text(encoding="utf-8")
    imports = set(re.findall(r"\bfrom\s+['\"]([^'\"]+)['\"]", text))

    assert imports == {"@hermes/plugin-sdk", "react/jsx-runtime"}


def test_native_desktop_plugin_preserves_webui_dashboard():
    manifest = PLUGIN_ROOT / "dashboard" / "manifest.json"

    assert manifest.is_file()
    assert '"name": "memory-wiki"' in manifest.read_text(encoding="utf-8")
