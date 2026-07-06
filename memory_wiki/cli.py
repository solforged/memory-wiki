"""`hermes memory-wiki <sub>` CLI, discovered by the plugin CLI system.

Only argparse wiring and printing lives here; logic is in scaffold.py
and friends. Hermes imports happen lazily inside handlers so this
module stays importable everywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path


def register_cli(subparser):
    """Build the ``hermes memory-wiki`` argparse subcommand tree."""
    subparser.description = "Wiki-native memory: status, init, review, lint"
    subs = subparser.add_subparsers(dest="mw_command")

    p_init = subs.add_parser(
        "init",
        help="Scaffold profile/, journal/, _inbox/ and patch the wiki's "
             "check.py + style guide (idempotent)")
    p_init.add_argument("--wiki-root", default=None, metavar="PATH",
                        help="Wiki repository root (default: configured wiki_root)")
    p_init.add_argument("--dry-run", action="store_true",
                        help="Print what would change without writing")

    subs.add_parser("status", help="Wiki, candidate, and config status")

    p_review = subs.add_parser("review", help="Review staged memory candidates")
    p_review.add_argument("--list", action="store_true", dest="list_only",
                          help="List pending candidates and exit")
    p_review.add_argument("--approve", metavar="ID", action="append", default=[],
                          help="Approve and promote a candidate by id")
    p_review.add_argument("--reject", metavar="ID", action="append", default=[],
                          help="Reject a candidate by id")
    p_review.add_argument("--reason", default="", help="Reason recorded on --reject")
    p_review.add_argument("--prune", action="store_true",
                          help="Delete promoted/rejected candidate files older than --days")
    p_review.add_argument("--days", type=int, default=30)

    subs.add_parser("lint", help="Validate pages and candidates, then run the "
                                 "wiki's own _scripts/check.py")

    subparser.set_defaults(func=memory_wiki_command)


def _load_cfg(wiki_root_override=None):
    from . import config as mw_config
    try:
        from hermes_constants import get_hermes_home
        hermes_home = get_hermes_home()
    except Exception:
        hermes_home = None
    overrides = {"wiki_root": wiki_root_override} if wiki_root_override else None
    return mw_config.load_config(hermes_home, overrides)


def memory_wiki_command(args):
    cmd = getattr(args, "mw_command", None)
    if cmd == "init":
        return _cmd_init(args)
    if cmd == "status":
        return _cmd_status(args)
    if cmd == "review":
        return _cmd_review(args)
    if cmd == "lint":
        return _cmd_lint(args)
    print("usage: hermes memory-wiki {init|status|review|lint}")
    return 1


def _cmd_init(args):
    from . import scaffold
    cfg = _load_cfg(getattr(args, "wiki_root", None))
    root = Path(cfg.wiki_root)
    if not root.is_dir():
        print(f"wiki root does not exist: {root}")
        return 1
    report = scaffold.init_wiki(cfg, dry_run=args.dry_run)
    verb = "would create" if args.dry_run else "created"
    if report["created"]:
        print(f"{verb}: " + ", ".join(report["created"]))
    else:
        print("runtime dirs and profile pages already present")
    for name, key in (("_scripts/check.py", "check_py"),
                      ("_meta/style-guide.md", "style_guide")):
        status, diff = report[key]
        if status == "anchor-missing":
            print(f"\n{name}: anchor not found (file was edited); apply manually:")
            print(diff)
        elif status == "no-file":
            print(f"\n{name}: not present, skipped")
        elif status == "already":
            print(f"{name}: already patched")
        else:
            print(f"\n{name}: {status}")
            if diff:
                print(diff)
    if not args.dry_run:
        _run_check_py(root)
    return 0


def _cmd_status(args):
    from . import schema, wikifs
    cfg = _load_cfg()
    root = Path(cfg.wiki_root)
    print(f"wiki root: {root} ({'ok' if root.is_dir() else 'MISSING'})")
    if not root.is_dir():
        return 1
    counts = []
    for d, ntype in schema.DIR_TYPE.items():
        p = root / d
        n = len(list(p.glob("*.md"))) if p.is_dir() else 0
        if n:
            counts.append(f"{d}/ {n}")
    print("pages: " + (", ".join(counts) if counts else "none"))
    by_status = {}
    inbox = root / schema.INBOX_DIR
    if inbox.is_dir():
        for f in inbox.glob("*.md"):
            fm, _ = wikifs.parse_frontmatter(wikifs.read_text(f) or "")
            st = (fm or {}).get("status") or "unparsable"
            by_status[st] = by_status.get(st, 0) + 1
    print("candidates: " + (", ".join(f"{k} {v}" for k, v in sorted(by_status.items()))
                            if by_status else "none"))
    decisions = root / schema.DECISIONS_FILE
    if decisions.is_file():
        tail = (wikifs.read_text(decisions) or "").strip().splitlines()[-5:]
        print("recent decisions:")
        for line in tail:
            print(f"  {line}")
    for k in ("auto_capture", "llm_extraction", "journal_episodes",
              "prefetch_limit", "prompt_block_budget"):
        print(f"config {k}: {getattr(cfg, k)}")
    return 0


def _cmd_review(args):
    print("review is not implemented yet (write path lands in phase 3)")
    return 1


def _cmd_lint(args):
    print("lint is not implemented yet (write path lands in phase 3)")
    return 1


def _run_check_py(root):
    check = root / "_scripts" / "check.py"
    if not check.is_file():
        return
    import subprocess
    print("\nrunning _scripts/check.py:")
    proc = subprocess.run([sys.executable, str(check)],
                          capture_output=True, text=True)
    print(proc.stdout.strip())
    if proc.returncode != 0:
        print(proc.stderr.strip())
