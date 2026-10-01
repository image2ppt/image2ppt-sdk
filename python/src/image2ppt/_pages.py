"""Local reading of the ``pages`` selection, e.g. ``"1-3, 7"``.

The API picks pages out of the whole submission laid end to end (an image is one
page, a PDF is its own page count). This module only answers what can be known
without the files: whether the spelling is valid, and how many pages it selects.

**It must never refuse a selection the service would accept.** Every rule here is
the contract's, and the Node client pins the identical ones. That includes what
counts as whitespace: the Node client uses JavaScript's set, and Python's
``str.strip`` / ``\\s`` disagree with it at the edges (U+FEFF is whitespace to one
and not the other), so the set is spelled out here to keep the two clients agreeing.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .errors import InvalidPagesError, PagesOutOfRangeError

#: Longest ``pages`` value the API accepts. Measured in characters here and in UTF-16
#: units by the Node client; the two only differ for characters outside the Basic
#: Multilingual Plane, and any such character is a spelling error on both sides anyway.
MAX_PAGES_LENGTH = 1000

#: JavaScript's whitespace — the set the Node client trims and matches against.
_WS = "\t\n\x0b\x0c\r \xa0  -     　﻿"
_TRIM = re.compile(f"^[{_WS}]+|[{_WS}]+$")
#: ``[0-9]``, not ``\\d``: Python's ``\\d`` also matches full-width and other Unicode
#: digits, which the service does not.
_TOKEN = re.compile(f"([0-9]+)(?:[{_WS}]*-[{_WS}]*([0-9]+))?")
#: Largest page number the API accepts (2**53 - 1). Past it the answer is
#: ``INVALID_PAGES``, not an out-of-range page — so it is here too.
_MAX_SAFE_INTEGER = 2**53 - 1

PageRanges = List[Tuple[int, int]]


def normalize_pages(pages: Optional[str]) -> Optional[str]:
    """Return ``pages`` if it selects anything, or None when it counts as not given.

    ``None``, an empty string, and a string of nothing but whitespace all mean "no
    selection" to the service, so none of them is sent. Anything but a string is a
    caller mistake: sent as a form field it would silently become text, and in a
    JSON body it would be refused — the same call behaving two different ways.
    """
    if pages is None:
        return None
    if not isinstance(pages, str):
        raise TypeError(f"pages must be a string like '1-3,7', not {type(pages).__name__}")
    return None if is_blank(pages) else pages


def is_blank(text: str) -> bool:
    """Whether ``text`` is empty once trimmed the way the service trims fields."""
    return not _TRIM.sub("", text)


def parse_pages(pages: str) -> PageRanges:
    """Parse a non-empty selection into ``(start, end)`` ranges, as written.

    Raises:
        InvalidPagesError: The spelling is wrong (``INVALID_PAGES``). Every token is
            checked for spelling before any range is, so ``"0,abc"`` is a spelling
            error, as it is to the service.
        PagesOutOfRangeError: A page number is 0 (``PAGES_OUT_OF_RANGE``).
    """
    # Measured after trimming: surrounding whitespace is not part of the value.
    length = len(_TRIM.sub("", pages))
    if length > MAX_PAGES_LENGTH:
        raise InvalidPagesError(
            f"pages is {length} characters long, over the {MAX_PAGES_LENGTH} allowed",
            code="INVALID_PAGES",
        )
    ranges: PageRanges = []
    for raw_token in pages.split(","):
        token = _TRIM.sub("", raw_token)
        match = _TOKEN.fullmatch(token)
        start = int(match.group(1)) if match else 0
        end = int(match.group(2)) if match and match.group(2) is not None else start
        if not match or max(start, end) > _MAX_SAFE_INTEGER or start > end:
            raise InvalidPagesError(
                f"pages {pages!r} is not a valid selection: write single pages or "
                "start-end ranges separated by commas, e.g. '1-3, 7'",
                code="INVALID_PAGES",
            )
        ranges.append((start, end))
    if any(start < 1 for start, _ in ranges):
        raise PagesOutOfRangeError(
            f"pages {pages!r} selects page 0; pages are numbered from 1",
            code="PAGES_OUT_OF_RANGE",
        )
    return ranges


def selected_page_count(ranges: PageRanges) -> int:
    """How many distinct pages the ranges select, overlaps counted once.

    Counted by merging, never by expanding: ``"1-1000000000000"`` is a valid spelling,
    and listing it out would be a very long wait for the answer "too many".
    """
    count = 0
    covered_to = 0
    for start, end in sorted(ranges):
        if end <= covered_to:
            continue
        count += end - max(start, covered_to + 1) + 1
        covered_to = end
    return count
