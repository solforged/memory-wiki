import json
from pathlib import Path

import pytest

from memory_wiki import candidates, layers, promote, scaffold, schema, wikifs
from memory_wiki.index import WikiIndex


@pytest.fixture
def ready(cfg):
    scaffold.ensure_runtime(cfg)
    idx = WikiIndex(cfg.wiki_root)
    idx.refresh()
    return cfg, idx


def stage(cfg, idx, content, **kw):
    r = candidates.stage(cfg, idx, content, **kw)
    assert r["staged"], r
    return r["id"]


def read(cfg, rel):
    return wikifs.read_text(Path(cfg.wiki_root) / rel)


def test_preference_routes_to_profile(ready):
    cfg, idx = ready
    cid = stage(cfg, idx, "Wants terse, no-preamble answers.",
                kind="preference", session_id="s1")
    result = promote.approve(cfg, idx, cid, actor="cli")
    assert result["ok"] and result["target"] == "profile/user.md"
    text = read(cfg, "profile/user.md")
    pref = text.split("## Preferences")[1].split("##")[0]
    assert "Wants terse, no-preamble answers." in pref
    assert schema.candidate_marker(cid) in pref
    assert schema.validate_page("profile/user.md", text) == []
    # candidate file updated
    _, fm, _, _ = candidates.load(cfg, cid)
    assert fm["status"] == "promoted"
    assert fm["promoted_to"] == "profile/user.md"
    # idempotent re-promote
    again = promote.promote(cfg, idx, cid)
    assert again["ok"] and again["result"] == "already_promoted"
    assert read(cfg, "profile/user.md").count(schema.candidate_marker(cid)) == 1


def test_note_and_decision_routes(ready):
    cfg, idx = ready
    cid_n = stage(cfg, idx, "The wiki uses markdown links, not wikilinks.",
                  kind="note")
    cid_d = stage(cfg, idx, "Ship phase three before the weekend.",
                  kind="decision")
    assert promote.approve(cfg, idx, cid_n)["target"] == "profile/agent.md"
    result = promote.approve(cfg, idx, cid_d)
    assert result["target"] == layers.journal_rel()
    jtext = read(cfg, layers.journal_rel())
    assert "Ship phase three" in jtext
    assert schema.validate_page(layers.journal_rel(), jtext) == []


def test_claim_new_page(ready):
    cfg, idx = ready
    cid = stage(cfg, idx, "Review-first staging beats direct writes for "
                          "agent memory.", kind="claim",
                confidence="likely", topics=["memory"], session_id="8f3e9c12")
    result = promote.approve(cfg, idx, cid)
    assert result["ok"], result
    rel = result["target"]
    assert rel.startswith("claims/review-first-staging-beats-direct")
    text = read(cfg, rel)
    fm, body = wikifs.parse_frontmatter(text)
    assert fm["type"] == "claim" and fm["confidence"] == "likely"
    assert fm["maturity"] == "seedling" and fm["topics"] == ["memory"]
    assert "## Evidence" in body and "## Open questions" in body
    assert "[[journal/" in body and "session 8f3e9c12" in body
    assert schema.promoted_comment(cid) in body
    assert schema.validate_page(rel, text) == []


def test_concept_new_page_and_existing_refused(ready):
    cfg, idx = ready
    cid = stage(cfg, idx, "Prefix caching keeps repeated prompts cheap.",
                kind="concept", topics=["llm"])
    result = promote.approve(cfg, idx, cid)
    assert result["ok"]
    rel = result["target"]
    assert rel.startswith("concepts/")
    assert schema.validate_page(rel, read(cfg, rel)) == []
    # a second candidate slugging to the same page is refused at promote
    # time (index=None bypasses staging dedup, which would catch it earlier)
    cid2 = stage(cfg, None, "Prefix caching keeps repeated prompts cheap!!",
                 kind="concept")
    result2 = promote.approve(cfg, idx, cid2)
    assert not result2["ok"] and "already exists" in result2["error"]
    _, fm2, _, _ = candidates.load(cfg, cid2)
    assert fm2["status"] == "approved"  # stays approved for retargeting


FULL_CLAIM_PAGE = ("---\ntype: claim\ntitle: staged pages promote verbatim\n"
                   "topics: [memory]\nstance: model\nconfidence: likely\n"
                   "maturity: budding\ncreated: 2026-07-06\n---\n"
                   "Argument paragraph.\n\n## Evidence\n\n- staged evidence\n"
                   "\n## Open questions\n")


def test_page_shaped_claim_promotes_verbatim(ready):
    cfg, idx = ready
    cid = stage(cfg, idx, FULL_CLAIM_PAGE, kind="claim")
    result = promote.approve(cfg, idx, cid)
    assert result["ok"], result
    rel = result["target"]
    assert rel == "claims/staged-pages-promote-verbatim.md"
    text = read(cfg, rel)
    fm, body = wikifs.parse_frontmatter(text)
    assert fm["stance"] == "model" and fm["maturity"] == "budding"
    assert "Argument paragraph." in body
    assert "- staged evidence" in body
    assert "## Open questions" in body
    assert schema.promoted_comment(cid) in body
    assert schema.validate_page(rel, text) == []
    # page-shaped promoted candidates cannot be superseded in place
    idx.refresh()
    r = promote.supersede(cfg, idx, cid, "replacement text")
    assert not r["ok"] and "full page" in r["error"]


def test_multiline_append_refused_stays_approved(ready):
    cfg, idx = ready
    cid = stage(cfg, idx, "line one\nline two", kind="preference")
    result = promote.approve(cfg, idx, cid)
    assert not result["ok"] and "single-line bullets" in result["error"]
    _, fm, _, _ = candidates.load(cfg, cid)
    assert fm["status"] == "approved"  # recoverable: retarget or edit
    fixed = promote.approve(cfg, idx, cid,
                            overrides={"content": "line one; line two"})
    assert fixed["ok"] and fixed["target"] == "profile/user.md"


def test_page_shaped_append_target_refused(ready):
    cfg, idx = ready
    # staging refuses an existing target outright...
    r = candidates.stage(cfg, idx, FULL_CLAIM_PAGE,
                         target="claims/small-tools-beat-frameworks.md")
    assert not r["staged"] and "new-page target" in r["reason"]
    # ...and retargeting to an existing page at approve time is caught too
    cid = stage(cfg, idx, FULL_CLAIM_PAGE, kind="claim")
    result = promote.approve(cfg, idx, cid, overrides={
        "target": "claims/small-tools-beat-frameworks.md"})
    assert not result["ok"] and "new-page target" in result["error"]


def test_source_requires_author_and_medium(ready):
    cfg, idx = ready
    cid = stage(cfg, idx, "Great post on memory architectures.",
                kind="fact", target="sources/memory-post.md")
    result = promote.approve(cfg, idx, cid)
    assert not result["ok"] and "author" in result["error"]
    result2 = promote.approve(cfg, idx, cid, overrides={
        "fields": {"author": "Jane Doe", "medium": "post"}})
    assert result2["ok"], result2
    text = read(cfg, "sources/memory-post.md")
    fm, _ = wikifs.parse_frontmatter(text)
    assert fm["author"] == "Jane Doe" and fm["medium"] == "post"
    assert schema.validate_page("sources/memory-post.md", text) == []


def test_explicit_existing_target_appends_section(ready):
    cfg, idx = ready
    cid = stage(cfg, idx, "Matuschak notes support this.",
                target="claims/small-tools-beat-frameworks.md",
                section="## Evidence")
    result = promote.approve(cfg, idx, cid)
    assert result["ok"]
    text = read(cfg, "claims/small-tools-beat-frameworks.md")
    evidence = text.split("## Evidence")[1].split("##")[0]
    assert "Matuschak notes support this." in evidence
    assert schema.validate_page("claims/small-tools-beat-frameworks.md",
                                text) == []


def test_reject_then_promote_fails(ready):
    cfg, idx = ready
    cid = stage(cfg, idx, "Dubious speculation about the roadmap.")
    r = promote.reject(cfg, cid, reason="not established")
    assert r["ok"]
    _, fm, _, _ = candidates.load(cfg, cid)
    assert fm["status"] == "rejected"
    assert "not established" in fm["reviewed"]
    assert not promote.promote(cfg, idx, cid)["ok"]
    assert not promote.approve(cfg, idx, cid)["ok"]


def test_approve_without_promote_then_promote(ready):
    cfg, idx = ready
    cid = stage(cfg, idx, "Deploys happen only on Fridays.", kind="preference")
    r = promote.approve(cfg, idx, cid, and_promote=False)
    assert r["ok"] and r["result"] == "approved"
    _, fm, _, _ = candidates.load(cfg, cid)
    assert fm["status"] == "approved"
    r2 = promote.promote(cfg, idx, cid)
    assert r2["ok"] and r2["target"] == "profile/user.md"


def test_approve_with_content_override(ready):
    cfg, idx = ready
    cid = stage(cfg, idx, "user said something roughly like this",
                kind="preference")
    r = promote.approve(cfg, idx, cid,
                        overrides={"content": "Prefers evening deploys."})
    assert r["ok"]
    assert "Prefers evening deploys." in read(cfg, "profile/user.md")
    assert "roughly like this" not in read(cfg, "profile/user.md")


def test_supersede_profile_bullet(ready):
    cfg, idx = ready
    cid = stage(cfg, idx, "Deploys on Thursdays.", kind="preference")
    promote.approve(cfg, idx, cid)
    idx.refresh()
    r = promote.supersede(cfg, idx, cid, "Deploys on Wednesdays now.")
    assert r["ok"] and r["result"] == "replaced" and r["supersedes"] == cid
    new_id = r["id"]
    text = read(cfg, "profile/user.md")
    assert "Deploys on Wednesdays now." in text
    assert "Deploys on Thursdays." not in text
    assert schema.candidate_marker(new_id) in text
    assert schema.candidate_marker(cid) not in text
    assert schema.validate_page("profile/user.md", text) == []
    _, old_fm, _, _ = candidates.load(cfg, cid)
    assert old_fm["status"] == "superseded" and new_id in old_fm["reviewed"]
    _, new_fm, _, _ = candidates.load(cfg, new_id)
    assert new_fm["status"] == "promoted" and new_fm["supersedes"] == cid
    lines = [json.loads(l) for l in
             read(cfg, schema.DECISIONS_FILE).strip().splitlines()]
    assert any(l["action"] == "superseded" and l["id"] == cid for l in lines)


def test_supersede_knowledge_page_is_review_first(ready):
    cfg, idx = ready
    rel = "claims/small-tools-beat-frameworks.md"
    cid = stage(cfg, idx, "Evidence supports this.", target=rel,
                section="## Evidence")
    promote.approve(cfg, idx, cid)
    idx.refresh()
    r = promote.supersede(cfg, idx, cid, "Stronger evidence replaces this.")
    assert r["ok"] and r["result"] == "pending_review"
    assert "Stronger evidence" not in read(cfg, rel)  # review-first
    r2 = promote.promote(cfg, idx, r["id"])
    assert r2["ok"] and r2["result"] == "replaced"
    text = read(cfg, rel)
    assert "Stronger evidence replaces this." in text
    assert "Evidence supports this." not in text
    assert schema.validate_page(rel, text) == []
    _, old_fm, _, _ = candidates.load(cfg, cid)
    assert old_fm["status"] == "superseded"


def test_supersede_guards(ready):
    cfg, idx = ready
    cid = stage(cfg, idx, "Still pending proposal.")
    r = promote.supersede(cfg, idx, cid, "replacement")
    assert not r["ok"] and "pending" in r["error"]
    missing = promote.supersede(cfg, idx, "20990101-000000-deadbeef-01", "x")
    assert not missing["ok"]
    promote.approve(cfg, idx, cid)
    assert not promote.supersede(cfg, idx, cid, "   ")["ok"]


def test_superseded_candidate_cannot_be_reapproved(ready):
    cfg, idx = ready
    cid = stage(cfg, idx, "Deploys on Thursdays.", kind="preference")
    promote.approve(cfg, idx, cid)
    idx.refresh()
    r = promote.supersede(cfg, idx, cid, "Deploys on Wednesdays now.")
    assert r["ok"]
    # its marker is gone from the page; re-approving would re-append stale content
    back = promote.approve(cfg, idx, cid)
    assert not back["ok"] and "superseded" in back["error"]
    text = read(cfg, "profile/user.md")
    assert "Deploys on Thursdays." not in text


def test_supersede_appends_when_marker_hand_deleted(ready):
    cfg, idx = ready
    cid = stage(cfg, idx, "Original preference.", kind="preference")
    promote.approve(cfg, idx, cid)
    path = Path(cfg.wiki_root) / "profile/user.md"
    fm, body = wikifs.parse_frontmatter(wikifs.read_text(path))
    body = "\n".join(l for l in body.splitlines()
                     if schema.candidate_marker(cid) not in l)
    wikifs.atomic_write(path, wikifs.render_page(fm, body))
    idx.refresh()
    r = promote.supersede(cfg, idx, cid, "Replacement preference.")
    assert r["ok"] and r["result"] == "promoted"  # appended, not replaced
    assert "Replacement preference." in read(cfg, "profile/user.md")


def test_decisions_journal_records_lifecycle(ready):
    cfg, idx = ready
    cid = stage(cfg, idx, "Track everything in the audit log.", kind="note")
    promote.approve(cfg, idx, cid)
    lines = [json.loads(l) for l in
             read(cfg, schema.DECISIONS_FILE).strip().splitlines()]
    actions = [l["action"] for l in lines if l["id"] == cid]
    assert actions == ["staged", "approved", "promoted"]
    assert all(l["ts"].endswith("Z") for l in lines)


def test_apply_tool_end_to_end(fixture_wiki, tmp_path):
    import memory_wiki
    provider = memory_wiki.MemoryWikiProvider(
        overrides={"wiki_root": str(fixture_wiki)})
    provider.initialize("8f3e9c12-sess", hermes_home=str(tmp_path),
                        platform="cli")

    def apply(**args):
        return json.loads(provider.handle_tool_call("wiki_apply", args))

    out = apply(action="write_profile", page="user",
                content="Prefers uv over pip", section="## Preferences")
    assert out["ok"] and out["ref"] == "profile/user.md"
    assert "Prefers uv over pip" in provider.system_prompt_block()

    out = apply(action="append_journal", content="Started phase three")
    assert out["ok"] and out["ref"].startswith("journal/")

    out = apply(action="stage", content="Candidate pipelines should stay "
                                        "boring.", kind="claim",
                topics=["memory"])
    assert out["ok"] and out["note"] == "pending human review"
    cid = out["id"]
    dup = apply(action="stage", content="Candidate pipelines should stay "
                                        "boring.")
    assert not dup["ok"] and "duplicate" in dup["error"]

    out = apply(action="approve", id=cid)
    assert out["ok"] and out["target"].startswith("claims/")
    out = apply(action="approve", id=cid)  # re-approve is the idempotent path
    assert out["ok"] and out["result"] == "already_promoted"
    # promote is no longer a public action (review cannot be skipped)
    out = apply(action="promote", id=cid)
    assert not out["ok"] and "unknown action" in out["error"]

    out = apply(action="stage", content="Rejected idea.", kind="note")
    rej = apply(action="reject", id=out["id"], reason="not worth keeping")
    assert rej["ok"]

    assert not apply(action="approve")["ok"]          # missing id
    assert not apply(action="frobnicate")["ok"]       # unknown action
    q = json.loads(provider.handle_tool_call("wiki_get",
                                             {"ref": "review-queue"}))
    assert q["count"] == 0
    dec = json.loads(provider.handle_tool_call("wiki_get",
                                               {"ref": "decisions"}))
    assert dec["ok"] and dec["count"] >= 5


def test_apply_supersede_tool(fixture_wiki, tmp_path):
    import memory_wiki
    provider = memory_wiki.MemoryWikiProvider(
        overrides={"wiki_root": str(fixture_wiki)})
    provider.initialize("8f3e9c12-sess", hermes_home=str(tmp_path),
                        platform="cli")

    def apply(**args):
        return json.loads(provider.handle_tool_call("wiki_apply", args))

    cid = apply(action="stage", content="Prefers npm for everything.",
                kind="preference")["id"]
    out = apply(action="approve", id=cid)
    assert out["ok"] and out["target"] == "profile/user.md"
    assert "Prefers npm for everything." in provider.system_prompt_block()

    out = apply(action="supersede", id=cid, content="Now prefers pnpm.")
    assert out["ok"] and out["result"] == "replaced"
    assert out["supersedes"] == cid
    block = provider.system_prompt_block()
    assert "Now prefers pnpm." in block
    assert "Prefers npm for everything." not in block
    assert not apply(action="supersede", id=cid, content="again")["ok"]
