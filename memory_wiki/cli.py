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
    p_review.add_argument("--content", default=None, metavar="TEXT",
                          help="Replace the candidate content on --approve")
    p_review.add_argument("--target", default=None, metavar="PATH",
                          help="Retarget the candidate on --approve")
    p_review.add_argument("--section", default=None, metavar="'## Heading'",
                          help="Section to append under on --approve")
    p_review.add_argument("--confidence", default=None,
                          help="Override confidence on --approve")
    p_review.add_argument("--topic", action="append", default=[], metavar="TOPIC",
                          help="Topic for a new page on --approve (repeatable)")
    p_review.add_argument("--field", action="append", default=[],
                          metavar="KEY=VALUE",
                          help="Extra frontmatter for new-page promotion on "
                               "--approve (repeatable, e.g. stance=model)")
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
    for k in ("auto_capture", "llm_extraction", "embeddings",
              "embedding_model", "journal_episodes", "prefetch_limit",
              "prompt_block_budget", "candidate_stale_days"):
        print(f"config {k}: {getattr(cfg, k)}")
    return 0


def _cmd_review(args):
    from . import candidates, promote
    from .index import WikiIndex
    cfg = _load_cfg()
    if not Path(cfg.wiki_root).is_dir():
        print(f"wiki root does not exist: {cfg.wiki_root}")
        return 1
    if args.prune:
        removed = candidates.prune(cfg, days=args.days)
        print(f"pruned {len(removed)} candidate file(s)")
        return 0
    index = WikiIndex(cfg.wiki_root)
    index.refresh()
    overrides = _approve_overrides(args)
    if overrides and len(args.approve) != 1:
        print("override flags need exactly one --approve ID")
        return 1
    rc = 0
    for cand_id in args.approve:
        result = promote.approve(cfg, index, cand_id,
                                 overrides=overrides, actor="cli")
        print(f"{cand_id}: " + (result.get("result", "")
                                if result.get("ok") else
                                f"ERROR {result.get('error')}"))
        rc |= 0 if result.get("ok") else 1
    for cand_id in args.reject:
        result = promote.reject(cfg, cand_id, reason=args.reason, actor="cli")
        print(f"{cand_id}: " + (result.get("result", "")
                                if result.get("ok") else
                                f"ERROR {result.get('error')}"))
        rc |= 0 if result.get("ok") else 1
    if args.approve or args.reject:
        return rc
    pending = candidates.list_candidates(cfg, statuses=("pending",))
    if not pending:
        print("no pending candidates")
        return 0
    if args.list_only:
        for c in pending:
            target = c["target"] or f"(route by kind: {c['kind']})"
            age = f"  {c['age_days']}d" if c.get("age_days") else ""
            print(f"{c['id']}  {c['kind']:<10} {c['confidence']:<11}{age} "
                  f"-> {target}")
            print(f"    {c['content'][:120]}")
            hint = _hint_line(index, c, pending)
            if hint:
                print(f"    {hint}")
        return 0
    return _review_interactive(cfg, index, pending)


def _approve_overrides(args):
    """promote.approve overrides from the CLI flags; {} when none given."""
    overrides = {}
    for key in ("content", "target", "section", "confidence"):
        val = getattr(args, key, None)
        if val is not None:
            overrides[key] = val
    if getattr(args, "topic", None):
        overrides["topics"] = list(args.topic)
    fields = {}
    for pair in getattr(args, "field", None) or []:
        key, sep, val = pair.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"--field expects KEY=VALUE, got {pair!r}")
        fields[key.strip()] = val.strip()
    if fields:
        overrides["fields"] = fields
    return overrides


def _hint_line(index, cand, pending):
    """One compact 'hint: ...' line, or '' when nothing stands out."""
    from . import hints
    h = hints.candidate_hints(index, cand, pending)
    parts = []
    if h.get("warnings"):
        parts.append(", ".join(h["warnings"]))
    if h.get("covered_by"):
        parts.append("covered by " + ", ".join(h["covered_by"]))
    if h.get("similar_candidates"):
        parts.append("similar to " + ", ".join(h["similar_candidates"]))
    if h.get("suggested_target"):
        parts.append(f"consider target {h['suggested_target']}")
    return f"hint: {'; '.join(parts)}" if parts else ""


def _review_interactive(cfg, index, pending):
    from . import promote
    print(f"{len(pending)} pending candidate(s). "
          "[a]pprove+promote / [e]dit then approve / "
          "[t]arget then approve / [r]eject / [s]kip / [q]uit")
    for c in pending:
        target = c["target"] or f"route by kind ({c['kind']})"
        age = f", {c['age_days']}d old" if c.get("age_days") else ""
        print(f"\n{c['id']}  {c['kind']} ({c['confidence']}{age}) -> {target}")
        print(f"  {c['content']}")
        hint = _hint_line(index, c, pending)
        if hint:
            print(f"  {hint}")
        try:
            choice = input("  [a/e/t/r/s/q] ").strip().lower()
        except EOFError:
            break
        if choice == "q":
            break
        if choice == "a":
            result = promote.approve(cfg, index, c["id"], actor="cli")
        elif choice == "e":
            edited = _edit_content(c["content"])
            if edited is None or edited == c["content"]:
                print("  unchanged, skipped")
                continue
            result = promote.approve(cfg, index, c["id"],
                                     overrides={"content": edited},
                                     actor="cli")
        elif choice == "t":
            new_target = input("  target path: ").strip()
            new_section = input("  section (optional): ").strip()
            overrides = {"target": new_target}
            if new_section:
                overrides["section"] = new_section
            result = promote.approve(cfg, index, c["id"],
                                     overrides=overrides, actor="cli")
        elif choice == "r":
            reason = input("  reason (optional): ").strip()
            result = promote.reject(cfg, c["id"], reason=reason, actor="cli")
        else:
            continue
        print("  " + (result.get("result", "done") if result.get("ok")
                      else f"ERROR {result.get('error')}"))
    return 0


def _edit_content(content):
    """Edited candidate content via $EDITOR, or an input() replacement
    when no editor is configured. None means keep as-is."""
    import os
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile("w+", suffix=".md",
                                         delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            if subprocess.run([*editor.split(), tmp_path]).returncode != 0:
                return None
            with open(tmp_path, encoding="utf-8") as f:
                return f.read().strip()
        finally:
            os.unlink(tmp_path)
    try:
        replacement = input("  new content (empty keeps current): ").strip()
    except EOFError:
        return None
    return replacement or None


def _cmd_lint(args):
    import re
    from . import schema, wikifs
    cfg = _load_cfg()
    root = Path(cfg.wiki_root)
    if not root.is_dir():
        print(f"wiki root does not exist: {root}")
        return 1
    errors = []
    known_ids = set()
    for d in schema.DIR_TYPE:
        dpath = root / d
        if not dpath.is_dir():
            continue
        for p in sorted(dpath.glob("*.md")):
            rel = f"{d}/{p.name}"
            errors.extend(schema.validate_page(rel, wikifs.read_text(p) or ""))
    inbox = root / schema.INBOX_DIR
    if inbox.is_dir():
        for p in sorted(inbox.glob("*.md")):
            fm, _ = wikifs.parse_frontmatter(wikifs.read_text(p) or "")
            if fm is None:
                errors.append(f"{schema.INBOX_DIR}/{p.name}: missing frontmatter")
                continue
            for e in schema.validate_candidate(fm):
                errors.append(f"{schema.INBOX_DIR}/{p.name}: {e}")
            if fm.get("id"):
                known_ids.add(fm["id"])
    # orphaned candidate markers: promoted content whose candidate file is gone
    marker_re = re.compile(
        r"<!-- memory-wiki: (?:promoted )?(\d{8}-\d{6}-\w{8}-\d{2}) -->")
    for d in schema.DIR_TYPE:
        dpath = root / d
        if not dpath.is_dir():
            continue
        for p in sorted(dpath.glob("*.md")):
            for cid in marker_re.findall(wikifs.read_text(p) or ""):
                if cid not in known_ids:
                    print(f"  warn  {d}/{p.name}: marker for pruned/missing "
                          f"candidate {cid}")
    if errors:
        print(f"errors ({len(errors)}):")
        for e in errors:
            print(f"  {e}")
    else:
        print("plugin checks: ok")
    check_rc = _run_check_py(root)
    return 1 if errors or check_rc else 0


def _run_check_py(root):
    check = root / "_scripts" / "check.py"
    if not check.is_file():
        return 0
    import subprocess
    print("\nrunning _scripts/check.py:")
    proc = subprocess.run([sys.executable, str(check)],
                          capture_output=True, text=True)
    print(proc.stdout.strip())
    if proc.returncode != 0:
        print(proc.stderr.strip())
    return proc.returncode
