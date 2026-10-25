from typing import Annotated

from fastapi import APIRouter, Depends, Form, Path, Query, Request, UploadFile

from app.api.dictationer import AuthorId, Store, get_transcriber_router
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.schemas.books import MAX_CHAPTER
from app.schemas.dictation import DictationType, Language
from app.schemas.instructions import AcceptResponse, ChapterEdits, InstructionResponse, PageContent
from app.services import speech
from app.services.instruction_extractor import Book, InstructionExtractor
from app.services.llm import AgentLLM, LLMNotConfiguredError
from app.services.locator import Locator
from app.services.transcription.router import TranscriberRouter
from app.storage.chapter_store import ChapterStore, page_hash, slugify

router = APIRouter(prefix="/v1", tags=["instructions"])


class ChapterMismatchError(AppError):
    status_code = 422
    code = "chapter_mismatch"


def get_agent_llm(request: Request) -> AgentLLM:
    llm: AgentLLM = request.app.state.agent_llm
    return llm


def get_locator(request: Request) -> Locator:
    locator: Locator = request.app.state.locator
    return locator


async def read_units(store: ChapterStore, author_id: str, book: str, kind: DictationType) -> Book:
    """The book in the extractor's {chapter: {page: text}} shape for the dictation type."""
    if kind is DictationType.MANUSCRIPT:
        text = await store.read_manuscript(author_id, book)
        return {1: {1: text}} if text else {}
    if kind is DictationType.LAPS:
        return {lap: {1: text} for lap, text in (await store.read_laps(author_id, book)).items()}
    return await store.read_book(author_id, book)


@router.post("/instructions", response_model=InstructionResponse)
async def create_instructions(
    author_id: AuthorId,
    audio_file: Annotated[UploadFile, Form(alias="audio")],
    language: Annotated[Language, Form()],
    book_name: Annotated[str, Form(min_length=1, max_length=128)],
    store: Store,
    transcribers: Annotated[TranscriberRouter, Depends(get_transcriber_router)],
    llm: Annotated[AgentLLM, Depends(get_agent_llm)],
    locator: Annotated[Locator, Depends(get_locator)],
    settings: Annotated[Settings, Depends(get_settings)],
    dictation_type: Annotated[DictationType, Form(alias="type")] = DictationType.CHAPTER,
) -> InstructionResponse:
    """Proposes edits from a spoken instruction. Writes nothing: only /accept changes files."""
    book = slugify(book_name)
    # Cheap checks first, so a missing book or LLM never costs an STT call.
    pages = await read_units(store, author_id, book, dictation_type)
    if not llm.configured:
        raise LLMNotConfiguredError("ANTHROPIC_AGENT_API_KEY and ANTHROPIC_AGENT_MODEL must be set")
    heard = await speech.transcribe_upload(
        audio_file, language, transcribers, settings, settings.segment_merge
    )
    extractor = InstructionExtractor(
        llm, locator, settings.instruction_max_book_chars, settings.locator_max_pages
    )
    chapters, unresolved = await extractor.extract(heard.text, pages, dictation_type)
    return InstructionResponse(
        book=book,
        type=dictation_type,
        transcript=heard.text,
        instructions=chapters,
        unresolved=unresolved,
    )


@router.post("/books/{book_name}/chapters/{chapter_number}/accept", response_model=AcceptResponse)
async def accept_instructions(
    author_id: AuthorId,
    book_name: str,
    chapter_number: Annotated[int, Path(ge=1, le=MAX_CHAPTER)],
    body: ChapterEdits,
    store: Store,
    # laps: path number is the lap number; manuscript: path and body numbers are ignored.
    dictation_type: Annotated[DictationType, Query(alias="type")] = DictationType.CHAPTER,
) -> AcceptResponse:
    """Applies one chapter block of an instruction_response exactly as given. No LLM."""
    if dictation_type is not DictationType.MANUSCRIPT and body.chapter != chapter_number:
        raise ChapterMismatchError(
            f"Body is for chapter {body.chapter}, path is chapter {chapter_number}"
        )
    book = slugify(book_name)
    if dictation_type is DictationType.MANUSCRIPT:
        chapter_number = 1
        pages = {1: await store.apply_manuscript_edits(author_id, book, body.instructions)}
    elif dictation_type is DictationType.LAPS:
        lap = await store.apply_lap_edits(author_id, book, chapter_number, body.instructions)
        pages = {1: lap}
    else:
        pages = await store.apply_edits(author_id, book, chapter_number, body.instructions)
    return AcceptResponse(
        book=book,
        type=dictation_type,
        chapter=chapter_number,
        pages=[
            PageContent(page=n, content=content, content_sha256=page_hash(content))
            for n, content in pages.items()
        ],
    )
