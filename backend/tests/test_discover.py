"""Model discovery, free-provider presets, and the offline brain's context.

These are the two halves of the fix for "the replies are a fixed script": find a
real model if one is running (``discover``), and - when there is genuinely no
model - make the offline brain react to what was actually asked.
"""

from __future__ import annotations

from app.llm.discover import (
    LOCAL_ENDPOINTS,
    _ollama_models,
    _openai_models,
    discover,
    free_presets,
    pick_model,
)
from app.llm.echo import _classify, _topic
from app.memory.facts import topical_words
from app.pipeline.language import detect_language
from app.pipeline.quality import is_echo


# --------------------------------------------------------------------------- #
#  Discovery helpers (pure - no sockets)
# --------------------------------------------------------------------------- #
def test_ollama_model_list_is_read_from_the_native_shape():
    payload = {"models": [{"name": "llama3.1:8b"}, {"name": "qwen2.5:7b"}]}
    assert _ollama_models(payload) == ["llama3.1:8b", "qwen2.5:7b"]


def test_openai_model_list_is_read_from_the_standard_shape():
    payload = {"data": [{"id": "local-model"}, {"id": "other"}]}
    assert _openai_models(payload) == ["local-model", "other"]


def test_malformed_model_lists_yield_nothing():
    assert _ollama_models({"models": "nope"}) == []
    assert _openai_models({"data": [{"nope": 1}]}) == []
    assert _ollama_models(None) == []


def test_pick_model_prefers_a_chat_model():
    assert pick_model(["nomic-embed-text", "llama3.1:8b"]) == "llama3.1:8b"


def test_pick_model_never_returns_an_embedding_or_tts_model():
    assert pick_model(["nomic-embed-text", "whisper-large"]) == ""
    assert pick_model([]) == ""


def test_presets_put_the_private_local_option_first():
    presets = free_presets()
    assert presets[0]["id"] == "ollama"
    assert presets[0]["needs_key"] is False
    for preset in presets:
        assert {"id", "label", "provider", "base_url", "model", "needs_key", "note"} <= set(preset)
        if preset["needs_key"]:
            assert preset.get("key_url"), preset["id"]


def test_free_hosted_presets_all_use_the_openai_provider():
    for preset in free_presets():
        if preset["id"] in ("groq", "gemini", "openrouter"):
            assert preset["provider"] == "openai"
            assert preset["base_url"].startswith("https://")


def test_discover_with_everything_excluded_does_no_io():
    import asyncio

    everything = [endpoint.base_url for endpoint in LOCAL_ENDPOINTS]
    assert asyncio.run(discover(exclude=everything)) == []


# --------------------------------------------------------------------------- #
#  The offline brain reacts to the question
# --------------------------------------------------------------------------- #
def test_classifies_question_words():
    assert _classify("how do I fix my deadlines?") == "question_how"
    assert _classify("why is the sky blue?") == "question_why"
    assert _classify("what is a monad") == "question_what"
    assert _classify("do you like coffee") == "question_yesno"


def test_classifies_the_social_moves():
    assert _classify("hello there") == "greeting"
    assert _classify("thanks!") == "thanks"
    assert _classify("bye") == "farewell"
    assert _classify("ok") == "affirmation"


def test_topic_is_the_salient_word_not_a_question_word():
    assert _topic("how do I fix my deadlines?") == "deadlines"
    assert _topic("hello there") == ""


def test_topic_prefers_a_proper_noun_over_a_longer_common_word():
    # "capital" is longer than "France", but France is what the question is about.
    assert _topic("what is the capital of France") == "France"
    assert _topic("I am flying to Dhaka tomorrow") == "Dhaka"


def test_bengali_questions_and_topics():
    # Bengali question words appear inside the sentence, not only at the start.
    assert _classify("আমি কীভাবে শিখব").startswith("question_")
    assert _topic("আমি কফি ভালোবাসি") == "কফি"


def test_offline_reply_references_the_topic_and_is_not_an_echo():
    """The whole point: no reply may be judged a parroting copy of the input."""
    inputs = [
        "how do I fix my deadlines?",
        "what is the capital of France?",
        "I really love drinking coffee in the morning before work",
        "my team keeps missing the sprint goal",
        "why is the deploy always broken on Fridays?",
        "ok",
        "hello",
        "thanks",
        "I hate early mornings",
    ]
    for text in inputs:
        reply = _compose(text)
        assert reply.strip(), text
        assert not is_echo(reply, text), f"{text!r} -> {reply!r}"


def test_offline_reply_picks_up_the_topic():
    reply = _compose("how do I fix my deadlines?")
    assert "deadline" in reply.casefold()


def test_a_like_from_memory_can_be_called_back():
    llm = _echo()
    llm.set_context({"profile": {"name": "Rahim", "likes": ["coffee"], "dislikes": []}})
    # Turn 3 is where the memory pool is allowed to fire (rationed on purpose).
    llm._turn = 3
    reply = llm._compose("I love coffee")
    assert "coffee" in reply.casefold()
    assert "fond" in reply.casefold() or "forget" in reply.casefold()


def test_a_known_name_is_used_in_greetings():
    llm = _echo()
    llm.set_context({"profile": {"name": "Rahim", "likes": [], "dislikes": []}})
    llm._turn = 2
    assert "Rahim" in llm._compose("hello there")


def test_echo_brain_never_says_the_same_thing_twice_in_a_row():
    llm = _echo()
    previous = ""
    for _ in range(24):
        reply = llm._compose("tell me about the project")
        assert reply != previous
        previous = reply


# --------------------------------------------------------------------------- #
#  Language detection
# --------------------------------------------------------------------------- #
def test_detect_language_recognises_the_scripts_we_promise():
    assert detect_language("hello there friend") == "en"
    assert detect_language("আমি ভালো আছি") == "bn"
    assert detect_language("मैं ठीक हूँ") == "hi"
    assert detect_language("こんにちは") == "ja"


def test_detect_language_returns_nothing_for_non_language():
    assert detect_language("") == ""
    assert detect_language("   ") == ""
    assert detect_language("123 !!! ???") == ""


# --------------------------------------------------------------------------- #
#  Small shims
# --------------------------------------------------------------------------- #
def _echo():
    from app.config import LLMConfig
    from app.llm.echo import EchoLLM

    return EchoLLM(LLMConfig())


def _compose(text: str) -> str:
    return _echo()._compose(text)


def test_topical_words_are_shared_with_memory():
    # One tokenizer, so "what the brain talks about" and "what memory keeps"
    # cannot drift apart.
    assert topical_words("the deadline for the release")[0] == "deadline"
