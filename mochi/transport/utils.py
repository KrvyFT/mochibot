"""Shared transport utilities — bubble splitting & marker cleaning.

Used by both Telegram and WeChat transports to avoid code duplication.
"""

import re

# ── Marker cleaning ─────────────────────────────────────────────────────────
# Side-channel markers embedded in LLM replies (sticker, image, etc.)

_IMAGE_FILE_RE = re.compile(r"\[IMAGE_FILE:[^\]]+\]")
_STICKER_RE = re.compile(r"\[STICKER:[^\]]+\]")
_VOICE_FILE_RE = re.compile(r"\[VOICE_FILE:[^\]]+\]")
# History prefixes look like `[2026-09-02 22:01] `. Strip only at line start
# of outgoing bubbles; leave the same pattern untouched mid-sentence.
_HISTORY_TIMESTAMP_LINE_PREFIX_RE = re.compile(
    r"^[ \t]*\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\][ \t]*",
    re.MULTILINE,
)
_FULLWIDTH_PAREN_RE = re.compile(r"（[^）]*）")
_ASCII_CJK_PAREN_RE = re.compile(r"\(([^)]*[\u4e00-\u9fff][^)]*)\)")


def normalize_legacy_bubble_delimiters(text: str) -> str:
    """Turn legacy reply separators into natural paragraphs outside code."""
    segments = re.split(r"(```.*?```|`[^`\n]*`)", text, flags=re.DOTALL)
    for index in range(0, len(segments), 2):
        segments[index] = re.sub(r"\s*\|\|\|\s*", "\n\n", segments[index])
    return re.sub(r"\n{3,}", "\n\n", "".join(segments)).strip()


def strip_outgoing_history_timestamps(text: str) -> str:
    """Remove history-style timestamps only at the start of outgoing lines.

    The model still *reads* `[YYYY-MM-DD HH:MM]` prefixes in conversation
    history. Copied prefixes must not appear as the first tokens of a bubble
    sent to the owner. A timestamp in the middle of a sentence is left as-is.
    """
    if not text:
        return text
    return _HISTORY_TIMESTAMP_LINE_PREFIX_RE.sub("", text).strip()


def strip_stage_directions(text: str) -> str:
    """Drop parenthetical action/voice narration from outgoing chat."""
    if not text:
        return text
    parts = re.split(r"(```.*?```)", text, flags=re.DOTALL)
    for index in range(0, len(parts), 2):
        chunk = _FULLWIDTH_PAREN_RE.sub("", parts[index])
        chunk = _ASCII_CJK_PAREN_RE.sub("", chunk)
        chunk = re.sub(r"[ \t]+\n", "\n", chunk)
        chunk = re.sub(r"\n{3,}", "\n\n", chunk)
        chunk = re.sub(r"[ \t]{2,}", " ", chunk)
        parts[index] = chunk
    return "".join(parts).strip()


def clean_reply_markers(text: str) -> str:
    """Strip side-channel markers from LLM reply text.

    Removes image and sticker markers handled before transport delivery.
    Runtime silence is resolved before replies reach this layer.
    """
    text = _IMAGE_FILE_RE.sub("", text)
    text = _STICKER_RE.sub("", text)
    text = _VOICE_FILE_RE.sub("", text)
    text = strip_stage_directions(text)
    return normalize_legacy_bubble_delimiters(text)


# ── System command presentation ─────────────────────────────────────────────

def format_usage_summary(summary: dict) -> str:
    """Format the shared /cost response for chat transports."""
    blocks = []
    for title, period in (("📊 今日", summary["today"]), ("📊 本月", summary["month"])):
        lines = [f"{title} · 总计 {period['total']:,} tokens"]
        by_model = period["by_model"]
        if not by_model:
            lines.append("  (无记录)")
        for model, data in sorted(by_model.items()):
            lines.append(f"  {model}")
            line = (
                f"    input {data['prompt']:,}  |  output {data['completion']:,}"
            )
            if data.get("reasoning", 0) > 0:
                line += f"  (其中 reasoning {data['reasoning']:,})"
            lines.append(line)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ── Text splitting ──────────────────────────────────────────────────────────

def split_text(text: str, limit: int) -> list[str]:
    """Split text into chunks respecting a character limit."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


def split_bubbles(text: str, max_bubbles: int = 8,
                  delimiter: str = "|||",
                  min_chars: int = 8) -> list[str]:
    """Split text into chat bubbles for a natural multi-message feel.

    Primary split: natural double-newline paragraphs.
    Compatibility fallback: configured legacy delimiter when no paragraphs exist.
    Merge short fragments into previous bubble.
    """
    if "\n\n" in text:
        parts: list[str] = []
        current: list[str] = []
        fence: str | None = None
        for line in text.splitlines(keepends=True):
            stripped = line.lstrip()
            marker = (
                "```" if stripped.startswith("```")
                else "~~~" if stripped.startswith("~~~")
                else None
            )
            if marker:
                if fence is None:
                    fence = marker
                elif fence == marker:
                    fence = None
                current.append(line)
                continue
            if fence is None and not line.strip():
                paragraph = "".join(current).strip()
                if paragraph:
                    parts.append(paragraph)
                current = []
            else:
                current.append(line)
        paragraph = "".join(current).strip()
        if paragraph:
            parts.append(paragraph)
    elif delimiter and delimiter in text:
        parts = [p.strip() for p in text.split(delimiter) if p.strip()]
    else:
        parts = [text.strip()]

    if len(parts) <= 1:
        single = strip_stage_directions(strip_outgoing_history_timestamps(text))
        return [single] if single else [""]

    # Merge short fragments into previous bubble
    bubbles: list[str] = [parts[0]]
    for part in parts[1:]:
        if len(part) < min_chars:
            bubbles[-1] += "\n\n" + part
        else:
            bubbles.append(part)

    limit = max(1, int(max_bubbles))
    if len(bubbles) > limit:
        bubbles = (
            bubbles[:limit - 1]
            + ["\n\n".join(bubbles[limit - 1:])]
        )
    cleaned: list[str] = []
    for bubble in bubbles:
        visible = strip_stage_directions(
            strip_outgoing_history_timestamps(bubble),
        )
        if visible:
            cleaned.append(visible)
    return cleaned or [""]
