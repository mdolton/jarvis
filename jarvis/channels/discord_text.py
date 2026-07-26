"""Discord message-size helpers, shared by the adapter and the stream.

Discord rejects any message body over 2000 characters with a 400 (error code
50035, "Invalid Form Body — In content: Must be 2000 or fewer in length").
That is a hard API limit, not a soft preference, so every outbound path has to
split ahead of the send rather than hope the text is short.
"""

MESSAGE_LIMIT = 2000


def chunk_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split `text` into Discord-sized messages, preferring line boundaries.

    Briefs are markdown — bullet lists, headings, emoji — so a blind split at
    the character limit lands mid-word and mangles the formatting across the
    seam. Packing whole lines keeps each message readable on its own. A single
    line longer than `limit` has no boundary to break on and is hard-split.

    Returns [] for empty or whitespace-only text; callers decide what an empty
    result means rather than having a silent no-op forced on them.
    """
    text = text.strip()
    if not text:
        return []

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
