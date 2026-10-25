"""Confession segment parsing for UIT Confessions posts."""

from __future__ import annotations

import re
from dataclasses import dataclass

PARSER_VERSION = "2.1.0"

# Match confession headers at the start of a line (standalone "#3694" or inline "#2518: text" / "#2518 text")
# Enforces atomic digit boundary (?!\d) and handles CRLF without backtracking into digits.
_HEADER_RE = re.compile(
    r"(?m)^[ \t]*#(\d+)(?!\d)(?:[ \t]*[:\.\-–—][ \t]*|[ \t]+(?=\S)|[ \t]*(?=\r?$|\r?\n))"
)
_SEPARATOR_LINE_RE = re.compile(r"[\r\n]+[ \t]*[-=_]{3,}[ \t]*[\r\n]*$")
_TRAILING_PAGE_TAGS_RE = re.compile(
    r"[\r\n]+#(?:UITconfessions|UIT|cfs\w*)\b.*$",
    flags=re.IGNORECASE,
)


@dataclass(slots=True, frozen=True)
class ConfessionSegment:
    index: int
    confession_number: str
    text: str
    raw_header: str
    start_char: int
    end_char: int


@dataclass(slots=True, frozen=True)
class ConfessionParseResult:
    segments: list[ConfessionSegment]
    is_confession: bool
    status: str  # 'parsed', 'skipped_no_confession', 'needs_review'
    reason: str | None  # None, 'ambiguous_confession_parse', 'incomplete_post'
    parser_version: str


def parse_confessions_full(
    post_text: str, is_incomplete: bool = False
) -> ConfessionParseResult:
    """Parse post text into confession segments with ambiguity and completeness checks."""
    if not post_text:
        if is_incomplete:
            return ConfessionParseResult(
                segments=[],
                is_confession=False,
                status="needs_review",
                reason="incomplete_post",
                parser_version=PARSER_VERSION,
            )
        return ConfessionParseResult(
            segments=[],
            is_confession=False,
            status="skipped_no_confession",
            reason=None,
            parser_version=PARSER_VERSION,
        )

    matches = list(_HEADER_RE.finditer(post_text))
    if not matches:
        if is_incomplete:
            return ConfessionParseResult(
                segments=[],
                is_confession=False,
                status="needs_review",
                reason="incomplete_post",
                parser_version=PARSER_VERSION,
            )
        return ConfessionParseResult(
            segments=[],
            is_confession=False,
            status="skipped_no_confession",
            reason=None,
            parser_version=PARSER_VERSION,
        )

    segments: list[ConfessionSegment] = []
    seen_numbers: set[str] = set()
    has_duplicates = False
    has_empty_segment = False

    for i, match in enumerate(matches):
        cfs_num = match.group(1)
        if cfs_num in seen_numbers:
            has_duplicates = True
        seen_numbers.add(cfs_num)

        raw_header = match.group(0).strip()
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(post_text)

        raw_body = post_text[body_start:body_end]
        cleaned_body = _SEPARATOR_LINE_RE.sub("", raw_body).strip()
        cleaned_body = _TRAILING_PAGE_TAGS_RE.sub("", cleaned_body).strip()

        if not cleaned_body:
            has_empty_segment = True

        segments.append(
            ConfessionSegment(
                index=i,
                confession_number=cfs_num,
                text=cleaned_body,
                raw_header=raw_header,
                start_char=match.start(),
                end_char=body_end,
            )
        )

    if is_incomplete:
        return ConfessionParseResult(
            segments=segments,
            is_confession=True,
            status="needs_review",
            reason="incomplete_post",
            parser_version=PARSER_VERSION,
        )

    if has_duplicates or has_empty_segment:
        return ConfessionParseResult(
            segments=segments,
            is_confession=True,
            status="needs_review",
            reason="ambiguous_confession_parse",
            parser_version=PARSER_VERSION,
        )
    return ConfessionParseResult(
        segments=segments,
        is_confession=True,
        status="parsed",
        reason=None,
        parser_version=PARSER_VERSION,
    )


def parse_confessions(post_text: str) -> list[ConfessionSegment]:
    """Parse a post text into confession segments.

    Returns an empty list if no valid confession headers are found,
    signaling a non-confession post (announcement, event, meme, etc.).
    """
    res = parse_confessions_full(post_text)
    if res.status == "skipped_no_confession":
        return []
    return res.segments


def is_confession_post(post_text: str) -> bool:
    """Return True if the post text contains one or more valid confession headers."""
    res = parse_confessions_full(post_text)
    return res.is_confession and res.status == "parsed"
