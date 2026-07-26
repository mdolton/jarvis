"""chunk_message — Discord's 2000-char body limit is a hard API constraint."""

from jarvis.channels.discord_text import MESSAGE_LIMIT, chunk_message


def test_empty_and_whitespace_yield_no_chunks():
    assert chunk_message("") == []
    assert chunk_message("   \n\n  ") == []


def test_short_text_is_one_chunk_and_is_stripped():
    assert chunk_message("  hello world\n") == ["hello world"]


def test_text_at_the_limit_is_not_split():
    text = "x" * MESSAGE_LIMIT
    assert chunk_message(text) == [text]


def test_every_chunk_respects_the_limit():
    text = "\n".join(f"line {i} " + "y" * 200 for i in range(60))
    chunks = chunk_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= MESSAGE_LIMIT for c in chunks)


def test_splits_on_line_boundaries_not_mid_word():
    """A brief is markdown; splitting mid-bullet mangles it across the seam."""
    lines = [f"- bullet {i} " + "z" * 100 for i in range(40)]
    chunks = chunk_message("\n".join(lines))
    assert len(chunks) > 1
    for chunk in chunks:
        for line in chunk.split("\n"):
            assert line in lines


def test_no_content_is_lost_across_the_seam():
    lines = [f"- bullet {i} " + "w" * 100 for i in range(40)]
    text = "\n".join(lines)
    assert "\n".join(chunk_message(text)) == text


def test_a_single_overlong_line_is_hard_split():
    """No boundary to break on — the stream path has always done this and its
    test asserts the exact shape."""
    assert chunk_message("x" * 2500) == ["x" * 2000, "x" * 500]


def test_overlong_line_after_a_short_one_flushes_first():
    chunks = chunk_message("intro\n" + "q" * 2500)
    assert chunks == ["intro", "q" * 2000, "q" * 500]


def test_blank_lines_between_paragraphs_survive():
    text = "para one\n\npara two"
    assert chunk_message(text) == [text]
