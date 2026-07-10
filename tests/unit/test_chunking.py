import pytest

from jarvis.memory.chunking import chunk_text


def test_empty_and_whitespace_text_yields_no_chunks():
    assert chunk_text("", max_chars=100) == []
    assert chunk_text("   \n\n  ", max_chars=100) == []


def test_short_text_is_a_single_chunk():
    assert chunk_text("hello world", max_chars=100) == ["hello world"]


def test_paragraphs_pack_into_chunks_without_splitting():
    text = "para one.\n\npara two.\n\npara three."
    chunks = chunk_text(text, max_chars=25)
    assert chunks == ["para one.\n\npara two.", "para three."]


def test_long_paragraph_is_hard_split_with_overlap():
    text = "abcdefghij" * 10  # 100 chars, no paragraph breaks
    chunks = chunk_text(text, max_chars=40, overlap=10)
    assert all(len(c) <= 40 for c in chunks)
    # step is 30, so consecutive chunks share their 10-char boundary
    assert chunks[0][-10:] == chunks[1][:10]
    # every character is covered
    assert chunks[0] + "".join(c[10:] for c in chunks[1:]) == text


def test_invalid_arguments_raise():
    with pytest.raises(ValueError):
        chunk_text("x", max_chars=0)
    with pytest.raises(ValueError):
        chunk_text("x", max_chars=10, overlap=10)
    with pytest.raises(ValueError):
        chunk_text("x", max_chars=10, overlap=-1)
