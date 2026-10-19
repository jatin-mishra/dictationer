from enum import StrEnum

from pydantic import BaseModel


class Language(StrEnum):
    HI = "hi"
    EN = "en"
    HINGLISH = "hinglish"


class DictationResponse(BaseModel):
    text: str
    language: Language
    engine: str
    book: str
    chapter: int
    page: int
    warnings: list[str]


class PageResponse(BaseModel):
    book: str
    chapter: int
    page: int
    content: str
    available_pages: list[int]
