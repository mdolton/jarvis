"""Sensitivity signal for approval decisions.

Terms come from memory preferences of the form ``sensitive: a, b; c``
(case-insensitive marker, comma/semicolon-separated terms). A tool call
whose arguments or name contain any term escalates to confirm — the memory
layer makes escalation context-aware instead of a static tool list.
"""

import json
import re
from collections.abc import Iterable, Sequence

_MARKER = re.compile(r"^\s*sensitive\s*:\s*(?P<terms>.+)$", re.IGNORECASE | re.DOTALL)


def extract_sensitivity_terms(preferences: Iterable[str]) -> list[str]:
    """Collect sensitive terms from preference contents, lowercased and deduped."""
    terms: list[str] = []
    for content in preferences:
        match = _MARKER.match(content or "")
        if match is None:
            continue
        for raw in re.split(r"[,;]", match.group("terms")):
            term = raw.strip().lower()
            if term and term not in terms:
                terms.append(term)
    return terms


def find_sensitive_match(
    terms: Sequence[str],
    *,
    tool_name: str = "",
    arguments: dict | None = None,
) -> str | None:
    """Return the first term found in the tool name or serialized arguments."""
    if not terms:
        return None
    haystack = tool_name.lower()
    if arguments:
        try:
            haystack += " " + json.dumps(arguments, default=str).lower()
        except (TypeError, ValueError):
            haystack += " " + str(arguments).lower()
    for term in terms:
        if term in haystack:
            return term
    return None
