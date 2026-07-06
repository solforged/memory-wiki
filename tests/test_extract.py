from memory_wiki import extract


def kinds(text):
    return [(i["kind"], i["content"]) for i in extract.extract(text)]


def test_preference_detection():
    out = kinds("I prefer uv over pip for everything python.")
    assert out and out[0][0] == "preference"


def test_correction_beats_preference():
    out = kinds("Actually, I prefer the dark theme after all.")
    assert out[0][0] == "correction"


def test_decision_and_fact():
    out = kinds("Let's go with sqlite-free storage for this project. "
                "My timezone is Europe/Dublin by the way.")
    got = dict(out)
    assert "decision" in got and "fact" in got


def test_questions_and_short_lines_skipped():
    assert kinds("Do I prefer uv or pip?") == []
    assert kinds("I like it.") == []          # under min length
    assert kinds("ok thanks") == []


def test_code_blocks_and_pastes_skipped():
    fenced = "I always run this:\n```\npip install -e . # I prefer pip here\n```"
    out = kinds(fenced)
    assert all("pip install" not in c for _, c in out)
    assert extract.extract("word " * 1500) == []   # paste-sized input


def test_long_sentence_trimmed():
    out = kinds("I always deploy on Fridays because " + "reason " * 60)
    assert out and len(out[0][1]) <= 304 and out[0][1].endswith("...")


def test_duplicate_sentences_collapse():
    out = kinds("I prefer tabs over spaces. I prefer tabs over spaces.")
    assert len(out) == 1


def test_bullets_are_unwrapped():
    out = kinds("- I always review PRs before merging them")
    assert out and not out[0][1].startswith("-")


def test_message_text_variants():
    assert extract.message_text({"content": "plain"}) == "plain"
    assert extract.message_text(
        {"content": [{"type": "text", "text": "a"}, {"text": "b"}]}) == "a b"
    assert extract.message_text({"content": None}) == ""


def test_excerpt():
    assert extract.excerpt("  spaced   out  ") == "spaced out"
    long = extract.excerpt("word " * 100, limit=50)
    assert len(long) <= 54 and long.endswith("...")
