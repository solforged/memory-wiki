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
    subparser.description = "Wiki-native memory: status, init, review, dream, lint"
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
    p_review.add_argument("--kind", action="append", default=[], metavar="KIND",
                          help="Filter by candidate kind (repeatable)")
    p_review.add_argument("--confidence-filter", action="append", default=[],
                          metavar="LEVEL",
                          help="Filter by confidence (repeatable)")
    p_review.add_argument("--target-filter", action="append", default=[],
                          metavar="PATH",
                          help="Filter by explicit or effective target (repeatable)")
    p_review.add_argument("--effective-target", action="append", default=[],
                          metavar="PATH",
                          help="Filter by effective target (repeatable)")
    p_review.add_argument("--stale-only", action="store_true",
                          help="Show only candidates older than candidate_stale_days")
    p_review.add_argument("--min-age", type=int, default=None, metavar="DAYS",
                          help="Filter to candidates at least DAYS old")
    p_review.add_argument("--limit", type=int, default=None, metavar="N",
                          help="Maximum candidates after filtering")
    p_review.add_argument("--approve-filtered", action="store_true",
                          help="Approve every candidate selected by filters")
    p_review.add_argument("--reject-filtered", action="store_true",
                          help="Reject every candidate selected by filters")
    p_review.add_argument("--yes", action="store_true",
                          help="Confirm a filtered batch mutation")

    p_dream = subs.add_parser(
        "dream", help="Plan a conservative curation pass over pending candidates")
    p_dream.add_argument("--limit", type=int, default=None,
                         help="Max pending candidates to inspect (default: config dream_limit)")
    p_dream.add_argument("--apply-safe", action="store_true",
                         help="Promote only high-confidence profile/agent suggestions")

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
    if cmd == "dream":
        return _cmd_dream(args)
    if cmd == "lint":
        return _cmd_lint(args)
    print("usage: hermes memory-wiki {init|status|review|dream|lint}")
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
              "prompt_block_budget", "candidate_stale_days", "dream_limit"):
        print(f"config {k}: {getattr(cfg, k)}")
    return 0


def _cmd_dream(args):
    from . import dream
    from .index import WikiIndex
    cfg = _load_cfg()
    if not Path(cfg.wiki_root).is_dir():
        print(f"wiki root does not exist: {cfg.wiki_root}")
        return 1
    index = WikiIndex(cfg.wiki_root)
    index.refresh()
    result = dream.run(cfg, index, limit=args.limit,
                       apply_safe=args.apply_safe, actor="cli")
    print(f"dream: considered {result['considered']} of {result['pending']} "
          f"pending candidate(s)")
    if result["summary"]:
        summary = ", ".join(f"{k} {v}" for k, v in sorted(result["summary"].items()))
        print(f"summary: {summary}")
    else:
        print("summary: no suggestions")
    for suggestion in result["suggestions"]:
        _print_dream_suggestion(suggestion)
    if args.apply_safe:
        applied = result.get("applied", [])
        failed = [a for a in applied if not a.get("ok")]
        print(f"applied safe promotions: {len(applied) - len(failed)}/"
              f"{len(applied)}")
        for item in applied:
            status = item.get("result") if item.get("ok") else f"ERROR {item.get('error')}"
            print(f"  {item['id']}: {status} -> {item.get('target', '')}")
        return 1 if failed else 0
    return 0


def _print_dream_suggestion(suggestion):
    if suggestion.get("ids"):
        ids = ", ".join(suggestion["ids"])
        print(f"\n{suggestion['action']}  {ids}")
        print(f"  {suggestion['reason']}")
        for item in suggestion.get("excerpts", []):
            flags = ""
            if item.get("covered_by"):
                flags += " [covered]"
            if item.get("warnings"):
                flags += " [" + ",".join(item["warnings"]) + "]"
            if item.get("suggested_target"):
                flags += f" [-> {item['suggested_target']}]"
            print(f"  - {item['id']}: {item['excerpt']}{flags}")
    else:
        safe = " [safe]" if suggestion.get("safe_apply") else ""
        print(f"\n{suggestion['id']}  {suggestion['action']}{safe} -> "
              f"{suggestion.get('target', '')}")
        print(f"  {suggestion['reason']}")
        print(f"  {suggestion.get('excerpt', '')}")
    if suggestion.get("covered_by"):
        print("  covered by: " + ", ".join(suggestion["covered_by"]))
    if suggestion.get("suggested_target"):
        print(f"  suggested target: {suggestion['suggested_target']}")
    if suggestion.get("command"):
        print(f"  next: {suggestion['command']}")


def _cmd_review(args):
    from . import candidates, hints, promote
    from .index import WikiIndex
    cfg = _load_cfg()
    if not Path(cfg.wiki_root).is_dir():
        print(f"wiki root does not exist: {cfg.wiki_root}")
        return 1
    batch_approve = bool(getattr(args, "approve_filtered", False))
    batch_reject = bool(getattr(args, "reject_filtered", False))
    batch = batch_approve or batch_reject
    if batch and args.prune:
        print("filtered batch mode cannot be combined with --prune")
        return 1
    if batch and args.list_only:
        print("filtered batch mode cannot be combined with --list")
        return 1
    if args.prune:
        removed = candidates.prune(cfg, days=args.days)
        print(f"pruned {len(removed)} candidate file(s)")
        return 0
    index = WikiIndex(cfg.wiki_root)
    index.refresh()
    overrides = _approve_overrides(args)
    filters = _review_filters(args)
    if batch_approve and batch_reject:
        print("choose only one of --approve-filtered or --reject-filtered")
        return 1
    if batch and (args.approve or args.reject):
        print("filtered batch mode cannot be combined with --approve or --reject")
        return 1
    if batch and overrides:
        print("override flags cannot be used with filtered batch mode")
        return 1
    if batch and not filters and getattr(args, "limit", None) is None:
        print("filtered batch mode requires at least one filter")
        return 1
    if batch and not getattr(args, "yes", False):
        print("filtered batch mode requires --yes; no candidates changed")
        return 1
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
    try:
        queue = hints.build_review_queue(
            cfg, index, filters=filters, limit=getattr(args, "limit", None))
    except ValueError as e:
        print(f"invalid review filter: {e}")
        return 1
    pending = queue["candidates"]
    if batch:
        action = "approval" if batch_approve else "rejection"
        print(f"selected {len(pending)} candidate(s) for filtered {action}")
        completed = 0
        for c in pending:
            if batch_approve:
                result = promote.promote(cfg, index, c["id"], actor="cli")
            else:
                result = promote.reject(
                    cfg, c["id"], reason=args.reason, actor="cli")
            print(f"{c['id']}: " + (result.get("result", "")
                                    if result.get("ok") else
                                    f"ERROR {result.get('error')}"))
            if not result.get("ok"):
                print(f"stopped after {completed} of {len(pending)} "
                      "candidate(s); remaining candidates unchanged")
                return 1
            completed += 1
        return 0
    if not pending:
        if queue["summary"]["total_pending"]:
            print("no pending candidates match the filters")
        else:
            print("no pending candidates")
        return 0
    if args.list_only:
        summary = queue["summary"]
        print(f"matched {summary['matched']} of {summary['total_pending']} "
              f"pending candidate(s); {summary['stale_count']} stale")
        for c in pending:
            age = f"  {c['age_days']}d" if c.get("age_days") else ""
            print(f"{c['id']}  {c['kind']:<10} {c['confidence']:<11}{age} "
                  f"-> {c['effective_target']}")
            print(f"    {c['title']}")
            if c["preview"] != c["title"]:
                print(f"    {c['preview']}")
            hint = _hint_line(c)
            if hint:
                print(f"    {hint}")
        return 0
    return _review_interactive(cfg, index, pending)


def _review_filters(args):
    filters = {}
    for arg_name, filter_name in (
            ("kind", "kind"),
            ("confidence_filter", "confidence"),
            ("target_filter", "target"),
            ("effective_target", "effective_target")):
        value = getattr(args, arg_name, None)
        if value:
            filters[filter_name] = value
    if getattr(args, "stale_only", False):
        filters["stale_only"] = True
    min_age = getattr(args, "min_age", None)
    if min_age is not None:
        filters["min_age"] = min_age
    return filters


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


def _hint_line(cand):
    """One compact 'hint: ...' line from a review queue item."""
    parts = []
    if cand.get("warnings"):
        parts.append(", ".join(cand["warnings"]))
    if cand.get("covered_by"):
        parts.append("covered by " + ", ".join(cand["covered_by"]))
    if cand.get("similar_candidates"):
        parts.append("similar to " + ", ".join(cand["similar_candidates"]))
    if cand.get("suggested_target"):
        parts.append(f"consider target {cand['suggested_target']}")
    return f"hint: {'; '.join(parts)}" if parts else ""


def _review_interactive(cfg, index, pending):
    from . import promote
    print(f"{len(pending)} pending candidate(s). "
          "[a]pprove+promote / [e]dit then approve / "
          "[t]arget then approve / [r]eject / [s]kip / [q]uit")
    for c in pending:
        target = c["effective_target"]
        age = f", {c['age_days']}d old" if c.get("age_days") else ""
        print(f"\n{c['id']}  {c['kind']} ({c['confidence']}{age}) -> {target}")
        print(f"  {c['content']}")
        hint = _hint_line(c)
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
