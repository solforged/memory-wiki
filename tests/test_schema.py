import pytest

from memory_wiki import schema, wikifs


def render(ntype, title, relname=None, topics=("t",), extras=None, body="Body.\n"):
    fm = schema.page_frontmatter(ntype, title, topics=list(topics),
                                 created="2026-07-01", extras=extras)
    return wikifs.render_page(fm, body)


@pytest.mark.parametrize("ntype,relpath,extras", [
    ("concept", "concepts/zettelkasten.md", {"aliases": ["slip-box"]}),
    ("claim", "claims/x-beats-y.md", None),
    ("source", "sources/some-post.md", {"author": "Jane", "medium": "post"}),
    ("atlas", "atlases/engineering.md", None),
    ("synthesis", "syntheses/big-essay.md", None),
    ("ref", "refs/git-cheatsheet.md", {"url": "https://x.test"}),
    ("profile", "profile/user.md", None),
    ("journal", "journal/2026-07-05.md", None),
    ("taste", "tastes/tastes.md", None),
])
def test_valid_pages_per_type(ntype, relpath, extras):
    title = relpath.split("/")[1][:-3].replace("-", " ")
    text = render(ntype, title, extras=extras)
    assert schema.validate_page(relpath, text) == []


def test_journal_date_filename_passes_kebab():
    assert schema.KEBAB.match("2026-07-05.md")


def test_claim_page_emits_stance():
    fm = schema.page_frontmatter("claim", "x", created="2026-07-01")
    assert fm["stance"] == "candidate"
    keys = list(fm)
    assert keys.index("topics") < keys.index("stance") < keys.index("confidence")
    fm2 = schema.page_frontmatter("claim", "x", created="2026-07-01",
                                  extras={"stance": "external"})
    assert fm2["stance"] == "external"


def test_claim_missing_or_bad_stance_fails():
    fm = schema.page_frontmatter("claim", "x", created="2026-07-01")
    del fm["stance"]
    errs = schema.validate_page("claims/x.md", wikifs.render_page(fm, "b\n"))
    assert any("'stance'" in e for e in errs)
    fm["stance"] = "hunch"
    errs = schema.validate_page("claims/x.md", wikifs.render_page(fm, "b\n"))
    assert any("stance 'hunch'" in e for e in errs)


def test_unassessed_confidence_is_valid_on_pages():
    fm = schema.page_frontmatter("claim", "x", created="2026-07-01",
                                 extras={"confidence": "unassessed"})
    assert schema.validate_page("claims/x.md",
                                wikifs.render_page(fm, "b\n")) == []
    assert "unassessed" not in schema.CONFIDENCE  # candidates stay assessed


def test_wrong_folder_type_mismatch():
    text = render("claim", "x")
    errs = schema.validate_page("concepts/x.md", text)
    assert any("type 'claim'" in e for e in errs)


def test_bad_enum_and_date():
    fm = schema.page_frontmatter("claim", "x", created="2026-07-01")
    fm["confidence"] = "very-sure"
    fm["createdAt"] = "July 1"
    errs = schema.validate_page("claims/x.md", wikifs.render_page(fm, "b\n"))
    assert any("confidence" in e for e in errs)
    assert any("ISO date" in e for e in errs)


def test_legacy_created_date_remains_valid():
    fm = schema.page_frontmatter("concept", "x", created="2026-07-01")
    fm["created"] = fm.pop("createdAt")
    assert schema.validate_page(
        "concepts/x.md", wikifs.render_page(fm, "b\n")) == []


def test_missing_required_extra():
    fm = schema.page_frontmatter("source", "x", extras={"author": "", "medium": ""})
    errs = schema.validate_page("sources/x.md", wikifs.render_page(fm, "b\n"))
    assert any("'author'" in e for e in errs)
    assert any("'medium'" in e for e in errs)


def test_non_kebab_filename():
    text = render("concept", "X")
    errs = schema.validate_page("concepts/Not_Kebab.md", text)
    assert any("kebab" in e for e in errs)


def test_nested_or_unknown_dir_rejected():
    text = render("concept", "x")
    assert schema.validate_page("concepts/sub/x.md", text)
    assert schema.validate_page("random/x.md", text)


def test_profile_seed_pages_validate():
    for fname in ("user.md", "agent.md"):
        fm, body = schema.profile_seed(fname, "2026-07-01")
        assert schema.validate_page(f"profile/{fname}",
                                    wikifs.render_page(fm, body)) == []


def test_journal_frontmatter_validates():
    fm = schema.journal_frontmatter("2026-07-05")
    text = wikifs.render_page(fm, schema.journal_seed_body())
    assert schema.validate_page("journal/2026-07-05.md", text) == []


def test_validate_candidate():
    good = {"id": "20260705-120000-abcd1234-01", "status": "pending",
            "kind": "preference", "confidence": "plausible",
            "origin": "sync_turn", "created": "2026-07-05",
            "fields": {"author": "x"}}
    assert schema.validate_candidate(good) == []
    bad = dict(good, status="maybe", fields={"nope": 1})
    errs = schema.validate_candidate(bad)
    assert any("status" in e for e in errs)
    assert any("allowlist" in e for e in errs)
    assert any("missing" in e for e in schema.validate_candidate({}))
