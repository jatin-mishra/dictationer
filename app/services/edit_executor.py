import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.errors import AppError
from app.schemas.instructions import AddEdit, Edit, RemoveEdit
from app.services.script_normalizer import escape_markers

_JOINERS = {"्", "‍"}  # Devanagari virama, zero-width joiner


class InvalidEditError(AppError):
    status_code = 422
    code = "invalid_edit"


@dataclass(frozen=True)
class Span:
    """An edit as `content[start:end] → text`. start == end is an insert (a point)."""

    start: int
    end: int
    text: str

    @property
    def is_point(self) -> bool:
        return self.start == self.end


def prepare_text(text: str) -> str:
    """Inserted text is stored like dictated text: NFC, no page-marker lines."""
    return escape_markers(unicodedata.normalize("NFC", text))


def splits_letter(content: str, offset: int) -> bool:
    """True if a cut at `offset` would separate a consonant from its matra / nukta / virama
    or break a conjunct — it would leave an orphan combining mark behind."""
    if offset < len(content) and unicodedata.category(content[offset]).startswith("M"):
        return True
    return offset > 0 and content[offset - 1] in _JOINERS


def conflicts(a: Span, b: Span) -> bool:
    if a.is_point and b.is_point:
        return a.start == b.start  # two inserts at one offset: order would be ambiguous
    if a.is_point:
        return b.start < a.start < b.end
    if b.is_point:
        return a.start < b.start < a.end
    return a.start < b.end and b.start < a.end


def to_span(edit: Edit) -> tuple[Span, int, str | None]:
    """(span, declared size of the new text, expected old text if declared).
    Remove has no text, so its size is 0."""
    if isinstance(edit, RemoveEdit):
        d = edit.details
        return Span(d.from_offset, d.to_offset, ""), 0, None
    if isinstance(edit, AddEdit):
        a = edit.details
        return Span(a.at_offset, a.at_offset, a.text), a.size, None
    u = edit.details
    return Span(u.from_offset, u.to_offset, u.text), u.new_size, u.old_text


def check(content: str, span: Span, size: int, old_text: str | None = None) -> str | None:
    """Why `span` can't be applied to `content`, or None if it can."""
    if not 0 <= span.start <= span.end <= len(content):
        return f"offsets [{span.start}, {span.end}) out of range for length {len(content)}"
    if size != len(span.text):
        return f"size {size} != len(text) {len(span.text)}"
    if old_text is not None and content[span.start : span.end] != old_text:
        return "old_text does not match the page at these offsets"
    for offset in {span.start, span.end}:
        if splits_letter(content, offset):
            return f"offset {offset} splits a letter"
    return None


def apply(content: str, edits: Sequence[Edit]) -> str:
    """Validate every edit against `content`, then apply all of them, or raise and apply none.

    Offsets all refer to the original `content`. Edits are applied from the highest start
    offset down so earlier offsets stay valid; at the same offset the range edit goes first, so
    an insert placed at the start of a removed range survives.
    """
    spans: list[Span] = []
    for i, edit in enumerate(edits):
        span, size, old_text = to_span(edit)
        if (problem := check(content, span, size, old_text)) is not None:
            raise InvalidEditError(f"edit {i}: {problem}")
        for j, other in enumerate(spans):
            if conflicts(span, other):
                raise InvalidEditError(f"edit {i} overlaps edit {j}")
        spans.append(span)

    for span in sorted(spans, key=lambda s: (-s.start, s.is_point)):
        content = content[: span.start] + prepare_text(span.text) + content[span.end :]
    return content
