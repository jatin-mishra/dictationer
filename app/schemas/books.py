import unicodedata
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, StringConstraints

MAX_CHAPTER = 1000
MAX_PAGE = 10_000


def _single_line_nfc(value: str) -> str:
    """Names are written into front matter as one `key: value` line, so newlines are rejected."""
    if len(value.splitlines()) > 1:
        raise ValueError("must be a single line")
    return unicodedata.normalize("NFC", value)


BookName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    AfterValidator(_single_line_nfc),
]
ChapterName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    AfterValidator(_single_line_nfc),
]


class CreateBookRequest(BaseModel):
    book_name: BookName


class BookResponse(BaseModel):
    book: str
    book_name: str
    created_at: str


class CreateChapterRequest(BaseModel):
    chapter_number: Annotated[int, Field(ge=1, le=MAX_CHAPTER)]
    chapter_name: ChapterName


class ChapterResponse(BaseModel):
    book: str
    chapter: int
    chapter_name: str
    created_at: str
    updated_at: str
    chapter_hash: str
