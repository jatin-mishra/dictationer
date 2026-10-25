from enum import StrEnum

from pydantic import BaseModel


class Language(StrEnum):
    HI = "hi"
    EN = "en"
    HINGLISH = "hinglish"


class DictationType(StrEnum):
    """Where a dictation is written: a chapter page, the book's manuscript.md, or laps.md."""

    CHAPTER = "chapter"
    MANUSCRIPT = "manuscript"
    LAPS = "laps"


class DictationResponse(BaseModel):
    text: str
    language: Language
    engine: str
    book: str
    type: DictationType = DictationType.CHAPTER
    chapter: int | None  # null unless type=chapter
    page: int | None  # null unless type=chapter
    lap: int | None = None  # set only for type=laps
    warnings: list[str]


class PageResponse(BaseModel):
    book: str
    type: DictationType = DictationType.CHAPTER
    chapter: int | None  # null unless type=chapter
    page: int | None  # null unless type=chapter
    lap: int | None = None  # set only for type=laps
    content: str
    available_pages: list[int]  # empty unless type=chapter
    available_laps: list[int] = []  # set only for type=laps
    content_sha256: str  # pass back to /accept so edits never land on a changed page
