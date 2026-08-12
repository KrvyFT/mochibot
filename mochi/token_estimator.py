"""Deterministic token estimates for context-budget protection."""

from __future__ import annotations

import math


def _is_cjk(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def estimate_tokens(text: str) -> int:
    """Return a conservative tokenizer-independent count for budget checks."""
    if not text:
        return 0

    cjk = ascii_chars = other = 0
    for char in text:
        if _is_cjk(char):
            cjk += 1
        elif char.isascii():
            ascii_chars += 1
        else:
            other += 1
    return math.ceil(cjk + other + ascii_chars / 4)


def truncate_to_token_budget(
    text: str,
    max_tokens: int,
    *,
    suffix: str = "…",
) -> str:
    """Return the longest prefix whose conservative estimate fits the budget."""
    budget = max(0, int(max_tokens))
    if estimate_tokens(text) <= budget:
        return text
    if budget == 0 or estimate_tokens(suffix) > budget:
        return ""
    content_budget = budget - estimate_tokens(suffix)
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= content_budget:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + suffix
