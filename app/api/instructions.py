from typing import Annotated

from fastapi import APIRouter, Depends, Form, Path, Request, UploadFile

from app.api.dictationer import AuthorId, Store, get_transcriber_router
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.schemas.books import MAX_CHAPTER
from app.schemas.dictation import Language
from app.schemas.instructions import AcceptResponse, ChapterEdits, InstructionResponse, PageContent
from app.services import speech
from app.services.instruction_extractor import InstructionExtractor
from app.services.llm import AgentLLM, LLMNotConfiguredError
from app.services.transcription.router import TranscriberRouter
from app.storage.chapter_store import page_hash, slugify

router = APIRouter(prefix="/v1", tags=["instructions"])


class ChapterMismatchError(AppError):
    status_code = 422
    code = "chapter_mismatch"


def get_agent_llm(request: Request) -> AgentLLM:
    llm: AgentLLM = request.app.state.agent_llm
    return llm


@router.post("/instructions", response_model=InstructionResponse)
async def create_instructions(
    author_id: AuthorId,
    audio_file: Annotated[UploadFile, Form(alias="audio")],
    language: Annotated[Language, Form()],
    book_name: Annotated[str, Form(min_length=1, max_length=128)],
    store: Store,
    transcribers: Annotated[TranscriberRouter, Depends(get_transcriber_router)],
    llm: Annotated[AgentLLM, Depends(get_agent_llm)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> InstructionResponse:
    """Proposes edits from a spoken instruction. Writes nothing: only /accept changes files."""
    book = slugify(book_name)
    # Cheap checks first, so a missing book or LLM never costs an STT call.
    pages = await store.read_book(author_id, book)
    if not llm.configured:
        raise LLMNotConfiguredError("ANTHROPIC_AGENT_API_KEY and ANTHROPIC_AGENT_MODEL must be set")
    heard = await speech.transcribe_upload(
        audio_file, language, transcribers, settings, settings.segment_merge
    )
    extractor = InstructionExtractor(llm, settings.instruction_max_book_chars)
    chapters, unresolved = await extractor.extract(heard.text, pages)
    return InstructionResponse(
        book=book, transcript=heard.text, instructions=chapters, unresolved=unresolved
    )


@router.post("/books/{book_name}/chapters/{chapter_number}/accept", response_model=AcceptResponse)
async def accept_instructions(
    author_id: AuthorId,
    book_name: str,
    chapter_number: Annotated[int, Path(ge=1, le=MAX_CHAPTER)],
    body: ChapterEdits,
    store: Store,
) -> AcceptResponse:
    """Applies one chapter block of an instruction_response exactly as given. No LLM."""
    if body.chapter != chapter_number:
        raise ChapterMismatchError(
            f"Body is for chapter {body.chapter}, path is chapter {chapter_number}"
        )
    book = slugify(book_name)
    pages = await store.apply_edits(author_id, book, chapter_number, body.instructions)
    return AcceptResponse(
        book=book,
        chapter=chapter_number,
        pages=[
            PageContent(page=n, content=content, content_sha256=page_hash(content))
            for n, content in pages.items()
        ],
    )
