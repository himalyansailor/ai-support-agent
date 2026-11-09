from supportcore.voice.speech_text import (
    MAX_SPOKEN_CHARS,
    SentenceSplitter,
    cap_spoken,
    to_speech_text,
)


def test_to_speech_text_strips_markdown_and_links():
    md = (
        "# Refunds\n\nYou can **request** a refund in __Billing__ with `code`.\n"
        "- First step\n- Second step\n\nSee [our policy](https://x.com/p) or https://x.com/more now.\n"
        "```\nprint(1)\n```\nDone *really*."
    )
    out = to_speech_text(md)
    for bad in ("**", "__", "`", "#", "http", "](", "print(1)"):
        assert bad not in out
    assert "See our policy or now." in out
    assert "First step." in out and "Second step." in out
    assert "  " not in out and "\n" not in out
    assert out.endswith("Done really.")


def test_splitter_streams_sentences():
    s = SentenceSplitter()
    assert s.feed("Hi there. How") == ["Hi there."]
    assert s.feed(" are you? Fine") == ["How are you?"]
    assert s.flush() == "Fine"
    assert s.flush() is None


def test_splitter_does_not_split_decimals():
    s = SentenceSplitter()
    assert s.feed("It costs 4.50 dollars.") == []
    assert s.flush() == "It costs 4.50 dollars."


def test_splitter_splits_on_newline_and_long_runs():
    s = SentenceSplitter()
    assert s.feed("one\ntwo") == ["one"]
    splitter = SentenceSplitter()
    out = splitter.feed("word " * 60)
    tail = splitter.flush()
    assert out and all(len(x) <= 200 for x in out)
    assert " ".join([*out, tail or ""]) == ("word " * 60).strip()


def test_splitter_allows_closing_quote_after_sentence_end():
    s = SentenceSplitter()
    assert s.feed('He said "Hi." Then') == ['He said "Hi."']
    assert s.flush() == "Then"


def test_cap_spoken_cuts_at_sentence():
    text = "First one. Second one. Third one."
    assert cap_spoken(text, 25) == "First one. Second one."
    assert cap_spoken("short.", 25) == "short."
    assert cap_spoken("aaaa bbbb cccc", 9) == "aaaa bbbb"


def test_max_spoken_chars():
    assert MAX_SPOKEN_CHARS == 1200


def test_url_keeps_trailing_sentence_punctuation():
    assert to_speech_text("Visit https://x.com/a. Then go.") == "Visit. Then go."
    assert to_speech_text("(see https://x.com/a), ok") == "(see), ok"


def test_strips_rules_quotes_tables_html_escapes_strike():
    assert to_speech_text("Intro\n\n---\n\n***\n\nOutro") == "Intro Outro"
    assert to_speech_text("> quoted text") == "quoted text"
    table = "| Plan | Price |\n|---|---|\n| Pro | 10 |"
    assert to_speech_text(table) == "Plan, Price. Pro, 10."
    assert to_speech_text("~~gone~~ <b>bold</b> a\\*lit\\*") == "gone bold a*lit*"
    assert "|" not in to_speech_text("a | b")


def test_arithmetic_asterisks_survive():
    assert to_speech_text("2*3*4 and 5 * 3") == "2*3*4 and 5 * 3"
    assert to_speech_text("a *really* b") == "a really b"


def test_headings_get_sentence_break_and_emoji_removed():
    assert to_speech_text("## Head\n* star item") == "Head. star item."
    assert to_speech_text("Hello \U0001F600 there \u2705!") == "Hello there!"


def test_marker_only_and_fence_only_sentences_are_empty():
    for frag in ("1.", "2)", "-", "*", "```", "```python", "  ```  "):
        assert to_speech_text(frag) == ""


def test_splitter_then_to_speech_text_chain_is_safe():
    s = SentenceSplitter()
    parts = s.feed("1. Open settings\n2. Click save.\n")
    tail = s.flush()
    if tail:
        parts.append(tail)
    spoken = [t for t in (to_speech_text(p) for p in parts) if t]
    assert spoken == ["Open settings", "Click save."]
