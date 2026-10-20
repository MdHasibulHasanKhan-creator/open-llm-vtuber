"""Long-term memory: extraction, the profile store, and prompt rendering.

The feature under test is "it remembers you". Two failure modes matter more than
the happy path, so most of these tests are about them: recording *garbage* (a
memory that is wrong is worse than none) and losing or clobbering an existing
profile.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.memory import ProfileStore, extract, topical_words
from app.memory.facts import ExtractedFacts


# --------------------------------------------------------------------------- #
#  Extraction (the part that can quietly learn nonsense)
# --------------------------------------------------------------------------- #
def test_extracts_a_name():
    facts = extract("Hi, my name is Rahim and I work in Dhaka.")
    assert facts.name == "Rahim"


def test_extracts_a_like_without_swallowing_the_whole_sentence():
    facts = extract("I really love coffee and long walks but not mornings.")
    # The clause boundary matters: "coffee" is the like, not the rest of it.
    assert facts.likes
    assert facts.likes[0] == "coffee"


def test_extracts_a_dislike():
    facts = extract("Honestly I can't stand early mornings.")
    assert facts.dislikes == ["early mornings"]


def test_extracts_a_job():
    facts = extract("I work as a teacher during the week.")
    assert facts.facts.get("works_as") == "teacher"


def test_extracts_where_they_live():
    facts = extract("I'm from Dhaka originally.")
    assert facts.facts.get("from") == "Dhaka"


def test_extracts_a_bengali_name_and_like():
    # Bengali is object-before-verb; these rules look ahead for the verb.
    assert extract("আমার নাম রহিম").name == "রহিম"
    assert extract("আমি কফি ভালোবাসি").likes == ["কফি"]


def test_a_bare_stopword_capture_is_discarded():
    # "I like to be" must not remember the value "be".
    facts = extract("I like to be outside.")
    assert "be" not in facts.likes
    assert facts.facts == {}
    assert facts.name == ""


def test_nothing_learned_from_noise():
    facts = extract("hmm okay sure")
    assert not facts.likes and not facts.dislikes and not facts.name


def test_topical_words_keep_non_latin_scripts():
    # `quality.content_tokens` deliberately keeps only [0-9a-z']; memory must not.
    words = topical_words("আমার প্রিয় খাবার ভাত")
    assert any("\u0980" <= ch <= "\u09ff" for word in words for ch in word)


# --------------------------------------------------------------------------- #
#  The store
# --------------------------------------------------------------------------- #
def test_observe_then_describe(tmp_path: Path):
    store = ProfileStore(tmp_path / "profile.json")
    learned = store.observe("My name is Rahim and I love coffee.")
    assert any(item.startswith("name=") for item in learned)
    assert any(item.startswith("like=") for item in learned)

    block = store.describe()
    assert "Rahim" in block
    assert "coffee" in block


def test_describe_is_empty_when_nothing_is_known(tmp_path: Path):
    assert ProfileStore(tmp_path / "profile.json").describe() == ""


def test_the_file_round_trips(tmp_path: Path):
    path = tmp_path / "profile.json"
    ProfileStore(path).observe("My name is Ada, I love tea.")

    again = ProfileStore(path)
    assert again.profile.name == "Ada"
    assert "tea" in again.profile.likes
    # And the on-disk shape is the documented one.
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["name"] == "Ada"


def test_likes_are_deduplicated_case_insensitively(tmp_path: Path):
    store = ProfileStore(tmp_path / "profile.json")
    store.observe("I love coffee.")
    store.observe("I love Coffee.")
    assert [item.casefold() for item in store.profile.likes].count("coffee") == 1


def test_turns_are_counted_and_clear_resets(tmp_path: Path):
    store = ProfileStore(tmp_path / "profile.json")
    store.observe("hello there friend")
    store.observe("how are you doing today")
    assert store.profile.turns == 2

    store.clear()
    assert store.profile.turns == 0
    assert store.describe() == ""


def test_summary_is_capped_and_rendered(tmp_path: Path):
    store = ProfileStore(tmp_path / "profile.json")
    store.set_summary("They are working on a deadline. " * 40)
    assert len(store.profile.summary) <= 400
    assert "deadline" in store.describe()


def test_external_edits_are_picked_up(tmp_path: Path):
    """The file is documented as hand-editable; the store must notice."""
    path = tmp_path / "profile.json"
    store = ProfileStore(path)
    store.observe("I love coffee.")
    assert store.profile.name == ""

    data = json.loads(path.read_text(encoding="utf-8"))
    data["name"] = "Edited"
    path.write_text(json.dumps(data), encoding="utf-8")

    store.observe("and I like tea")
    assert store.profile.name == "Edited"


def test_a_corrupt_file_does_not_crash(tmp_path: Path):
    path = tmp_path / "profile.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = ProfileStore(path)  # must not raise
    assert store.profile.turns == 0
    store.observe("My name is Ada")
    assert store.profile.name == "Ada"


def test_snapshot_shape_for_the_offline_brain(tmp_path: Path):
    store = ProfileStore(tmp_path / "profile.json")
    store.observe("My name is Ada, I love tea.")
    snapshot = store.snapshot()
    assert set(snapshot) == {"name", "likes", "dislikes", "topics", "turns"}


def test_extracted_facts_helper_is_honest_when_empty():
    assert ExtractedFacts().empty() is True
    assert ExtractedFacts(name="Ada").empty() is False
