import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping

from app.schemas.instructions import (
    AddDetails,
    AddEdit,
    ChapterEdits,
    Edit,
    PageEdits,
    QuotedEdit,
    RemoveDetails,
    RemoveEdit,
    Unresolved,
    UpdateDetails,
    UpdateEdit,
)
from app.services.edit_executor import Span, conflicts, prepare_text, splits_letter
from app.storage.chapter_store import page_hash

Book = Mapping[int, Mapping[int, str]]  # {chapter: {page: content}}


class _Unplaced(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def find(content: str, quote: str | None, occurrence: int) -> tuple[int, int]:
    """Span of the `occurrence`-th (1-based) match of `quote` in `content`.

    Exact match on NFC text first; if there is none, one fallback that treats any run of
    whitespace as equal, mapped back to the original offsets. Nothing fuzzier: a wrong guess is
    worse than an unresolved instruction.
    """
    quote = unicodedata.normalize("NFC", quote or "")
    if not quote.strip() or occurrence < 1:
        raise _Unplaced("quote_not_found")
    spans = [(m.start(), m.end()) for m in re.finditer(re.escape(quote), content)]
    if not spans:
        loose = r"\s+".join(re.escape(word) for word in quote.split())
        spans = [(m.start(), m.end()) for m in re.finditer(loose, content)]
    if occurrence > len(spans):
        raise _Unplaced("quote_not_found")
    return spans[occurrence - 1]


_WORD = re.compile(r"\S+")


def _remove(start: int, end: int) -> tuple[Span, Edit]:
    details = RemoveDetails(from_offset=start, to_offset=end)
    return Span(start, end, ""), RemoveEdit(action="remove", details=details)


def _add(at: int, text: str) -> tuple[Span, Edit]:
    details = AddDetails(at_offset=at, size=len(text), text=text)
    return Span(at, at, text), AddEdit(action="add", details=details)


def _replace(content: str, start: int, end: int, new_text: str) -> tuple[Span, Edit]:
    """The smallest whole-word change that turns content[start:end] into new_text.

    The action comes from the diff, not from the LLM: dropping words is a remove, adding words is
    an add, anything else an update of only the words that differ. Whitespace between kept words
    follows the page. Word boundaries are whitespace, so this never splits a letter.
    """
    old = list(_WORD.finditer(content, start, end))
    new = list(_WORD.finditer(new_text))
    p = 0
    while p < min(len(old), len(new)) and old[p].group() == new[p].group():
        p += 1
    k = 0
    while k < min(len(old), len(new)) - p and old[-1 - k].group() == new[-1 - k].group():
        k += 1
    old_mid, new_mid = old[p : len(old) - k], new[p : len(new) - k]

    if not old_mid and not new_mid:
        raise _Unplaced("no_change")
    if not new_mid:  # words only dropped
        if k:  # a kept word follows: remove up to it
            return _remove(old_mid[0].start(), old[len(old) - k].start())
        return _remove(old[p - 1].end() if p else old_mid[0].start(), old_mid[-1].end())
    text = new_text[new_mid[0].start() : new_mid[-1].end()]
    if not old_mid:  # words only inserted, between kept words
        if p:
            sep = new_text[new[p - 1].end() : new_mid[0].start()] or " "
            return _add(old[p - 1].end(), sep + text)
        sep = new_text[new_mid[-1].end() : new[len(new) - k].start()] or " "
        return _add(old[len(old) - k].start(), text + sep)
    start, end = old_mid[0].start(), old_mid[-1].end()
    details = UpdateDetails(
        from_offset=start,
        to_offset=end,
        new_size=len(text),
        text=text,
        old_text=content[start:end],
    )
    return Span(start, end, text), UpdateEdit(action="update", details=details)


def _place(q: QuotedEdit, content: str) -> tuple[Span, Edit]:
    if q.action == "remove":
        return _remove(*find(content, q.target_text, q.occurrence))
    if q.new_text is None:
        raise _Unplaced("missing_text")
    text = prepare_text(q.new_text)
    if q.action == "update":
        return _replace(content, *find(content, q.target_text, q.occurrence), text)
    if q.position == "page_start":
        at = 0
    elif q.position == "page_end":
        at = len(content)
    elif q.position in ("before", "after"):
        start, end = find(content, q.anchor_text, q.occurrence)
        at = start if q.position == "before" else end
    else:
        raise _Unplaced("missing_position")
    return _add(at, text)


def resolve(edits: Iterable[QuotedEdit], book: Book) -> tuple[list[ChapterEdits], list[Unresolved]]:
    """Turn the LLM's quoted edits into offset edits for the pages in `book`.

    An edit that can't be placed safely (unknown page, quote not found, splits a letter, overlaps
    an edit already placed on that page) goes to `unresolved` instead; the rest still apply.
    """
    placed: dict[tuple[int, int], list[tuple[Span, Edit]]] = defaultdict(list)
    unresolved: list[Unresolved] = []
    for q in edits:
        try:
            content = book.get(q.chapter, {}).get(q.page)
            if content is None:
                raise _Unplaced("page_not_found")
            span, edit = _place(q, content)
            if splits_letter(content, span.start) or splits_letter(content, span.end):
                raise _Unplaced("splits_letter")
            if any(conflicts(span, other) for other, _ in placed[q.chapter, q.page]):
                raise _Unplaced("conflicts_with_other_edit")
            placed[q.chapter, q.page].append((span, edit))
        except _Unplaced as e:
            unresolved.append(
                Unresolved(
                    chapter=q.chapter, page=q.page, instruction=q.instruction, reason=e.reason
                )
            )

    by_chapter: dict[int, list[PageEdits]] = defaultdict(list)
    for (chapter, page), items in sorted(placed.items()):
        if items:
            by_chapter[chapter].append(
                PageEdits(
                    page=page,
                    content_sha256=page_hash(book[chapter][page]),
                    instructions=[edit for _, edit in items],
                )
            )
    chapters = [ChapterEdits(chapter=c, instructions=pages) for c, pages in by_chapter.items()]
    return chapters, unresolved
