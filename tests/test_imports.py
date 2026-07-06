"""Every root module must import cleanly: the hermes plugin loader
execs each one and silently swallows failures."""

import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "memory_wiki"


def test_every_root_module_imports():
    for py in sorted(ROOT.glob("*.py")):
        importlib.import_module(f"memory_wiki.{py.stem}" if py.stem != "__init__"
                                else "memory_wiki")


def test_no_third_party_imports_at_module_level():
    # stdlib-only rule; `yaml` must only be imported lazily inside functions
    import ast
    allowed_internal = {py.stem for py in ROOT.glob("*.py")}
    for py in sorted(ROOT.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in tree.body:
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [(node.module or "").split(".")[0]]
            for name in names:
                if not name:
                    continue
                if name in allowed_internal or name == "agent":
                    continue  # package-relative or the hermes ABC
                assert _is_stdlib(name), f"{py.name} imports non-stdlib '{name}' at module level"


def _is_stdlib(name):
    import sys
    return name in sys.stdlib_module_names
