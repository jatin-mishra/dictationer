import re
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Header, Path, Query, Request, UploadFile, status
from starlette.requests import HTTPConnection

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.schemas.books import MAX_CHAPTER, MAX_PAGE
from app.schemas.dictation import DictationResponse, DictationType, Language, PageResponse
from app.services import speech
from app.services.idempotency import IdempotencyCache
from app.services.transcription.router import TranscriberRouter
from app.storage.chapter_store import (
    BookNotFoundError,
    ChapterNotFoundError,
    ChapterStore,
    page_hash,
    slugify,
)

router = APIRouter(prefix="/v1", tags=["dictation"])

_AUTHOR_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{8,128}")


class InvalidAuthorIdError(AppError):
    status_code = 400
    code = "invalid_author_id"


class InvalidIdempotencyKeyError(AppError):
    status_code = 400
    code = "invalid_idempotency_key"


class MissingFieldError(AppError):
    status_code = 422
    code = "missing_field"


def get_author_id(author_id: Annotated[str | None, Header()] = None) -> str:
    """The `author-id` header doubles as a folder name, so it is validated strictly."""
    if author_id is None or not _AUTHOR_ID.fullmatch(author_id):
        raise InvalidAuthorIdError("Header 'author-id' must match [A-Za-z0-9_-]{1,64}")
    return author_id


def get_idempotency_key(idempotency_key: Annotated[str | None, Header()] = None) -> str | None:
    if idempotency_key is not None and not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise InvalidIdempotencyKeyError("Header 'Idempotency-Key' must match [A-Za-z0-9_-]{8,128}")
    return idempotency_key


# HTTPConnection (not Request) so the live WebSocket route can share these dependencies.
def get_chapter_store(request: HTTPConnection) -> ChapterStore:
    store: ChapterStore = request.app.state.chapter_store
    return store


def get_transcriber_router(request: HTTPConnection) -> TranscriberRouter:
    transcriber_router: TranscriberRouter = request.app.state.transcriber_router
    return transcriber_router


def get_idempotency_cache(request: Request) -> IdempotencyCache:
    cache: IdempotencyCache = request.app.state.idempotency_cache
    return cache


AuthorId = Annotated[str, Depends(get_author_id)]
Store = Annotated[ChapterStore, Depends(get_chapter_store)]


@router.post("/dictations", status_code=status.HTTP_201_CREATED, response_model=DictationResponse)
async def create_dictation(
    author_id: AuthorId,
    idempotency_key: Annotated[str | None, Depends(get_idempotency_key)],
    audio_file: Annotated[UploadFile, Form(alias="audio")],
    language: Annotated[Language, Form()],
    book_name: Annotated[str, Form(min_length=1, max_length=128)],
    store: Store,
    transcribers: Annotated[TranscriberRouter, Depends(get_transcriber_router)],
    cache: Annotated[IdempotencyCache, Depends(get_idempotency_cache)],
    settings: Annotated[Settings, Depends(get_settings)],
    # type=laps sends the lap number in `chapter`; type=manuscript ignores chapter and page.
    dictation_type: Annotated[DictationType, Form(alias="type")] = DictationType.CHAPTER,
    chapter: Annotated[int | None, Form(ge=1, le=MAX_CHAPTER)] = None,
    page_number: Annotated[int | None, Form(ge=1, le=MAX_PAGE)] = None,
    merge: Annotated[bool | None, Query()] = None,
) -> DictationResponse:
    book = slugify(book_name)
    # false: segment texts are appended as-is (overlap repeated) — used to inspect raw overlap.
    merge_segments = settings.segment_merge if merge is None else merge
    if dictation_type is DictationType.MANUSCRIPT:
        chapter = page_number = None
    elif dictation_type is DictationType.LAPS:
        page_number = None
    required = {
        DictationType.CHAPTER: {"chapter": chapter, "page_number": page_number},
        DictationType.LAPS: {"chapter": chapter},
        DictationType.MANUSCRIPT: {},
    }[dictation_type]
    if missing := [name for name, value in required.items() if value is None]:
        raise MissingFieldError(f"type={dictation_type.value} requires {', '.join(missing)}")

    async def dictate() -> DictationResponse:
        # Fail before spending an STT call on a missing chapter / book.
        if dictation_type is DictationType.CHAPTER:
            assert chapter is not None
            if not await store.chapter_exists(author_id, book, chapter):
                raise ChapterNotFoundError(f"Chapter {chapter} not found")
        elif not await store.book_exists(author_id, book):
            raise BookNotFoundError(f"Book '{book}' not found")
        heard = await speech.transcribe_upload(
            audio_file, language, transcribers, settings, merge_segments
        )
        # Only reached when every step above succeeded: failures never write partial text.
        if dictation_type is DictationType.MANUSCRIPT:
            await store.append_to_manuscript(author_id, book, heard.text)
        elif dictation_type is DictationType.LAPS:
            assert chapter is not None
            await store.append_to_lap(author_id, book, chapter, heard.text)
        else:
            assert chapter is not None and page_number is not None
            await store.append_to_page(author_id, book, chapter, page_number, heard.text)
        is_lap = dictation_type is DictationType.LAPS
        return DictationResponse(
            text=heard.text,
            language=language,
            engine=heard.engine,
            book=book,
            type=dictation_type,
            chapter=None if is_lap else chapter,
            page=page_number,
            lap=chapter if is_lap else None,
            warnings=heard.warnings,
        )

    if idempotency_key is None:
        return await dictate()

    fingerprint = (dictation_type, language, book, chapter, page_number, merge_segments)
    async with cache.lock(author_id, idempotency_key):
        cached = cache.get(author_id, idempotency_key, fingerprint)
        if cached is not None:
            return cached
        response = await dictate()
        cache.put(author_id, idempotency_key, fingerprint, response)
        return response


@router.get("/books/{book_name}/chapters/{chapter_number}", response_model=PageResponse)
async def get_page(
    author_id: AuthorId,
    book_name: str,
    chapter_number: Annotated[int, Path(ge=1, le=MAX_CHAPTER)],
    store: Store,
    # type=laps reads lap {chapter_number}; type=manuscript ignores chapter_number and page.
    dictation_type: Annotated[DictationType, Query(alias="type")] = DictationType.CHAPTER,
    page: Annotated[int | None, Query(ge=1, le=MAX_PAGE)] = None,
) -> PageResponse:
    book = slugify(book_name)
    if dictation_type is DictationType.MANUSCRIPT:
        content = await store.read_manuscript(author_id, book)
        response = PageResponse(
            book=book,
            type=dictation_type,
            chapter=None,
            page=None,
            content=content,
            available_pages=[],
            content_sha256=page_hash(content),
        )
    elif dictation_type is DictationType.LAPS:
        content, laps = await store.read_lap(author_id, book, chapter_number)
        response = PageResponse(
            book=book,
            type=dictation_type,
            chapter=None,
            page=None,
            lap=chapter_number,
            content=content,
            available_pages=[],
            available_laps=laps,
            content_sha256=page_hash(content),
        )
    else:
        if page is None:
            raise MissingFieldError("type=chapter requires page")
        content, pages = await store.read_page(author_id, book, chapter_number, page)
        response = PageResponse(
            book=book,
            chapter=chapter_number,
            page=page,
            content=content,
            available_pages=pages,
            content_sha256=page_hash(content),
        )
    return response
