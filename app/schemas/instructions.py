from typing import Annotated, Literal

from pydantic import BaseModel, Field, NonNegativeInt

from app.schemas.books import MAX_CHAPTER, MAX_PAGE

# Offsets are Unicode code points (Python str indices) into the page content exactly as GET
# returns it; ranges are half-open [from_offset, to_offset). Range order, bounds and
# size == len(text) are checked by the edit executor against the page, so the error can name
# the page and the edit (422 invalid_edit) instead of a generic validation body.


class RemoveDetails(BaseModel):
    from_offset: NonNegativeInt
    to_offset: NonNegativeInt


class AddDetails(BaseModel):
    at_offset: NonNegativeInt
    size: NonNegativeInt
    text: str


class UpdateDetails(BaseModel):
    from_offset: NonNegativeInt
    to_offset: NonNegativeInt
    new_size: NonNegativeInt
    text: str
    # The text being replaced, for review. Always set by /instructions; optional on /accept,
    # where it must equal content[from_offset:to_offset] when given.
    old_text: str | None = None


class RemoveEdit(BaseModel):
    action: Literal["remove"]
    details: RemoveDetails


class AddEdit(BaseModel):
    action: Literal["add"]
    details: AddDetails


class UpdateEdit(BaseModel):
    action: Literal["update"]
    details: UpdateDetails


Edit = Annotated[RemoveEdit | AddEdit | UpdateEdit, Field(discriminator="action")]

ChapterNumber = Annotated[int, Field(ge=1, le=MAX_CHAPTER)]
PageNumber = Annotated[int, Field(ge=1, le=MAX_PAGE)]


class PageEdits(BaseModel):
    page: PageNumber
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    instructions: Annotated[list[Edit], Field(min_length=1, max_length=100)]


class ChapterEdits(BaseModel):
    """One chapter block of an instruction_response; also the /accept request body."""

    chapter: ChapterNumber
    instructions: Annotated[list[PageEdits], Field(min_length=1)]


class Unresolved(BaseModel):
    """An instruction the server could not turn into a safe edit; shown to the user as-is."""

    chapter: int | None
    page: int | None
    instruction: str
    reason: str


class InstructionResponse(BaseModel):
    book: str
    transcript: str
    instructions: list[ChapterEdits]
    unresolved: list[Unresolved]


class PageContent(BaseModel):
    page: int
    content: str
    content_sha256: str


class AcceptResponse(BaseModel):
    book: str
    chapter: int
    pages: list[PageContent]


# ── LLM structured output. The LLM quotes text; it never counts offsets. ──


class Scope(BaseModel):
    chapter: int | None = Field(description="Chapter number if spoken, else null")
    page: int | None = Field(description="Page number if spoken, else null")
    instruction: str = Field(description="One self-contained edit instruction")


class ScopeResult(BaseModel):
    scopes: list[Scope]


class QuotedEdit(BaseModel):
    chapter: int
    page: int
    action: Literal["remove", "add", "update"]
    target_text: str | None = Field(
        description="remove / update: text copied exactly from the page; null for add"
    )
    occurrence: int = Field(description="Which match of target_text / anchor_text, 1-based")
    anchor_text: str | None = Field(
        description="add with position before / after: exact page text next to the insertion"
    )
    position: Literal["before", "after", "page_start", "page_end"] | None = Field(
        description="add only: where new_text goes relative to anchor_text or the page"
    )
    new_text: str | None = Field(description="add / update: the text to insert; null for remove")
    instruction: str = Field(description="The instruction this edit carries out")


class LLMUnresolved(BaseModel):
    chapter: int | None
    page: int | None
    instruction: str


class EditResult(BaseModel):
    edits: list[QuotedEdit]
    unresolved: list[LLMUnresolved]
